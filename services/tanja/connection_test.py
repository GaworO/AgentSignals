"""TradersPost transport test only. Every outbound signal has test=True.

No non-test sender, account reader, AI-order link or broker execution exists here.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

URL_PATTERN = r'https://webhooks\.traderspost\.io/trading/webhook/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[A-Za-z0-9_-]{8,256}'
TICKER_PATTERN = r'MNQ[HMUZ]20[0-9]{2}'
ID_PATTERN = r'[0-9a-f]{32}'


class TestBlocked(ValueError):
    pass


class TransportError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def test_payload(ticker, request_id, now):
    if not re.fullmatch(TICKER_PATTERN, ticker) or not re.fullmatch(ID_PATTERN, request_id):
        raise TestBlocked('Explicit MNQ contract and valid test ID required')
    iso=lambda ts:datetime.fromtimestamp(ts,timezone.utc).isoformat().replace('+00:00','Z')
    return dict(ticker=ticker, action='buy', sentiment='bullish', orderType='market',
        quantity=1, quantityType='fixed_quantity', test=True, time=iso(now), expiresAt=iso(now+60),
        message='TANJA CONNECTION TEST ONLY — NO BROKER ORDERS',
        extras={'source':'tanja-connection-test','request_id':request_id})


def send_test(url, payload):
    # Revalidate at the network boundary. Configuration cannot enable live sends.
    if not re.fullmatch(URL_PATTERN,url): raise TestBlocked('Invalid TradersPost test webhook URL')
    if (payload.get('test') is not True or payload.get('quantity') != 1
        or payload.get('action') != 'buy' or payload.get('orderType') != 'market'
        or not re.fullmatch(TICKER_PATTERN,payload.get('ticker',''))):
        raise TestBlocked('Only the fixed no-order connection test is supported')
    request=urllib.request.Request(url,data=json.dumps(payload,allow_nan=False).encode(),method='POST',
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=10) as response:
            body=response.read(65537)
            if len(body)>65536:raise TransportError('RESPONSE_TOO_LARGE')
            result=json.loads(body)
            if not isinstance(result,dict):raise TransportError('UNRECOGNIZED_RESPONSE')
            # Never preserve provider message text, headers, redirects or secret URLs.
            receipt={key:result[key] for key in ('success','id','messageCode') if key in result}
            if type(receipt.get('success')) is not bool:raise TransportError('UNRECOGNIZED_RESPONSE')
            for key in ('id','messageCode'):
                if key in receipt and (not isinstance(receipt[key],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',receipt[key])):
                    receipt.pop(key)
            return receipt
    except urllib.error.HTTPError as exc:
        raise TransportError('HTTP_'+str(exc.code)) from None
    except (urllib.error.URLError,TimeoutError,OSError):
        raise TransportError('NETWORK_OUTCOME_UNKNOWN') from None
    except (ValueError,UnicodeError):
        raise TransportError('UNRECOGNIZED_RESPONSE') from None


class ConnectionTest:
    def __init__(self,store,config):
        self.store=store
        self.url=config.get('TANJA_TRADERSPOST_TEST_WEBHOOK_URL','').strip()
        self.ticker=config.get('TANJA_TEST_CONTRACT','').strip()
        self.fingerprint=hashlib.sha256(self.url.encode()).hexdigest()
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS connection_tests (
                id TEXT PRIMARY KEY, destination TEXT, started REAL, finished REAL,
                status TEXT, error TEXT, payload TEXT, receipt TEXT)''')

    def configured_status(self):
        if not self.url:return 'NOT_CONFIGURED'
        if not re.fullmatch(URL_PATTERN,self.url):return 'INVALID_WEBHOOK_URL'
        if not re.fullmatch(TICKER_PATTERN,self.ticker):return 'EXPLICIT_MNQ_CONTRACT_REQUIRED'
        return 'TEST_READY'

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE connection_tests SET status='unknown',error='PROCESS_INTERRUPTED_NO_RETRY' WHERE status='sending'")

    def run(self,request_id,transport=send_test,clock=time.time):
        if not re.fullmatch(ID_PATTERN,request_id):raise TestBlocked('Invalid test ID')
        if self.configured_status()!='TEST_READY':raise TestBlocked(self.configured_status())
        now=clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM connection_tests WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['destination'] != self.fingerprint:
                    raise TestBlocked('Test ID belongs to another webhook configuration')
                return self.public(old)  # Any outcome is immutable: no network retry.
            if db.execute("SELECT 1 FROM connection_tests WHERE status='sending'").fetchone():raise TestBlocked('A connection test is already running')
            last=db.execute('SELECT MAX(started) FROM connection_tests').fetchone()[0]
            if last is not None and now-last<60:raise TestBlocked('Wait 60 seconds between tests')
            if db.execute('SELECT COUNT(*) FROM connection_tests WHERE started>?',(now-86400,)).fetchone()[0]>=5:
                raise TestBlocked('Five-test limit reached for the last 24 hours')
            payload=test_payload(self.ticker,request_id,now)
            db.execute('INSERT INTO connection_tests(id,destination,started,status,payload) VALUES(?,?,?,?,?)',
                (request_id,self.fingerprint,now,'sending',json.dumps(payload)))
        receipt=None;error=None;status='unknown'
        try:
            receipt=transport(self.url,payload)
            status='test_received' if receipt.get('success') is True else 'test_rejected'
        except TransportError as exc:
            error=str(exc) if re.fullmatch(r'[A-Z_0-9]+',str(exc)) else 'TRANSPORT_ERROR'
        except Exception:
            error='TEST_OUTCOME_UNKNOWN'
        with self.store.connect() as db:
            db.execute('UPDATE connection_tests SET finished=?,status=?,error=?,receipt=? WHERE id=?',
                (clock(),status,error,json.dumps(receipt),request_id))
            return self.public(db.execute('SELECT * FROM connection_tests WHERE id=?',(request_id,)).fetchone())

    @staticmethod
    def public(row):
        record=dict(row);record.pop('destination',None)
        for key in ('payload','receipt'):record[key]=json.loads(record[key] or 'null')
        record.update(test_only=True,broker_fill_verified=False,account_routing_verified=False)
        return record

    def state(self):
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM connection_tests WHERE destination=? ORDER BY started DESC LIMIT 20',(self.fingerprint,)).fetchall()
        records=[self.public(row) for row in rows]
        status=self.configured_status()
        if status=='TEST_READY' and records:status=records[0]['status'].upper()
        return dict(status=status,configured=self.configured_status()=='TEST_READY',
            contract=self.ticker if re.fullmatch(TICKER_PATTERN,self.ticker) else None,
            records=records,test_only=True,orders_enabled=False,broker_connected=False,
            account_routing_verified=False,manual_test_required=True,
            note='A webhook receipt proves signal delivery only. Verify the strategy/account in TradersPost. No broker order is sent by this test.')
