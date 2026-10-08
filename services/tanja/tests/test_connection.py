import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App
from connection_test import ConnectionTest,TestBlocked,TransportError,test_payload,send_test
from store import Store

URL='https://webhooks.traderspost.io/trading/webhook/00000000-0000-0000-0000-000000000000/test_password_only'


class TestConnection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.config=dict(TANJA_TRADERSPOST_TEST_WEBHOOK_URL=URL,TANJA_TEST_CONTRACT='MNQZ2026')
        self.test=ConnectionTest(self.store,self.config)
        self.sent=[]
    def tearDown(self):self.temp.cleanup()
    def transport(self,url,payload):
        self.sent.append(payload)
        self.assertEqual(url,URL);self.assertIs(payload['test'],True)
        return {'success':True,'id':'test-signal-id'}

    def test_default_missing_invalid_and_exact_contract(self):
        self.assertEqual(ConnectionTest(self.store,{}).state()['status'],'NOT_CONFIGURED')
        for url in ['http://webhooks.traderspost.io/trading/webhook/a/b',URL+'?x=y',URL.replace('webhooks.traderspost.io','localhost'),URL.replace('https://','https://user:pass@')]:
            c=ConnectionTest(self.store,dict(self.config,TANJA_TRADERSPOST_TEST_WEBHOOK_URL=url))
            self.assertEqual(c.state()['status'],'INVALID_WEBHOOK_URL')
            with self.assertRaises(TestBlocked):c.run('a'*32,self.transport,lambda:1000)
        for ticker in ['MNQ1!','NQZ2026','MNQZ26','CME_MINI:MNQZ2026']:
            c=ConnectionTest(self.store,dict(self.config,TANJA_TEST_CONTRACT=ticker))
            self.assertEqual(c.state()['status'],'EXPLICIT_MNQ_CONTRACT_REQUIRED')
        self.assertFalse(self.sent)

    def test_ack_is_not_broker_fill_and_never_stores_url(self):
        r=self.test.run('a'*32,self.transport,lambda:1000)
        self.assertEqual(r['status'],'test_received')
        self.assertFalse(r['broker_fill_verified']);self.assertFalse(r['account_routing_verified'])
        self.assertEqual(r['payload']['quantity'],1);self.assertIs(r['payload']['test'],True)
        self.assertNotIn('test_password_only',json.dumps(self.test.state()))
        with self.store.connect() as db:
            self.assertNotIn('test_password_only',repr([tuple(r) for r in db.execute('SELECT * FROM connection_tests')]))
        self.assertFalse(self.test.state()['orders_enabled'])

    def test_same_request_id_cannot_send_twice_even_concurrently(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.test.run('a'*32,self.transport,lambda:1000),range(4)))
        self.assertEqual(len(self.sent),1)
        self.test.run('a'*32,self.transport,lambda:1100)
        self.assertEqual(len(self.sent),1)

    def test_unknown_result_not_retried_after_restart(self):
        def fail(*args):raise TransportError('NETWORK_OUTCOME_UNKNOWN')
        r=self.test.run('a'*32,fail,lambda:1000)
        self.assertEqual(r['status'],'unknown')
        restarted=ConnectionTest(self.store,self.config);restarted.recover()
        restarted.run('a'*32,self.transport,lambda:1200)
        self.assertFalse(self.sent)
        with self.store.connect() as db:db.execute("UPDATE connection_tests SET status='sending'")
        restarted.recover()
        self.assertEqual(restarted.state()['records'][0]['error'],'PROCESS_INTERRUPTED_NO_RETRY')

    def test_limits_and_changed_destination(self):
        for i in range(5):self.test.run(format(i,'032x'),self.transport,lambda i=i:1000+i*61)
        with self.assertRaises(TestBlocked):self.test.run('f'*32,self.transport,lambda:1305)
        self.test.run('f'*32,self.transport,lambda:90000)
        changed=ConnectionTest(self.store,dict(self.config,TANJA_TRADERSPOST_TEST_WEBHOOK_URL=URL+'changed'))
        self.assertEqual(changed.state()['records'],[])
        with self.assertRaises(TestBlocked):changed.run('f'*32,self.transport,lambda:91000)

    def test_boundary_forbids_non_test_and_redacts_provider_response(self):
        p=test_payload('MNQZ2026','a'*32,1000)
        for change in [{'test':False},{'test':1},{'quantity':2},{'action':'exit'},{'ticker':'ESZ2026'}]:
            with self.assertRaises(TestBlocked):send_test(URL,dict(p,**change))
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'success':True,'id':'ok-id','message':URL,'redirect':URL}).encode()
        with patch('connection_test.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            r=send_test(URL,p)
            request=opener.return_value.open.call_args.args[0]
            self.assertIs(json.loads(request.data)['test'],True)
            self.assertEqual(r,{'success':True,'id':'ok-id'})

    def test_http_errors_do_not_leak_secret_url(self):
        import urllib.error
        with patch('connection_test.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError(URL,403,URL,{},io.BytesIO(URL.encode()))
            with self.assertRaisesRegex(TransportError,'^HTTP_403$'):
                send_test(URL,test_payload('MNQZ2026','a'*32,1000))

    def test_wsgi_auth_csrf_custom_payload_rejected_and_valid_test(self):
        cfg=dict(self.config,DATA_DIR=self.temp.name,TANJA_FEED_TOKEN='a'*40,TANJA_DASHBOARD_PASSWORD='local_test_password_1234')
        app=App(cfg)
        def request(body,auth=True,csrf=True):
            raw=json.dumps(body).encode();env={'PATH_INFO':'/api/connection/test','REQUEST_METHOD':'POST','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
            if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:local_test_password_1234').decode()
            if csrf:env['HTTP_X_TANJA_CSRF']=app.csrf
            statuses=[];out=b''.join(app(env,lambda s,h:statuses.append(s)))
            return statuses[0],json.loads(out)
        body={'request_id':'a'*32}
        self.assertTrue(request(body,auth=False)[0].startswith('401'))
        self.assertTrue(request(body,csrf=False)[0].startswith('403'))
        self.assertTrue(request(dict(body,test=False))[0].startswith('422'))
        self.assertTrue(request(dict(body,quantity=10))[0].startswith('422'))
        original=app.connection_test.run
        with patch.object(app.connection_test,'run',side_effect=lambda ident:original(ident,self.transport,lambda:1000)):
            status,result=request(body)
            self.assertTrue(status.startswith('200'));self.assertEqual(result['status'],'test_received')
        self.assertEqual(len(self.sent),1)
