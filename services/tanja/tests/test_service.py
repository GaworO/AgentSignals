import base64
import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'integration'))
from app import App
from store import Store, validate_bar, InvalidBar, Conflict
from worker import process_one
from tanja_menu import install as menu
from install_menu import install as patch_menu


def bar(symbol='MNQ', end=None, prices=(100,102,99,101)):
    end=end or int(time.time()//60)*60
    return dict(schema_version=1,feed_version='tanja-1m-v1',symbol=symbol,ticker='CME_MINI:'+symbol+'1!',
        timeframe='1',session='extended',standard=True,confirmed=True,
        bar_open_ms=(end-60)*1000,bar_close_ms=end*1000,sent_at_ms=end*1000,
        open=prices[0],high=prices[1],low=prices[2],close=prices[3],volume=100)


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config=dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='test_'+'a'*40,
                         TANJA_DASHBOARD_PASSWORD='test_dashboard_password')
        self.app=App(self.config)
        self.end=int(time.time()//60)*60

    def tearDown(self):self.tmp.cleanup()

    def request(self,path,method='GET',data=None,auth=False):
        raw=json.dumps(data).encode() if data is not None else b''
        env={'PATH_INFO':path,'REQUEST_METHOD':method,'CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(('tanja:'+self.config['TANJA_DASHBOARD_PASSWORD']).encode()).decode()
        result={}
        def start(status,headers):result.update(status=status,headers=dict(headers))
        result['body']=b''.join(self.app(env,start)).decode()
        return result

    def test_closed_payload(self):
        self.assertEqual(validate_bar(bar(end=self.end),self.app.tickers,self.end+1)['symbol'],'MNQ')

    def test_cme_regular_means_eth(self):
        validate_bar(dict(bar(end=self.end),session='regular'),self.app.tickers,self.end+1)

    def test_rejects_bad_payloads(self):
        cases=[{'confirmed':False},{'standard':False},{'session':'us_regular'},{'timeframe':'5'},
               {'ticker':'CME_MINI:NQ1!'},{'symbol':'NQ'},{'close':float('nan')},
               {'high':98},{'low':103},{'volume':-1},{'open':100.1},{'close':True},
               {'schema_version':True},{'bar_open_ms':self.end*1000},
               {'bar_close_ms':(self.end+60)*1000},{'sent_at_ms':(self.end-1)*1000}]
        for change in cases:
            with self.subTest(change=change),self.assertRaises(InvalidBar):
                validate_bar(dict(bar(end=self.end),**change),self.app.tickers,self.end+1)
        with self.assertRaises(InvalidBar):validate_bar(bar(end=self.end-240),self.app.tickers,self.end+1)

    def test_auth_and_no_order_route(self):
        self.assertTrue(self.request('/health')['status'].startswith('200'))
        self.assertTrue(self.request('/api/state')['status'].startswith('401'))
        self.assertTrue(self.request('/api/state',auth=True)['status'].startswith('200'))
        self.assertTrue(self.request('/orders','POST',{},True)['status'].startswith('405'))
        self.assertTrue(self.request('/feed/wrong','POST',bar())['status'].startswith('404'))

    def test_storage_before_ack_no_inline_analysis(self):
        path='/feed/'+self.config['TANJA_FEED_TOKEN']
        for s in ('ES','MNQ'):
            self.assertTrue(self.request(path,'POST',bar(s))['status'].startswith('200'))
        state=self.app.store.state(time.time())
        self.assertEqual(state['jobs'][0]['status'],'queued')
        self.assertIsNone(state['latest_context'])
        self.assertEqual(state['feeds']['ES']['count'],1)

    def test_duplicates_and_conflicts(self):
        b=bar(end=self.end);s=self.app.store
        s.ingest(b,self.end+1)
        self.assertEqual(s.ingest(dict(b,sent_at_ms=(self.end+1)*1000),self.end+2)['status'],'duplicate')
        with self.assertRaises(Conflict):s.ingest(dict(b,volume=101),self.end+2)
        self.assertEqual(s.state(self.end+3)['feeds']['MNQ']['count'],1)

    def test_concurrent_duplicates_one_job(self):
        s=self.app.store
        def push(n):return s.ingest(bar('MNQ' if n%2 else 'ES',self.end),self.end+1)
        with ThreadPoolExecutor(4) as pool: list(pool.map(push,range(20)))
        state=s.state(self.end+1)
        self.assertEqual(len(state['jobs']),1)
        self.assertEqual(state['feeds']['ES']['count'],1)

    def test_symbol_isolation_and_csv(self):
        s=self.app.store
        s.ingest(bar('ES',self.end,(7000,7002,6999,7001)),self.end+1)
        s.ingest(bar('MNQ',self.end,(30000,30002,29999,30001)),self.end+2)
        es=list(csv.DictReader(io.StringIO(s.export_csv('ES'))))
        mnq=list(csv.DictReader(io.StringIO(s.export_csv('MNQ'))))
        self.assertEqual(es[0]['open'],'7000')
        self.assertEqual(mnq[0]['open'],'30000')
        self.assertEqual(es[0]['symbol'],'ES')

    def test_out_of_order_and_frozen_arrivals(self):
        s=self.app.store
        for symbol in ('ES','MNQ'):s.ingest(bar(symbol,self.end),self.end+1)
        for symbol in ('ES','MNQ'):s.ingest(bar(symbol,self.end-60),self.end+2)
        self.assertEqual(len(s.state(self.end+3)['jobs']),1)
        self.assertEqual(len(s.history('ES',self.end,self.end+1)),1)
        process_one(s,self.end+3)
        self.assertEqual(s.state(self.end+3)['latest_context']['packet']['coverage']['ES']['closed_minutes'],1)

    def test_restart_recovers_jobs(self):
        s=self.app.store
        for symbol in ('ES','MNQ'):s.ingest(bar(symbol,self.end),self.end+1)
        s.claim_job()
        reopened=Store(self.tmp.name);reopened.recover()
        self.assertTrue(process_one(reopened,self.end+3))
        self.assertEqual(reopened.state(self.end+3)['jobs'][0]['status'],'done')

    def test_causal_inversion_observation_not_trade(self):
        s=self.app.store
        # FVG up, then close strictly below its lower edge.
        for i,prices in enumerate([(100,101,99,100),(102,104,102,103),(104,106,103,105),(100,101,98,99)]):
            end=self.end-180+i*60
            for symbol in ('ES','MNQ'):s.ingest(bar(symbol,end,prices),end+1)
            process_one(s,end+2)
        state=s.state(self.end+3)
        c=[c for c in state['candidates'] if c['timeframe']==1]
        self.assertEqual(len(c),1);self.assertEqual(c[0]['direction'],'short')
        self.assertFalse(c[0]['executable']);self.assertEqual(state['executions'],[])
        self.assertEqual(state['ai_status'],'NOT_CONNECTED')

    def test_gaps_do_not_invent_candles(self):
        s=self.app.store
        for end in (self.end-120,self.end):
            for symbol in ('ES','MNQ'):s.ingest(bar(symbol,end),end+1)
            process_one(s,end+2)
        context=s.state(self.end+3)['latest_context']
        self.assertEqual(context['coverage']['ES']['1'],2)
        self.assertEqual(len(context['gaps']['ES']),1)
        self.assertEqual(context['coverage']['ES']['5'],0)

    def test_future_bars_cannot_change_queued_packet(self):
        s=self.app.store
        for symbol in ('ES','MNQ'):s.ingest(bar(symbol,self.end),self.end+1)
        # Store future fixture directly; real HTTP intake rejects it.
        for symbol in ('ES','MNQ'):s.ingest(bar(symbol,self.end+60,(200,300,199,250)),self.end+61)
        process_one(s,self.end+100)
        p=s.state(self.end+100)['latest_context']['packet']
        self.assertEqual(p['as_of'],self.end)
        self.assertTrue(all(e['available_at']<=self.end for e in p['evidence'].values()))

    def test_public_health_and_dashboard_do_not_leak_secrets(self):
        state=self.request('/api/state',auth=True)['body']
        self.assertNotIn(self.config['TANJA_FEED_TOKEN'],state)
        self.assertNotIn(self.config['TANJA_DASHBOARD_PASSWORD'],state)
        self.assertNotIn('api/state',self.request('/health')['body'])

    def test_changed_ticker_cannot_mix_existing_series(self):
        with self.assertRaises(ValueError):
            App(dict(self.config,TANJA_ES_TICKER='CME_MINI:ESZ2026'))

    def test_invalid_config(self):
        for change in ({'TANJA_FEED_TOKEN':'short'},{'TANJA_DASHBOARD_PASSWORD':'short'},
                       {'TANJA_PARENT_ORIGIN':'https://example.com/path'}):
            with self.assertRaises(ValueError):App(dict(self.config,**change))

    def test_menu_validation_and_idempotency(self):
        d=types.SimpleNamespace(PAGE="var STRAT={}; var NAV=[]; var frame=document.getElementById('frame');")
        self.assertTrue(menu(d,'https://tanja.example.com'))
        first=d.PAGE;menu(d,'https://tanja.example.com');self.assertEqual(first,d.PAGE)
        self.assertIn("['context','AI context'",first)
        with self.assertRaises(ValueError):menu(d,'https://x.com/\"</script>')
        with self.assertRaises(ValueError):menu(types.SimpleNamespace(PAGE='unknown'),'https://x.com')

    def test_installer_preserves_existing_source(self):
        root=Path(self.tmp.name)
        text='import dashboard\ndashboard.register(app) # existing route\nother_code = 1\n'
        (root/'agent.py').write_text(text)
        patch_menu(root)
        self.assertEqual((root/'agent.py.before-tanja').read_text(),text)
        self.assertIn('other_code = 1',(root/'agent.py').read_text())
        self.assertEqual(patch_menu(root),'Already installed')


if __name__=='__main__':unittest.main()
