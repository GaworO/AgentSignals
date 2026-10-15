"""GET-only CrossTrade capture. Never certifies coverage, costs or prop limits."""
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from execution_ledger import amount,timestamp


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class CrossTradeReader:
    def __init__(self,token):
        if not isinstance(token,str) or not token or any(ord(c)<33 or ord(c)>126 for c in token):
            raise ValueError('CROSSTRADE_API_TOKEN_REQUIRED')
        self.token=token
        self.opener=build_opener(NoRedirect())

    def get(self,path,params=None):
        if path not in ('/fills','/fills/history'): raise ValueError('READ_ROUTE_NOT_ALLOWED')
        url='https://app.crosstrade.io/v1/api/tv'+path
        if params: url+='?'+urlencode(params)
        request=Request(url,headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'},method='GET')
        try:
            with self.opener.open(request,timeout=15) as response:
                raw=response.read(8_000_001)
                if len(raw)>8_000_000: raise ValueError('CROSSTRADE_RESPONSE_TOO_LARGE')
                payload=json.loads(raw)
        except HTTPError as exc:
            # Never include URL, response body, credentials or account identifiers.
            raise ValueError('CROSSTRADE_RATE_LIMITED' if exc.code==429 else 'CROSSTRADE_HTTP_'+str(exc.code)) from None
        except (URLError,TimeoutError,OSError): raise ValueError('CROSSTRADE_CONNECTION_FAILED') from None
        if not isinstance(payload,dict) or payload.get('success') is not True: raise ValueError('CROSSTRADE_UPSTREAM_FAILED')
        return payload

    def history(self,start,end,account,environment):
        params={'from':start,'to':end,'account':account,'environment':environment,'limit':1000}
        result=[];cursors=set()
        for _ in range(100):
            payload=self.get('/fills/history',params)
            if not isinstance(payload.get('data'),list) or 'nextCursor' not in payload: raise ValueError('HISTORY_PAGE_SCHEMA_INVALID')
            result.extend(payload['data'])
            cursor=payload['nextCursor']
            if cursor is None: return result
            if not isinstance(cursor,str) or not cursor or cursor in cursors: raise ValueError('HISTORY_CURSOR_INVALID')
            cursors.add(cursor);params['cursor']=cursor
        raise ValueError('HISTORY_PAGE_LIMIT_REACHED')


class CrossTradeCapture:
    def __init__(self,ledger,config,reader=None):
        self.ledger=ledger
        self.enabled=config.get('TANJA_CROSSTRADE_CAPTURE_ENABLED','false').lower()=='true'
        self.account_name=config.get('TANJA_CROSSTRADE_ACCOUNT_NAME','')
        self.interval=max(15,float(config.get('TANJA_CROSSTRADE_CAPTURE_INTERVAL','30')))
        self.lookback=max(2,int(config.get('TANJA_CROSSTRADE_LOOKBACK_DAYS','7')))
        self.reader=reader
        if self.enabled:
            if not ledger.selected or not ledger.configured or not self.account_name: raise ValueError('CROSSTRADE_ACCOUNT_CONFIGURATION_REQUIRED')
            if self.reader is None: self.reader=CrossTradeReader(config.get('TANJA_CROSSTRADE_API_TOKEN',''))

    def poll(self,now=None):
        if not self.enabled: return False
        now=time.time() if now is None else now
        iso=lambda dt:dt.isoformat().replace('+00:00','Z')
        end=datetime.fromtimestamp(now,timezone.utc)
        try:
            # Always overlap: neither pagination nor IDs reveal fee revisions.
            rows=self.reader.history(iso(end-timedelta(days=self.lookback)),iso(end),self.account_name,self.ledger.environment)
            self.ledger.ingest(rows,now)
            session=self.reader.get('/fills')
            self.ledger.capture_session(session,now)
            if not isinstance(session.get('data'),list): raise ValueError('SESSION_SCHEMA_INVALID')
            known={r['execution_id']:r for r in self.ledger.records()}
            unresolved=0
            for identity in session['data']:
                if not isinstance(identity,dict) or not isinstance(identity.get('data'),list): raise ValueError('SESSION_IDENTITY_FAILED')
                if identity.get('environment')!=self.ledger.environment: continue
                for fill in identity['data']:
                    if not isinstance(fill,dict) or type(fill.get('id')) is not int: raise ValueError('SESSION_FILL_SCHEMA_INVALID')
                    eid='tv:'+self.ledger.environment+':'+str(fill['id'])
                    if 'accountId' in fill and str(fill['accountId'])!=self.ledger.account:
                        if eid in known:raise ValueError('SESSION_ACCOUNT_CONFLICT')
                        continue
                    # Identity-wide rows without account IDs cannot be silently
                    # assigned to this account. History can resolve ownership.
                    if eid not in known:unresolved+=1
                    else:
                        core=known[eid];spec=self.ledger.specs.get(fill.get('instrument'))
                        if not spec:raise ValueError('SESSION_CONTRACT_ALIAS_UNMAPPED')
                        if (type(fill.get('qty')) is not int or fill.get('action') not in ('Buy','Sell')
                            or fill['qty']*(1 if fill['action']=='Buy' else -1)!=core['quantity']
                            or amount(fill.get('price'))!=amount(core['price'])
                            or timestamp(fill.get('timestamp'))!=core['at'] or spec['contract']!=core['contract']):
                            raise ValueError('SESSION_CORE_CONFLICT')
            self.ledger.note_sync(now,'CAPTURED' if not unresolved else 'SESSION_OWNERSHIP_OR_CAPTURE_PENDING',{'unresolved_session_fills':unresolved})
            return not unresolved
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            code=str(exc) if re.fullmatch(r'[A-Z_0-9]{1,80}',str(exc)) else 'CAPTURE_SCHEMA_OR_VALUE_INVALID'
            self.ledger.note_sync(now,'CAPTURE_FAILED',{'reason':code})
            return False

    def run(self,stop):
        while not stop.is_set():
            try: self.poll()
            except Exception: self.ledger.note_sync(time.time(),'CAPTURE_FAILED')
            stop.wait(self.interval)
