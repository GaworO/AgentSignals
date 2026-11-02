"""Durable, account-bound GET backoff and redacted request accounting.

Never retries a request or gates protective POSTs. Cooldowns apply to both the
account poller and native-order reads, including after process restarts.
"""
import json
import math
import time
from email.utils import parsedate_to_datetime


def tables(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS broker_read_budget (
            binding TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT);
        CREATE TABLE IF NOT EXISTS broker_read_counts (
            binding TEXT, day TEXT, route TEXT, attempts INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0, throttled INTEGER DEFAULT 0,
            PRIMARY KEY(binding,day,route));
        CREATE TABLE IF NOT EXISTS broker_read_pacing (
            binding TEXT PRIMARY KEY, next_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS broker_read_streak (
            binding TEXT,route TEXT,failures INTEGER,
            PRIMARY KEY(binding,route));
    ''')


def route(path):
    p=path.split('?',1)[0]
    if p.startswith('/accounts/'):
        parts=p.split('/')[3:]
        return '/accounts/{account}/'+'/'.join('{id}' if x.isdigit() else x for x in parts)
    return p


def status(sync):
    with sync.connect() as db:
        tables(db)
        row=db.execute('SELECT until,reason FROM broker_read_budget WHERE binding=?',(sync.binding,)).fetchone()
        from datetime import datetime, timezone
        day=datetime.fromtimestamp(sync.clock(),timezone.utc).date().isoformat()
        counts=db.execute('SELECT route,attempts,failures,throttled FROM broker_read_counts WHERE binding=? AND day=?',(sync.binding,day)).fetchall()
    return dict(retry_at=row[0] if row else 0, reason=row[1] if row else None,
                remaining_seconds=max(0,round((row[0] if row else 0)-sync.clock(),2)),
                counts_day_utc=day, routes=[dict(zip(('route','attempts','failures','throttled'),r)) for r in counts])


def count(sync,path,field):
    assert field in ('attempts','failures','throttled')
    from datetime import datetime, timezone
    day=datetime.fromtimestamp(sync.clock(),timezone.utc).date().isoformat()
    with sync.connect() as db:
        tables(db)
        db.execute('INSERT OR IGNORE INTO broker_read_counts(binding,day,route) VALUES(?,?,?)',(sync.binding,day,route(path)))
        db.execute('UPDATE broker_read_counts SET '+field+'='+field+'+1 WHERE binding=? AND day=? AND route=?',(sync.binding,day,route(path)))


def succeeded(sync,path):
    with sync.connect() as db:
        tables(db)
        db.execute('DELETE FROM broker_read_streak WHERE binding=? AND route=?',(sync.binding,route(path)))


def before(sync,path):
    s=status(sync)
    if s['remaining_seconds']>0:
        from builder_broker_sync import SyncError
        raise SyncError('BROKER_READ_BACKOFF_'+s['reason'])
    # CrossTrade publishes 3 requests/sec shared with writes. Pace this
    # integration's GETs at 2/sec, leaving capacity for protective operations.
    # Shared SQLite reservations prevent separate workers bursting together.
    now=sync.clock()
    with sync.connect() as db:
        tables(db)
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT next_at FROM broker_read_pacing WHERE binding=?',(sync.binding,)).fetchone()
        at=max(now,row[0] if row else now)
        if at-now>5:
            from builder_broker_sync import SyncError
            raise SyncError('BROKER_READ_BACKOFF_PACING_BUSY')
        db.execute('INSERT OR REPLACE INTO broker_read_pacing VALUES(?,?)',(sync.binding,at+.5))
    if at>now:time.sleep(at-now)
    # Another read may have established a provider cooldown while we waited.
    s=status(sync)
    if s['remaining_seconds']>0:
        from builder_broker_sync import SyncError
        raise SyncError('BROKER_READ_BACKOFF_'+s['reason'])
    count(sync,path,'attempts')


def http_error(sync,path,exc):
    count(sync,path,'failures')
    payload={}
    try:
        raw=json.loads(exc.read(8193))
        if isinstance(raw,dict):payload=raw
    except (ValueError,UnicodeError,OSError):pass
    if exc.code==403 and payload.get('error')=='tradovate_session_expired':
        return 'TRADOVATE_SESSION_EXPIRED_RECONNECT_REQUIRED'
    if exc.code!=429:return 'BROKER_HTTP_'+str(exc.code)
    reasons={'rate_limited':'CROSSTRADE_RATE_LIMITED',
             'Rate limit exceeded':'CROSSTRADE_RATE_LIMITED',
             'broker_rate_limited':'TRADOVATE_RATE_LIMITED',
             'snapshot_refresh_pending':'BALANCE_REFRESH_PENDING',
             'Data transfer limit exceeded for this endpoint':'DATA_TRANSFER_LIMITED'}
    provider_error=payload.get('error')
    reason=reasons.get(provider_error,'HTTP_429_UNKNOWN') if isinstance(provider_error,str) else 'HTTP_429_UNKNOWN'
    delays=[]
    for value in (exc.headers.get('Retry-After') if exc.headers else None,payload.get('retryAfter')):
        try:
            seconds=float(value)
        except (TypeError,ValueError):
            try:seconds=parsedate_to_datetime(value).timestamp()-sync.clock()
            except (TypeError,ValueError,OverflowError):continue
        if math.isfinite(seconds) and seconds>=0:delays.append(seconds)
    fallback=1 if reason=='BALANCE_REFRESH_PENDING' else 2 if reason=='CROSSTRADE_RATE_LIMITED' else 60
    delay=max([1]+delays) if delays else fallback
    with sync.connect() as db:
        tables(db)
        db.execute('BEGIN IMMEDIATE')
        db.execute('''INSERT INTO broker_read_streak VALUES(?,?,1)
            ON CONFLICT(binding,route) DO UPDATE SET failures=failures+1''',(sync.binding,route(path)))
        streak=db.execute('SELECT failures FROM broker_read_streak WHERE binding=? AND route=?',(sync.binding,route(path))).fetchone()[0]
        # Provider Retry-After can be only a short hint for an unknown broker
        # penalty. Repeated refusals must slow down, never form a retry storm.
        maximum=15 if reason=='BALANCE_REFRESH_PENDING' else 60 if reason=='CROSSTRADE_RATE_LIMITED' else 3600
        delay=max(delay,min(maximum,fallback*2**min(streak-1,12)))
        db.execute('''INSERT INTO broker_read_budget VALUES(?,?,?)
            ON CONFLICT(binding) DO UPDATE SET until=max(until,excluded.until),
            reason=CASE WHEN excluded.until>=until THEN excluded.reason ELSE reason END''',
            (sync.binding,sync.clock()+delay,reason))
    count(sync,path,'throttled')
    return 'BROKER_HTTP_429_'+reason
