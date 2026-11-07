"""Compatibility-only webhook relay. No market/AI/trading workers."""
import hmac
import json
import os
import re
import urllib.request
import urllib.error
from http import HTTPStatus

TARGET = 'http://agent-50k.railway.internal:8080'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def create_app(config=None, opener=None):
    config = os.environ if config is None else config
    token = config.get('TANJA_FEED_TOKEN', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise ValueError('feed_token_required')
    client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def app(env, start_response):
        def send(code, data):
            body=json.dumps(data,separators=(',',':')).encode()
            start_response(str(code)+' '+HTTPStatus(code).phrase,[('Content-Type','application/json'),('Content-Length',str(len(body))),('Cache-Control','no-store')])
            return [body]
        path=env.get('PATH_INFO','');method=env.get('REQUEST_METHOD','GET')
        if path=='/health' and method=='GET':
            return send(200,dict(ok=True,service='tanja-webhook-relay',mode='RELAY_ONLY',ai=False,broker=False,scanners=False,release=config.get('RAILWAY_GIT_COMMIT_SHA',config.get('RELAY_RELEASE_SHA'))))
        if path=='/' and method=='GET':
            return send(200,dict(service='tanja-webhook-relay',dashboard='https://agent-50k-production.up.railway.app/ict-shadow',note='ES/NQ/MNQ context runs in Agent50k. Existing webhook URLs remain valid.'))
        route=next((r for r in ('/feed/','/reference/') if path.startswith(r)),None)
        if not route or not hmac.compare_digest(path[len(route):],token):return send(404,dict(error='not_found'))
        if method!='POST':return send(405,dict(error='post_required'))
        try:length=int(env.get('CONTENT_LENGTH','0'))
        except ValueError:return send(413,dict(error='invalid_payload_size'))
        if not 1<=length<=12000:return send(413,dict(error='invalid_payload_size'))
        body=env['wsgi.input'].read(length)
        if len(body)!=length:return send(400,dict(error='incomplete_payload'))
        req=urllib.request.Request(TARGET+route+token,data=body,headers={'Content-Type':'application/json'},method='POST')
        try:
            with client.open(req,timeout=8) as response:
                code=response.status;raw=response.read(65537)
        except urllib.error.HTTPError as exc:
            code=exc.code;raw=exc.read(65537)
        except Exception:
            # Never acknowledge data which the destination did not accept.
            # Do not retry an uncertain POST; duplicate handling is downstream.
            return send(503,dict(error='context_destination_unavailable'))
        try:
            if len(raw)>65536:raise ValueError()
            result=json.loads(raw)
            if not isinstance(result,dict):raise ValueError()
        except (ValueError,TypeError):return send(502,dict(error='invalid_destination_response'))
        return send(code,result)
    return app
