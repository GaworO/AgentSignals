"""CrossTrade quotes: durable monitoring and fail-closed executable validation.

The market endpoint is an NT8 Add-On feed, never cloud Tradovate market data.
Only documented quote timestamps are used; removed internal fields are ignored.
Candles measure volatility only, never stand in for bid/ask.
"""
import json
import math
import os
import re
import sqlite3
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

import ab_v3_live
from builder_broker_sync import NoRedirect

# Operational collection limits, NOT approved entry/spread/age thresholds.
MONITOR_MAX_AGE_MS = 10000
MONITOR_INTERVAL_SECONDS = 30
REQUEST_INTERVAL_SECONDS = 2
MAX_BODY = 65536


def instrument_name(contract):
    if not isinstance(contract, str) or not re.fullmatch(r'MNQ[HMUZ]20\d{2}', contract):
        raise ValueError('quote_explicit_contract_required')
    return 'MNQ '+{'H':'03','M':'06','U':'09','Z':'12'}[contract[3]]+'-'+contract[-2:]


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def quote_timestamps(raw):
    """REST epoch is seconds; quoteTimeUtc is UTC. Never guess local time zones.

    quoteTime is accepted alone only when it carries an explicit offset. The
    documented NT8 local display string is retained raw, but cannot establish
    freshness without epoch/quoteTimeUtc. This also avoids DST ambiguity.
    """
    stamps={}
    if raw.get('epoch') is not None:
        value=raw['epoch']
        if not number(value) or value<=0:raise ValueError('quote_provider_timestamp_required')
        stamps['epoch']=value*1000
    for key in ('quoteTimeUtc','quoteTime'):
        value=raw.get(key)
        if value is None:continue
        if not isinstance(value,str):raise ValueError('quote_provider_timestamp_invalid')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?',value):
            if key=='quoteTime':continue
            raise ValueError('quote_provider_timestamp_invalid')
        try:dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:
            if key=='quoteTime':continue  # NT8 local display, no known zone.
            raise ValueError('quote_provider_timestamp_invalid') from None
        if dt.tzinfo is None:
            if key=='quoteTime':continue
            dt=dt.replace(tzinfo=timezone.utc)
        elif key=='quoteTimeUtc' and dt.utcoffset().total_seconds()!=0:
            raise ValueError('quote_utc_timestamp_offset_invalid')
        stamps[key]=dt.timestamp()*1000
    if not stamps:raise ValueError('quote_provider_timestamp_required')
    # Documented display timestamps have second precision; use the oldest.
    if max(stamps.values())-min(stamps.values())>=1000:
        raise ValueError('quote_provider_timestamps_conflict')
    return stamps


def inspect_quote(raw, contract, now_ms, max_age_ms):
    """Validate documented NT8 data separately from deployment feed attestation."""
    expected=instrument_name(contract)
    if not isinstance(raw, dict): raise ValueError('quote_object_required')
    if raw.get('success') is False or raw.get('partial') or raw.get('unavailable') or raw.get('stale'):
        raise ValueError('quote_unavailable')
    if (raw.get('instrument') != expected or raw.get('tickSize') != .25
            or raw.get('pointValue') != 2):
        raise ValueError('quote_contract_metadata_mismatch')
    bid,ask=raw.get('bid'),raw.get('ask')
    if any(not number(v) or v <= 0 or abs(v/.25-round(v/.25))>1e-8 for v in (bid,ask)) or bid>ask:
        raise ValueError('quote_bid_ask_invalid')
    stamps=quote_timestamps(raw)
    at=min(stamps.values())
    if any(not 0<=now_ms-v<=max_age_ms for v in stamps.values()):
        raise ValueError('quote_stale_or_future')
    nt8=(raw.get('type')=='Future' and isinstance(raw.get('dataSource'),dict)
         and raw['dataSource'].get('status')=='Live')
    return dict(contract=contract, instrument=expected, source='crosstrade_nt8',
                bid=bid,ask=ask,spread_points=ask-bid,spread_ticks=(ask-bid)/.25,
                as_of_ms=int(at),quote_age_ms=now_ms-at,provider_timestamps_ms=stamps, observed_at_ms=now_ms,
                timestamp_units='epoch_seconds_and_utc_datetime',
                data_source=raw.get('dataSource'),provider_quote_time=raw.get('quoteTime'),
                provider_expiration=raw.get('expiration'),
                feed_candidate=nt8, timestamp_semantics_verified=False, execution_eligible=False,
                timestamp_semantics='UNVERIFIED_PROVIDER_TIMESTAMP',
                provenance_reason='quote_source_suitability_unverified',
                market_open=raw.get('isMarketOpen') is True)


def attest_quote(result, parameters):
    # Provider guidance confirms real-time NT8 quotes and the supported clocks.
    # It does not connect this installation or establish its CME entitlement.
    # Bind runtime feed/contract verification in the policy before execution.
    proof=(parameters or {}).get('quote_verification',{})
    now=result.get('observed_at_ms')
    start,end=proof.get('verified_at_ms'),proof.get('expires_at_ms')
    current=(type(now) is int and type(start) is int and type(end) is int and 0 < start <= now < end)
    if (current and result.get('feed_candidate') and proof.get('verified') is True
            and proof.get('source')==result['source']
            and proof.get('contract')==result['contract']
            and proof.get('reference')
            and proof.get('timestamp_semantics')=='bid_ask_event_time'
            and proof.get('venue_bbo') is True):
        result.update(execution_eligible=True,timestamp_semantics_verified=True,
                      timestamp_semantics=proof['timestamp_semantics'],
                      provenance_reference=proof['reference'],provenance_expires_at_ms=end,provenance_reason=None)
    return result


def normalize(raw, order, parameters, now_ms):
    result=attest_quote(inspect_quote(raw,order['contract'],now_ms,parameters['max_quote_age_ms']),parameters)
    if not result['execution_eligible']:raise ValueError('quote_source_suitability_unverified')
    return result


class QuoteClient:
    def __init__(self, *, clock=time.time, monotonic=time.monotonic, reader=None, path=None):
        self.clock,self.monotonic,self.reader=clock,monotonic,reader
        self.path=Path(path) if path else Path(os.environ.get('DATA_DIR','/data'))/'directional_quotes.sqlite3'

    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=5)
        db.execute('CREATE TABLE IF NOT EXISTS quote_rate (id INTEGER PRIMARY KEY, next_at REAL NOT NULL)')
        db.execute("CREATE TABLE IF NOT EXISTS quotes (id INTEGER PRIMARY KEY, observed_ms INTEGER, contract TEXT, order_id TEXT, purpose TEXT, evidence TEXT NOT NULL)")
        db.execute('CREATE INDEX IF NOT EXISTS quotes_observed ON quotes(observed_ms)')
        return db

    def reserve(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT next_at FROM quote_rate WHERE id=1').fetchone()
            now=self.clock()
            if row and now<row[0]:raise ValueError('quote_rate_limited_locally')
            db.execute('INSERT OR REPLACE INTO quote_rate VALUES(1,?)',(now+REQUEST_INTERVAL_SECONDS,))

    def defer(self, seconds):
        with self.connect() as db:
            db.execute('INSERT INTO quote_rate VALUES(1,?) ON CONFLICT(id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)',(self.clock()+seconds,))

    def read(self, contract):
        token=os.environ.get('DIRECTIONAL_QUOTE_API_TOKEN') or os.environ.get('BUILDER_BROKER_API_TOKEN')
        if not token:raise ValueError('quote_access_not_configured')
        url='https://app.crosstrade.io/v1/api/market/quote?'+urlencode(dict(instrument=instrument_name(contract)))
        with build_opener(NoRedirect()).open(Request(url,headers={'Authorization':'Bearer '+token}),timeout=5) as response:
            self.http_status=response.status
            body=response.read(MAX_BODY+1)
            if len(body)>MAX_BODY:raise ValueError('quote_response_too_large')
            self.response_text=body.decode('utf-8',errors='replace')
            return json.loads(body)

    def fetch(self, contract, *, parameters=None, order_id=None, execution=False):
        start=self.clock();mono=self.monotonic()
        evidence=dict(request_started_ms=int(start*1000),allowed=False,reason=None,raw=None,
                      monitoring_only=not execution,diagnostic_max_age_ms=MONITOR_MAX_AGE_MS if not execution else None)
        result=None
        self.response_text=None
        self.http_status=None
        try:
            instrument_name(contract)
            self.reserve()
            raw=(self.reader or self.read)(contract)
            # Quote endpoint is market-only; redact a credential if a provider echoes it.
            text=json.dumps(raw)
            self.response_text=text if self.response_text is None else self.response_text
            for key in ('DIRECTIONAL_QUOTE_API_TOKEN','BUILDER_BROKER_API_TOKEN'):
                secret=os.environ.get(key)
                if secret:text=text.replace(secret,'[REDACTED]')
            try:
                json.dumps(raw,allow_nan=False)
                evidence['raw']=json.loads(text)
            except ValueError:pass
            now=int(self.clock()*1000)
            # Internal source/pricingBasis/etc are not a provider contract.
            # Detect only documented connection/feed-status changes.
            previous=self.latest(contract=contract, with_raw=True)
            prior=(previous or {}).get('raw') or {}
            evidence['data_source_changed']=bool(prior and prior.get('dataSource')!=raw.get('dataSource'))
            evidence['previous_data_source']=prior.get('dataSource')
            result=inspect_quote(raw,contract,now,(parameters or {}).get('max_quote_age_ms',MONITOR_MAX_AGE_MS))
            result=attest_quote(result,parameters)
            evidence['quote']=result
            if execution:
                if not result['execution_eligible']:raise ValueError('quote_source_suitability_unverified')
                if os.environ.get('DIRECTIONAL_QUOTE_SOURCE')!='crosstrade_nt8':raise ValueError('executable_quote_access_not_configured')
                if parameters is None:raise ValueError('validated_execution_policy_missing')
                latency=(self.monotonic()-mono)*1000
                if latency>parameters.get('max_quote_latency_ms',parameters['max_quote_age_ms']):
                    raise ValueError('quote_request_latency_exceeded')
            evidence['allowed']=bool(execution and result['execution_eligible'])
            evidence['reason']=result['provenance_reason']
        except HTTPError as exc:
            self.http_status=exc.code
            try:self.response_text=exc.read(MAX_BODY).decode('utf-8',errors='replace')
            except Exception:pass
            evidence['reason']='quote_http_'+str(exc.code)
            if exc.code==429:
                try:delay=float(exc.headers.get('Retry-After','7'))
                except (TypeError,ValueError):delay=7
                self.defer(max(7,delay) if number(delay) else 7)
            elif exc.code>=500:self.defer(7)
        except (TimeoutError,OSError) as exc:
            evidence['reason']='quote_transport_unavailable'
            self.defer(7)
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            # Never log arbitrary exception/HTTP bodies that may contain credentials.
            reason=str(exc)
            evidence['reason']=reason if re.fullmatch(r'[a-z_]+',reason) else 'quote_response_invalid'
        if self.http_status is not None:evidence['http_status']=self.http_status
        if self.response_text is not None:
            text=self.response_text
            for key in ('DIRECTIONAL_QUOTE_API_TOKEN','BUILDER_BROKER_API_TOKEN'):
                secret=os.environ.get(key)
                if secret:text=text.replace(secret,'[REDACTED]')
            evidence['original_response_text']=text
        evidence.update(observed_ms=int(self.clock()*1000),request_latency_ms=(self.monotonic()-mono)*1000)
        with self.connect() as db:
            db.execute('INSERT INTO quotes(observed_ms,contract,order_id,purpose,evidence) VALUES(?,?,?,?,?)',
                       (evidence['observed_ms'],contract,order_id,'execution' if execution else 'monitor',json.dumps(evidence,allow_nan=False)))
            # Retain 30 days, bounded also for pathological caller traffic.
            db.execute('DELETE FROM quotes WHERE observed_ms<? OR id < (SELECT MAX(id)-100000 FROM quotes)',(evidence['observed_ms']-30*86400000,))
        if execution and not evidence['allowed']:raise ValueError(evidence['reason'] or 'quote_validation_failed')
        if execution:return dict(result,request_latency_ms=evidence['request_latency_ms'])
        return evidence

    def latest(self, *, contract=None, with_raw=False):
        if not self.path.exists():return None
        with self.connect() as db:
            filters=[];args=[]
            if contract:
                filters.append('contract=?');args.append(contract)
            if with_raw:
                filters.append("json_type(evidence,'$.raw')='object'")
            where=(' WHERE '+' AND '.join(filters)) if filters else ''
            row=db.execute('SELECT evidence FROM quotes'+where+' ORDER BY id DESC LIMIT 1',args).fetchone()
        if not row:return None
        result=json.loads(row[0])
        result['observation_age_ms']=int(self.clock()*1000)-result['observed_ms']
        # This is a status snapshot, never an executable cached quote.
        return result


def monitor_once():
    import execution_validation, directional_resting_limit
    try:
        if execution_validation.policy().get('execution_model') == directional_resting_limit.MODEL:
            return None  # No NT8 polling for the explicit cloud-only policy.
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if os.environ.get('DIRECTIONAL_QUOTE_MONITOR_ENABLED')!='true':return None
    client=QuoteClient();latest=client.latest()
    if latest and latest['observation_age_ms']<MONITOR_INTERVAL_SECONDS*1000:return latest
    contract=os.environ.get('AB_V3_CONTRACT','')
    return client.fetch(contract)


_MONITOR_THREAD=None
_MONITOR_LOCK=threading.Lock()

def start_monitor():
    global _MONITOR_THREAD
    if os.environ.get('DIRECTIONAL_QUOTE_MONITOR_ENABLED')!='true':return
    with _MONITOR_LOCK:
        if _MONITOR_THREAD and _MONITOR_THREAD.is_alive():return
        def loop():
            while True:
                try:monitor_once()
                except Exception as exc:
                    print('[directional_quotes] monitor failed:',type(exc).__name__,flush=True)
                time.sleep(MONITOR_INTERVAL_SECONDS)
        _MONITOR_THREAD=threading.Thread(target=loop,daemon=True,name='directional-quotes')
        _MONITOR_THREAD.start()


class MarketSource:
    def __init__(self, *, clock=time.time, reader=None, market_open=None):
        self.market_open=market_open
        self.clock=clock
        self.client=QuoteClient(clock=clock,reader=reader)

    def __call__(self, order):
        import execution_validation
        p=execution_validation.policy_for(order)
        import directional_resting_limit
        if p.get('execution_model') == directional_resting_limit.MODEL:
            return directional_resting_limit.CandleSource(clock=self.clock, market_open=self.market_open)(order)
        if p.get('quote_source') != 'crosstrade_nt8':
            raise ValueError('quote_provider_not_supported')
        n=p.get('volatility_window_bars')
        if p.get('volatility_definition') != 'closed_m1_max_true_range' or type(n) is not int or not 1<=n<=1440:
            raise ValueError('validated_volatility_definition_required')
        now=int(self.clock()*1000);end=now//60000*60000
        with ab_v3_live.connect() as db:
            rows=db.execute("SELECT ts_ms,bar_json FROM bars WHERE tf='M1' AND contract=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                            (order['contract'],end-(n+1)*60000,end)).fetchall()
        if [r['ts_ms'] for r in rows] != list(range(end-(n+1)*60000,end,60000)):
            raise ValueError('volatility_reference_candles_missing')
        bars=[json.loads(r['bar_json']) for r in rows]
        ranges=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars,bars[1:])]
        result=self.client.fetch(order['contract'], parameters=p, order_id=order.get('order_id'), execution=True)
        if int(self.clock()*1000)//60000*60000 != end:
            raise ValueError('volatility_reference_rolled_during_read')
        return dict(result,volatility_points=max(ranges),volatility_definition=p['volatility_definition'])
