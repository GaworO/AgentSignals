"""Account-bound, GET-only CrossTrade synchronization for Builder 50K.

Broker snapshots, executions and local order outcomes are separate evidence.
Flat snapshots never fabricate a fill, a cancellation or realized trade P&L.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

_INSTANCE = None
_LOCK = threading.Lock()
BASE = 'https://app.crosstrade.io/v1/api/tv'
NY = ZoneInfo('America/New_York')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class SyncError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AccountSync:
    def __init__(self, config, *, reader=None, clock=time.time):
        self.config = dict(config)
        self.enabled = config.get('BUILDER_BROKER_SYNC_ENABLED') == '1'
        self.account = str(config.get('BUILDER_BROKER_ACCOUNT_ID', ''))
        self.name = config.get('BUILDER_BROKER_ACCOUNT_NAME', '')
        self.environment = config.get('BUILDER_BROKER_ENVIRONMENT', '')
        self.token = config.get('BUILDER_BROKER_API_TOKEN', '')
        self.clock = clock
        self.interval = 15
        self.binding = hashlib.sha256(encode([self.account, self.name, self.environment]).encode()).hexdigest()
        self.path = Path(config.get('DATA_DIR', '.')) / 'broker_account_sync.sqlite3'
        self.reader = reader or self._read
        self.poll_lock = threading.Lock()
        self.api = None
        self.thread = None
        if self.enabled:
            if (config.get('ACCOUNT_PLAN') != 'builder50'
                    or config.get('BUILDER_BROKER_ACCOUNT_VERIFIED') != '1'
                    or not self.account.isdigit() or int(self.account) <= 0
                    or not self.name or len(self.name) > 128
                    or any(ord(c) < 32 for c in self.name)
                    or self.environment not in ('demo', 'live')
                    or not self.token or any(not 33 <= ord(c) <= 126 for c in self.token)):
                raise SyncError('EXPLICIT_VERIFIED_BUILDER_ACCOUNT_REQUIRED')

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.executescript('''
            CREATE TABLE IF NOT EXISTS account_snapshots (
                binding TEXT PRIMARY KEY, observed_at REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS sync_attempts (
                binding TEXT PRIMARY KEY, attempted_at REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS broker_fills (
                binding TEXT, fill_id TEXT, first_seen REAL, last_seen REAL,
                payload TEXT, PRIMARY KEY(binding,fill_id));
            CREATE TABLE IF NOT EXISTS account_changes (
                binding TEXT, fingerprint TEXT, observed_at REAL, payload TEXT,
                PRIMARY KEY(binding,fingerprint));
        ''')
        return c

    def _read(self, path):
        # No configurable origin, redirects, arbitrary paths or HTTP mutations.
        prefix = '/accounts/' + quote(self.name, safe='')
        if path not in ('/accounts', prefix, prefix+'/positions', prefix+'/orders', prefix+'/fills'):
            raise SyncError('BROKER_READ_ROUTE_NOT_ALLOWED')
        req = Request(BASE+path, method='GET', headers={
            'Authorization': 'Bearer '+self.token, 'Accept': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(req, timeout=10) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise SyncError('BROKER_RESPONSE_TOO_LARGE')
                return json.loads(raw)
        except HTTPError as exc:
            raise SyncError('BROKER_HTTP_'+str(exc.code)) from None
        except (URLError, OSError, TimeoutError):
            raise SyncError('BROKER_CONNECTION_FAILED') from None
        except (ValueError, UnicodeError):
            raise SyncError('BROKER_RESPONSE_INVALID') from None

    def _data(self, path):
        response = self.reader(path)
        if (not isinstance(response, dict) or response.get('success') is not True
                or response.get('partial') or response.get('unavailable')):
            raise SyncError('COMPLETE_BROKER_RESPONSE_REQUIRED')
        return response.get('data')

    def poll(self, *, wait=False):
        acquired = self.poll_lock.acquire(timeout=15) if wait and self.enabled else self.poll_lock.acquire(blocking=False) if self.enabled else False
        if not acquired:
            return False
        started = self.clock()
        try:
            accounts = self._data('/accounts')
            matches = [a for a in accounts if isinstance(a, dict) and a.get('name') == self.name] if isinstance(accounts, list) else []
            if (len(matches) != 1 or str(matches[0].get('accountId')) != self.account
                    or matches[0].get('environment') != self.environment
                    or matches[0].get('active') is not True or matches[0].get('archived') is True):
                raise SyncError('BROKER_ACCOUNT_BINDING_CHANGED_OR_INACTIVE')
            prefix = '/accounts/' + quote(self.name, safe='')
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = {key: pool.submit(self._data, prefix+suffix) for key, suffix in
                        [('account',''),('positions','/positions'),('orders','/orders'),('fills','/fills')]}
                values = {key: job.result() for key, job in jobs.items()}
            finished = self.clock()
            if not 0 <= finished-started <= 12:
                raise SyncError('BROKER_SNAPSHOT_TOO_SLOW')
            account = values['account']
            if (not isinstance(account, dict) or str(account.get('accountId')) != self.account
                    or account.get('name') != self.name or account.get('environment') != self.environment):
                raise SyncError('BROKER_ACCOUNT_READ_MISMATCH')
            balance = account.get('balance', {})
            if any(not finite(balance.get(k)) for k in ('netLiq','totalCashValue')):
                raise SyncError('BROKER_BALANCE_REQUIRED')
            for key in ('positions','orders','fills'):
                rows = values[key]
                if not isinstance(rows, list) or len(rows) > 10000:
                    raise SyncError('BROKER_ARRAY_REQUIRED')
                for row in rows:
                    if not isinstance(row, dict) or (row.get('accountId') is not None and str(row['accountId']) != self.account):
                        raise SyncError('BROKER_ROW_ACCOUNT_MISMATCH')
                    if key == 'fills' and (type(row.get('id')) is not int or row['id'] <= 0):
                        raise SyncError('BROKER_FILL_ID_REQUIRED')
            snapshot = dict(observed_at=finished, read_started_at=started, **values)
            fingerprint = hashlib.sha256(encode(values).encode()).hexdigest()
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                for fill in values['fills']:
                    identity = str(fill['id'])
                    prior = c.execute('SELECT payload FROM broker_fills WHERE binding=? AND fill_id=?', (self.binding,identity)).fetchone()
                    if prior:
                        old = json.loads(prior[0])
                        core = ('id','orderId','accountId','contractId','timestamp','qty','price','action')
                        if any(old.get(k) != fill.get(k) for k in core):
                            raise SyncError('IMMUTABLE_BROKER_FILL_CONFLICT')
                    c.execute('INSERT INTO broker_fills VALUES(?,?,?,?,?) ON CONFLICT(binding,fill_id) DO UPDATE SET last_seen=excluded.last_seen,payload=excluded.payload',
                              (self.binding,identity,finished,finished,encode(fill)))
                c.execute('INSERT OR REPLACE INTO account_snapshots VALUES(?,?,?)', (self.binding,finished,encode(snapshot)))
                c.execute('INSERT OR IGNORE INTO account_changes VALUES(?,?,?,?)', (self.binding,fingerprint,finished,encode(values)))
                c.execute('INSERT OR REPLACE INTO sync_attempts VALUES(?,?,NULL)', (self.binding,finished))
            # The complete account snapshot is committed before order reconciliation.
            # A failed order-specific read must not erase a valid account balance.
            callback = getattr(self, 'after_poll', None)
            if callback:
                try:
                    callback(snapshot)
                    self.reconciliation_error = None
                except Exception:
                    self.reconciliation_error = 'NATIVE_RECONCILIATION_FAILED'
            return True
        except Exception as exc:
            error = str(exc) if isinstance(exc, SyncError) else 'BROKER_SYNC_FAILED'
            with self.connect() as c:
                c.execute('INSERT OR REPLACE INTO sync_attempts VALUES(?,?,?)', (self.binding,self.clock(),error))
            return False
        finally:
            self.poll_lock.release()

    def snapshot(self):
        if not self.enabled:
            return None
        with self.connect() as c:
            row = c.execute('SELECT payload FROM account_snapshots WHERE binding=?', (self.binding,)).fetchone()
        return json.loads(row[0]) if row else None

    def state(self):
        if not self.enabled:
            return dict(enabled=False, state='DISABLED')
        snapshot = self.snapshot()
        with self.connect() as c:
            attempt = c.execute('SELECT attempted_at,error FROM sync_attempts WHERE binding=?', (self.binding,)).fetchone()
            fills = c.execute('SELECT count(*) FROM broker_fills WHERE binding=?', (self.binding,)).fetchone()[0]
        age = None if not snapshot else self.clock()-snapshot['read_started_at']
        error = attempt['error'] if attempt else None
        current = bool(snapshot and not error and 0 <= age <= 30)
        balance = snapshot['account']['balance'] if snapshot else {}
        return dict(enabled=True, provider='CrossTrade', state='CURRENT' if current else 'ERROR' if error else 'STALE' if snapshot else 'WAITING',
            current=current, error=error, age_seconds=round(age,1) if age is not None else None,
            observed_at=snapshot['observed_at'] if snapshot else None,
            equity=balance.get('netLiq'), cash=balance.get('totalCashValue'),
            risk_equity=min(balance['netLiq'],balance['totalCashValue']) if snapshot else None,
            open_positions=len(snapshot['positions']) if snapshot else None,
            working_orders=len(snapshot['orders']) if snapshot else None,
            session_fills=len(snapshot['fills']) if snapshot else None, stored_fills=fills,
            poll_interval_seconds=self.interval,
            reconciliation_error=getattr(self,'reconciliation_error',None),
            reconciliation='Broker account inventory and equity; native order IDs reconcile individual outcomes when selected.')

    def entry_blocker(self, *, refresh=False, pending_order_id=None):
        if not self.enabled:
            return None
        if refresh and not self.poll(wait=True):
            return 'broker:account_refresh_failed'
        state = self.state()
        if not state['current']:
            return 'broker:account_snapshot_not_current'
        if state['open_positions'] or state['working_orders']:
            return 'broker:account_has_positions_or_orders'
        import builder_crosstrade
        if getattr(self,'reconciliation_error',None): return 'broker:native_reconciliation_failed'
        return (builder_crosstrade.entry_blocker(pending_order_id=pending_order_id)
                if pending_order_id is not None else builder_crosstrade.entry_blocker())

    def last_confirmed_fill(self):
        """Activity evidence survives session reset; it does not settle costs."""
        if not self.enabled:return None
        with self.connect() as c:
            rows=c.execute('SELECT payload FROM broker_fills WHERE binding=?',(self.binding,)).fetchall()
        found=[]
        for row in rows:
            try:
                f=json.loads(row[0]);at=datetime.fromisoformat(f['timestamp'].replace('Z','+00:00'))
                if (at.tzinfo is None or not 0<at.timestamp()<=self.clock()
                        or f.get('active') is not True or type(f.get('id')) is not int or f['id']<=0
                        or type(f.get('qty')) is not int or f['qty']<=0
                        or f.get('action') not in ('Buy','Sell')
                        or f.get('accountId') is not None and str(f['accountId'])!=self.account):continue
                found.append((at.timestamp(),dict(date=at.astimezone(NY).date().isoformat(),
                    et=at.astimezone(NY).isoformat(),strat='Broker-confirmed fill',qty=f['qty'])))
            except (ValueError,KeyError,TypeError,OverflowError):continue
        return max(found,key=lambda item:item[0])[1] if found else None

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        def run():
            while True:
                try:
                    self.poll()
                except Exception:
                    # Storage failures must not kill future recovery attempts.
                    # The old timestamp remains stale and entry checks block.
                    pass
                time.sleep(self.interval)
        self.thread = threading.Thread(target=run, daemon=True, name='builder-broker-read-sync')
        self.thread.start()


def instance():
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = AccountSync(os.environ)
        return _INSTANCE


def state():
    return instance().state()


def entry_blocker(*, refresh=False, pending_order_id=None):
    return instance().entry_blocker(refresh=refresh, pending_order_id=pending_order_id)


def overlay_equity(saved_state):
    if not instance().enabled:
        return saved_state
    snapshot = instance().snapshot()
    if not snapshot:
        return saved_state
    balance = snapshot['account']['balance']
    return dict(saved_state, equity=min(balance['netLiq'],balance['totalCashValue']),
        equity_ts=int(snapshot['observed_at']*1000),
        equity_sync_day=datetime.fromtimestamp(snapshot['observed_at'],NY).strftime('%Y-%m-%d'),
        equity_source='crosstrade', broker_cash=balance['totalCashValue'], broker_net_liq=balance['netLiq'])


def start():
    instance().start()

