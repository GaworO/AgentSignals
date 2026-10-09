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
        self.store = Store(config.get('DATA_DIR','/data'))
        self.store.bind_tickers(self.tickers)
        self.origin = config.get('TANJA_PARENT_ORIGIN','').rstrip('/')
        if self.origin and (not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?', self.origin)):
            raise ValueError('TANJA_PARENT_ORIGIN must be an HTTPS origin without a path')
        from ai_review import AIReview
        self.ai = AIReview(self.store, config)
        from plan_observer import PlanObserver
        self.plan_observer = PlanObserver(self.store)
        from connection_test import ConnectionTest
        self.connection_test = ConnectionTest(self.store, config)
        self.csrf = secrets.token_urlsafe(32)
        self.stop = threading.Event()
        if start_worker:
            self.connection_test.recover()
            from worker import run_worker
            threading.Thread(target=run_worker, args=(self.store,self.stop), daemon=True).start()
            threading.Thread(target=self.ai.run, args=(self.stop,), daemon=True).start()
            threading.Thread(target=self.plan_observer.run, args=(self.stop,), daemon=True).start()

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
            return send('200 OK', {'ok':True,'service':'tanja','mode':'OBSERVE_ONLY','orders_enabled':False,'version':'2.3-nq-collection'})
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
            state['ai'] = self.ai.state(now, state['latest_context'])
            state['ai_status'] = state['ai']['status']
            state['connection_test'] = self.connection_test.state()
            state['test_csrf'] = self.csrf
            state['plan_observer'] = self.plan_observer.state()
            return send('200 OK', state)
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
        files={'/':'index.html','/static/app.js':'app.js','/static/style.css':'style.css','/static/guide.css':'guide.css'}
        if path in files:
            name=files[path]
            kind={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
            return send('200 OK',(ROOT/'static'/name).read_bytes(),kind)
        return send('404 Not Found', {'error':'not found'})


def create_app():
    return App(os.environ, start_worker=True)
