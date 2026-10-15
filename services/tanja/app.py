"""Small WSGI service. Gunicorn handles production HTTP; no trading endpoint."""
import base64
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from store import Store, validate_bar, InvalidBar, Conflict

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self, config, start_worker=False):
        self.token = config.get('TANJA_FEED_TOKEN','')
        self.password = config.get('TANJA_DASHBOARD_PASSWORD','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.token):
            raise ValueError('Set TANJA_FEED_TOKEN to a random 32–128 character URL-safe value')
        if len(self.password) < 16 or self.password == self.token:
            raise ValueError('Set a separate TANJA_DASHBOARD_PASSWORD of at least 16 characters')
        self.tickers = {s:config.get('TANJA_'+s+'_TICKER', 'CME_MINI:'+s+'1!') for s in ('ES','MNQ','NQ')}
        self.data_dir = Path(config.get('DATA_DIR','/data'))
        self.store = Store(self.data_dir,analysis_symbol=config.get('TANJA_ANALYSIS_SYMBOL','NQ'))
        self.store.bind_tickers(self.tickers)
        from market_readiness import MarketReadiness
        self.market_readiness=MarketReadiness(self.store,config,self.tickers)
        self.origin = config.get('TANJA_PARENT_ORIGIN','').rstrip('/')
        if self.origin and (not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?', self.origin)):
            raise ValueError('TANJA_PARENT_ORIGIN must be an HTTPS origin without a path')
        from ai_review import AIReview
        self.ai = AIReview(self.store, config)
        from plan_observer import PlanObserver
        self.plan_observer = PlanObserver(self.store)
        from selection_observer import SelectionObserver
        self.selection_observer = SelectionObserver(self.store, config)
        from broker_feedback import BrokerFeedback
        from trading_loop import TradingLoop
        self.broker_feedback=BrokerFeedback(self.store,config)
        from crosstrade_capture import CrossTradeCapture
        self.crosstrade_capture=CrossTradeCapture(self.broker_feedback.accounting,config)
        if self.broker_feedback.quote_feed.selected:
            quote_token=self.broker_feedback.quote_feed.token
            if quote_token and quote_token in (self.token,self.password,self.broker_feedback.token):
                raise ValueError('Quote feed requires its own separate secret')
        self.trading_loop=TradingLoop(self.store,self.selection_observer,self.broker_feedback,config)
        self.trading_loop.market_readiness=self.market_readiness
        from connection_test import ConnectionTest
        self.connection_test = ConnectionTest(self.store, config)
        from transcript_ingestion import TranscriptIngestion
        self.transcripts = TranscriptIngestion(self.store, config)
        self.csrf = secrets.token_urlsafe(32)
        self.stop = threading.Event()
        if start_worker:
            self.connection_test.recover()
            from worker import run_worker
            threading.Thread(target=run_worker, args=(self.store,self.stop,config), daemon=True).start()
            threading.Thread(target=self.ai.run, args=(self.stop,), daemon=True).start()
            threading.Thread(target=self.plan_observer.run, args=(self.stop,), daemon=True).start()
            threading.Thread(target=self.selection_observer.news.run,args=(self.stop,),daemon=True).start()
            threading.Thread(target=self.selection_observer.run, args=(self.stop,), daemon=True).start()
            threading.Thread(target=self.transcripts.run, args=(self.stop,), daemon=True).start()
            if self.crosstrade_capture.enabled:
                threading.Thread(target=self.crosstrade_capture.run,args=(self.stop,),daemon=True).start()
            if self.trading_loop.mode!='off':threading.Thread(target=self.trading_loop.run,args=(self.stop,),daemon=True).start()

    def __call__(self, env, respond):
        path = env.get('PATH_INFO','/')
        method = env.get('REQUEST_METHOD','GET')
        headers = [('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer')]

        def send(code, body, kind='application/json', extra=()):
            if kind == 'application/json':
                body = json.dumps(body, allow_nan=False)
            raw = body.encode() if isinstance(body,str) else body
            respond(code, headers+[('Content-Type',kind),('Content-Length',str(len(raw)))]+list(extra))
            return [raw]

        if path == '/health' and method == 'GET':
            return send('200 OK', {'ok':True,'service':'tanja','mode':'OBSERVE_ONLY','orders_enabled':False,'version':'3.2-quotes-accounting-observe','analysis_inputs':['ES',self.store.analysis_symbol],'execution_symbol':'MNQ','trading_loop_mode':self.trading_loop.mode,'entry_selection_mode':self.selection_observer.entry_selection_mode})
        if path=='/api/live-quotes':
            quotes=self.broker_feedback.quote_feed
            if not quotes.configured:return send('503 Service Unavailable',{'error':'Separate quote source not configured'})
            supplied=env.get('HTTP_AUTHORIZATION','')
            if not hmac.compare_digest(supplied.encode(),('Bearer '+quotes.token).encode()):return send('401 Unauthorized',{'error':'Quote source authentication required'})
            if method!='POST':return send('405 Method Not Allowed',{'error':'POST required'})
            try:
                length=int(env.get('CONTENT_LENGTH','0'))
                if not 1<=length<=4096:return send('413 Payload Too Large',{'error':'Quote event too large'})
                return send('200 OK',dict(ok=True,**quotes.ingest(json.loads(env['wsgi.input'].read(length)),time.time())))
            except (ValueError,TypeError,KeyError):return send('422 Unprocessable Entity',{'error':'Quote contract, component timestamps or stream sequence invalid'})
            except Exception:return send('503 Service Unavailable',{'error':'Quote storage unavailable; event not acknowledged'})
        if path=='/api/broker-feedback':
            if not self.broker_feedback.configured:return send('503 Service Unavailable',{'error':'Broker bridge not configured'})
            supplied=env.get('HTTP_AUTHORIZATION','')
            if not hmac.compare_digest(supplied.encode(),('Bearer '+self.broker_feedback.token).encode()):return send('401 Unauthorized',{'error':'Broker bridge authentication required'})
            if method!='POST':return send('405 Method Not Allowed',{'error':'POST required'})
            try:
                length=int(env.get('CONTENT_LENGTH','0'))
                if not 1<=length<=2000000:return send('413 Payload Too Large',{'error':'Broker event too large'})
                result=self.broker_feedback.ingest(json.loads(env['wsgi.input'].read(length)),time.time())
                return send('200 OK',dict(ok=True,**result))
            except (ValueError,TypeError,KeyError):return send('422 Unprocessable Entity',{'error':'Broker event schema, freshness, completeness or immutable identity failed'})
        if path.startswith('/feed/'):
            supplied = path[len('/feed/'):]
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return send('404 Not Found', {'error':'not found'})
            if method != 'POST':
                return send('405 Method Not Allowed', {'error':'POST required'})
            try:
                length = int(env.get('CONTENT_LENGTH','0'))
                if length < 1 or length > 4096:
                    return send('413 Payload Too Large', {'error':'Expected a JSON bar, maximum 4096 bytes'})
                b = json.loads(env['wsgi.input'].read(length))
                now = time.time()
                validate_bar(b, self.tickers, now)
                result = self.store.ingest(b, now)
                return send('200 OK', dict(ok=True, **result))
            except Conflict as e:
                self.store.note('conflict','Conflicting duplicate rejected; original retained')
                return send('409 Conflict', {'error':str(e)})
            except (ValueError, TypeError, KeyError) as e:
                message = str(e) if isinstance(e,InvalidBar) else 'Invalid JSON or payload'
                self.store.note('rejected',message)
                return send('422 Unprocessable Entity', {'error':message})
            except Exception:
                return send('503 Service Unavailable', {'error':'Storage unavailable; bar not acknowledged'})
        # No credentials in URLs, JavaScript, CSV exports or iframe links.
        expected = 'Basic '+base64.b64encode(('tanja:'+self.password).encode()).decode()
        if not hmac.compare_digest(env.get('HTTP_AUTHORIZATION','').encode(), expected.encode()):
            return send('401 Unauthorized', {'error':'Sign in with username tanja'},
                        extra=[('WWW-Authenticate','Basic realm="Tanja", charset="UTF-8"')])
        if path == '/api/account-snapshot' and method == 'POST':
            if not hmac.compare_digest(env.get('HTTP_X_TANJA_CSRF','').encode(),self.csrf.encode()):
                return send('403 Forbidden',{'error':'Refresh the dashboard before saving'})
            from manual_account import save_snapshot
            from risk_sizing import risk_budget
            try:
                length=int(env.get('CONTENT_LENGTH','0'))
                if not 1<=length<=512:return send('413 Payload Too Large',{'error':'Account snapshot is too large'})
                snapshot=save_snapshot(self.data_dir,json.loads(env['wsgi.input'].read(length)),time.time())
                self.store.note('account_snapshot','Operator saved a daily manual balance; broker synchronization unavailable')
                return send('200 OK',dict(ok=True,account_snapshot=snapshot,risk_budget_usd=risk_budget(snapshot['balance']),orders_enabled=False))
            except (ValueError,TypeError):
                return send('422 Unprocessable Entity',{'error':'Check account values: positive balance/threshold/target, nonnegative risk amounts, daily allowance at most $1,000, whole contract count'})
            except OSError:
                return send('503 Service Unavailable',{'error':'Balance could not be saved; refresh before retrying'})
        if path == '/api/connection/test' and method == 'POST':
            if not hmac.compare_digest(env.get('HTTP_X_TANJA_CSRF','').encode(), self.csrf.encode()):
                return send('403 Forbidden', {'error':'Refresh the dashboard before testing'})
            from connection_test import TestBlocked
            try:
                length = int(env.get('CONTENT_LENGTH','0'))
                if not 1 <= length <= 256:
                    return send('413 Payload Too Large', {'error':'Expected only a test request ID'})
                body = json.loads(env['wsgi.input'].read(length))
                if not isinstance(body,dict) or set(body) != {'request_id'} or not isinstance(body['request_id'],str):
                    return send('422 Unprocessable Entity', {'error':'Custom signals are not supported; fixed test only'})
                return send('200 OK', self.connection_test.run(body['request_id']))
            except TestBlocked as exc:
                return send('409 Conflict', {'error':str(exc)})
            except (ValueError,TypeError):
                return send('422 Unprocessable Entity', {'error':'Invalid test request'})
            except Exception:
                return send('503 Service Unavailable', {'error':'Test status unavailable; inspect history before retrying'})
        if method != 'GET':
            return send('405 Method Not Allowed', {'error':'Only a fixed no-order connection test is supported'})
        headers.append(('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self' " + self.origin))
        if path == '/api/state':
            now = time.time()
            state = self.store.state(now)
            from data_health import data_health
            state['data_health'] = data_health(state, now)
            state['market_readiness']=self.market_readiness.state(now)
            from data_health import account_snapshot
            state['account_snapshot'] = account_snapshot(self.data_dir)
            from manual_account import snapshot_current
            state['account_snapshot_current'] = snapshot_current(state['account_snapshot'],now)
            state['ai'] = self.ai.state(now, state['latest_context'])
            state['ai_status'] = state['ai']['status']
            state['connection_test'] = self.connection_test.state()
            state['test_csrf'] = self.csrf
            state['plan_observer'] = self.plan_observer.state()
            state['automatic_selection'] = self.selection_observer.state()
            state['trading_loop']=self.trading_loop.state(now)
            state['transcription'] = self.transcripts.state()
            return send('200 OK', state)
        if path in ('/api/transcripts','/api/transcripts/shadow'):
            from urllib.parse import parse_qs
            try:
                after = int(parse_qs(env.get('QUERY_STRING','')).get('after',['0'])[0])
                if after < 0: raise ValueError()
            except (ValueError, TypeError):
                return send('422 Unprocessable Entity', {'error':'Nonnegative transcript cursor required'})
            if path.endswith('/shadow'):
                records = self.transcripts.journal.shadow(after=after)
                return send('200 OK', dict(records=records, next_sequence=records[-1]['event_sequence'] if records else after,
                                         orders_enabled=False, mode='OBSERVE_ONLY'))
            return send('200 OK', self.transcripts.journal.state(after=after))
        if path == '/api/automatic-selection':
            return send('200 OK', self.selection_observer.state())
        if path.startswith('/api/automatic-selection/pine/'):
            match = re.fullmatch(r'/api/automatic-selection/pine/([a-f0-9]{64})/([0-9]{1,12})', path)
            audit = self.selection_observer.audit(match[1], int(match[2])) if match else None
            if not audit:
                return send('404 Not Found', {'error':'Research plan not found'})
            from pine_export import export_plan
            try:
                script = export_plan(audit, self.tickers['MNQ'])
            except (ValueError, TypeError, KeyError):
                return send('409 Conflict', {'error':'A complete valid research plan is required; no trade fills are inferred'})
            return send('200 OK', script, 'text/plain; charset=utf-8',
                        [('Content-Disposition', f'attachment; filename="tanja-plan-{match[2]}.pine"')])
        if path.startswith('/api/automatic-selection/audit/'):
            match = re.fullmatch(r'/api/automatic-selection/audit/([a-f0-9]{64})/([0-9]{1,12})', path)
            audit = self.selection_observer.audit(match[1], int(match[2])) if match else None
            return send('200 OK', audit) if audit else send('404 Not Found', {'error':'not found'})
        if path == '/api/plans':
            return send('200 OK', self.plan_observer.state())
        if path.startswith('/api/ai/audit/'):
            ident = path.rsplit('/',1)[-1]
            audit = self.ai.audit(ident) if re.fullmatch(r'[a-f0-9]{32}', ident) else None
            return send('200 OK', audit) if audit else send('404 Not Found', {'error':'not found'})
        if path in ('/execution-guide','/EXECUTION_SETUP.html'):
            return send('200 OK', (ROOT/'EXECUTION_SETUP.html').read_text(), 'text/html; charset=utf-8')
        if path in ('/ai-guide','/API_SETUP.html'):
            return send('200 OK', (ROOT/'API_SETUP.html').read_text(), 'text/html; charset=utf-8')
        if path in ('/api/bars/ES.csv','/api/bars/MNQ.csv','/api/bars/NQ.csv'):
            symbol=path.split('/')[-1][:-4]
            return send('200 OK',self.store.export_csv(symbol),'text/csv',
                        [('Content-Disposition',f'attachment; filename="tanja_{symbol}_1m.csv"')])
        if path == '/pine':
            return send('200 OK',(ROOT/'tradingview_feed.pine').read_text(),'text/plain; charset=utf-8')
        if path == '/guide':
            return send('200 OK',(ROOT/'SETUP.html').read_text(),'text/html; charset=utf-8')
        files={'/':'index.html','/static/app.js':'app.js','/static/review_visual.js':'review_visual.js','/static/style.css':'style.css','/static/guide.css':'guide.css'}
        if path in files:
            name=files[path]
            kind={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
            return send('200 OK',(ROOT/'static'/name).read_bytes(),kind)
        return send('404 Not Found', {'error':'not found'})


def create_app():
    return App(os.environ, start_worker=True)
