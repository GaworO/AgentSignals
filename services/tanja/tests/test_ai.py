import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App
from ai_review import AIReview, APIError, call_openai, encoded
from store import Store
from worker import build_v3_packet, Bar
from context_v3 import unknown_decision
from test_service import bar


class TestAI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cutoff=int(datetime(2026,10,8,13,31,tzinfo=timezone.utc).timestamp())
        # Synthetic market candles; no broker data, paid API calls or performance claims.
        bs=[Bar(t,100,102,99,101) for t in range(cls.cutoff-16*3600,cls.cutoff,60)]
        cls.packet=build_v3_packet({'ES':bs,'MNQ':bs},cls.cutoff)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.now=self.cutoff+2
        self.config=dict(OPENAI_API_KEY='unit_test_secret_never_real',OPENAI_MODEL='gpt-6.1-sol',TANJA_AI_ENABLED='true')
        self.context=dict(packet=copy.deepcopy(self.packet),frozen_at=self.cutoff+1,processed_at=self.cutoff+1,
            coverage={s:self.packet['coverage'][s]['retained_bars_per_timeframe'] for s in ('ES','MNQ')})
        with self.store.connect() as db:
            for s in ('ES','MNQ'):
                for end in range(self.cutoff-14*60,self.cutoff+1,60):
                    db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',(s,end-60,end,end+.5,encoded(bar(s,end))))
            db.execute("INSERT INTO jobs(cutoff,frozen_at,status,packet,processed_at) VALUES(?,?,'done',?,?)",(self.cutoff,self.cutoff+1,encoded(self.context),self.cutoff+1))
        self.ai=AIReview(self.store,self.config)

    def tearDown(self):self.temp.cleanup()

    def response(self,payload,key):
        self.assertEqual(key,self.config['OPENAI_API_KEY'])
        p=json.loads(payload['input'][1]['content'])
        d=unknown_decision(p)
        d['rationale']='Synthetic API fixture: abstain, incomplete context.'
        return dict(status='completed',model='gpt-6.1-sol',id='mock-response',
            usage={'input_tokens':100,'output_tokens':50},output=[dict(type='message',content=[dict(type='output_text',text=encoded(d))])])

    def test_disabled_missing_config_no_network(self):
        for cfg,status in [({},'DISABLED'),({'TANJA_AI_ENABLED':'true'},'MISSING_API_KEY'),
                           (dict(self.config,OPENAI_MODEL=''),'MISSING_MODEL'),
                           (dict(self.config,TANJA_AI_MAX_CALLS_PER_DAY='500'),'CONFIG_ERROR')]:
            a=AIReview(self.store,cfg)
            self.assertEqual(a.state(self.now,self.context)['status'],status)
            self.assertFalse(a.process_one(lambda *a:self.fail('Network called'),lambda:self.now))

    def test_warmup_freshness_window_and_gap_gates(self):
        self.assertEqual(self.ai.gate(self.now,self.context),'READY')
        self.assertEqual(self.ai.gate(self.now+180,self.context),'WAITING_FOR_FRESH_DATA')
        self.assertEqual(self.ai.gate(self.cutoff-.1,self.context),'WAITING_FOR_FRESH_DATA')
        c=copy.deepcopy(self.context);c['coverage']['ES']['240']=2
        self.assertEqual(self.ai.gate(self.now,c),'WAITING_FOR_HISTORY')
        with self.store.connect() as db:db.execute("DELETE FROM bars WHERE symbol='ES' AND end=?",(self.cutoff-300,))
        self.assertEqual(self.ai.gate(self.now,self.context),'WAITING_FOR_CONTIGUOUS_DATA')

    def test_success_is_frozen_and_available_only_after_response(self):
        ticks=iter([self.now,self.now+30])
        self.assertTrue(self.ai.process_one(self.response,lambda:next(ticks)))
        r=self.ai.state(self.now+31,self.context)['records'][0]
        self.assertEqual(r['status'],'validated');self.assertEqual(r['available_at'],self.now+30)
        self.assertFalse(r['executable']);self.assertFalse(r['review']['executable'])
        audit=self.ai.audit(r['id'])
        self.assertEqual(json.loads(audit['request']['input'][1]['content']),self.packet)
        self.assertEqual(audit['snapshot'],self.context)
        self.assertNotIn(self.config['OPENAI_API_KEY'],encoded(audit))
        self.assertFalse(audit['request']['store'])
        self.assertEqual(audit['request']['text']['format']['schema']['properties']['schema_version']['enum'],[3])
        self.assertFalse(self.ai.process_one(lambda *a:self.fail('Repeated API call'),lambda:self.now+60))
        self.assertEqual(self.ai.state(self.now+31,self.context)['calls_today'],1)

    def test_rejects_forged_evidence(self):
        def forged(payload,key):
            raw=self.response(payload,key)
            d=json.loads(raw['output'][0]['content'][0]['text']);d['poi_evidence_ids']=['future_unknown_anchor']
            raw['output'][0]['content'][0]['text']=encoded(d);return raw
        self.ai.process_one(forged,lambda:self.now)
        r=self.ai.state(self.now,self.context)
        self.assertEqual(r['records'][0]['status'],'rejected')
        self.assertEqual(r['status'],'PAUSED_AFTER_ERROR')
        self.assertFalse(AIReview(self.store,self.config).process_one(self.response,lambda:self.now))

    def test_incomplete_refusal_and_timeout_are_not_decisions(self):
        for name,response in [('incomplete',{'status':'incomplete'}),('refusal',{'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]}),('timeout',None),('malformed',['unexpected'])]:
            with self.subTest(name=name):
                with self.store.connect() as db:db.execute('DELETE FROM ai_runs')
                def transport(*args):
                    if response is None:raise APIError('NETWORK_OUTCOME_UNKNOWN')
                    return response
                self.ai.process_one(transport,lambda:self.now)
                state=self.ai.state(self.now,self.context)
                self.assertEqual(state['status'],'PAUSED_AFTER_ERROR')
                self.assertIsNone(state['records'][0]['available_at'])
                self.assertEqual(state['calls_today'],1)

    def test_atomic_reservation_restart_and_daily_limit(self):
        def reserve(_):return self.ai.reserve(self.context,self.ai.make_payload(self.packet),self.now)
        with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(reserve,range(4)))
        self.assertEqual(sum(i is not None for i in ids),1)
        self.ai.recover()
        self.assertEqual(self.ai.state(self.now,self.context)['status'],'PAUSED_AFTER_ERROR')
        other=AIReview(self.store,dict(self.config,TANJA_AI_REVISION='2',TANJA_AI_MAX_CALLS_PER_DAY='1'))
        self.assertEqual(other.state(self.now,self.context)['status'],'DAILY_LIMIT_REACHED')
        self.assertEqual(other.state(self.now,self.context)['calls_today'],1)

    def test_window_and_daily_reset_use_new_york(self):
        with self.store.connect() as db:
            # Gate window independently; all data gates tested above.
            with patch('ai_review.datetime') as dates:
                dates.fromtimestamp.return_value=datetime(2026,10,8,4,31)
                self.assertEqual(self.ai.gate(self.now,self.context),'OUTSIDE_REVIEW_WINDOW')
        self.ai.reserve(self.context,self.ai.make_payload(self.packet),self.now)
        with self.store.connect() as db:
            db.execute("UPDATE ai_runs SET status='validated'")
            self.assertEqual(self.ai.limits(db,self.now+86400),'READY')

    def test_input_limit_and_authenticated_audit(self):
        with patch('ai_review.MAX_REQUEST_BYTES',10):
            self.assertEqual(self.ai.gate(self.now,self.context),'INPUT_TOO_LARGE')
            self.assertFalse(self.ai.process_one(lambda *a:self.fail('Network called'),lambda:self.now))
        self.ai.process_one(self.response,lambda:self.now)
        ident=self.ai.state(self.now,self.context)['records'][0]['id']
        cfg=dict(self.config,DATA_DIR=self.temp.name,TANJA_FEED_TOKEN='x'*40,TANJA_DASHBOARD_PASSWORD='test_password_1234')
        app=App(cfg)
        for auth,expected in [(False,'401'),(True,'200')]:
            env={'PATH_INFO':'/api/ai/audit/'+ident,'REQUEST_METHOD':'GET','wsgi.input':io.BytesIO()}
            if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test_password_1234').decode()
            statuses=[];body=b''.join(app(env,lambda s,h:statuses.append(s)))
            self.assertTrue(statuses[0].startswith(expected))
            self.assertNotIn(self.config['OPENAI_API_KEY'].encode(),body)

    def test_http_error_does_not_store_body_or_secret(self):
        import urllib.error
        error=urllib.error.HTTPError('https://api.openai.com/v1/responses',401,'secret message',{},io.BytesIO(b'secret'))
        with patch('ai_review.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaisesRegex(APIError,'^HTTP_401$'):
                call_openai({},'secret')
