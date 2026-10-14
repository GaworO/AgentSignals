"""NQ collection must never masquerade as an MNQ observation or fill."""
import csv
import io
import itertools
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from store import Store, Conflict, validate_bar, InvalidBar
from worker import process_one
from test_service import bar
import test_service as fixtures


class TestNQCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.tickers = {s:'CME_MINI:'+s+'1!' for s in ('ES','MNQ','NQ')}
        self.end = 1779807600

    def tearDown(self):
        self.tmp.cleanup()

    def test_upgrade_preserves_old_binding_and_rejects_rebind(self):
        old = {s:self.tickers[s] for s in ('ES','MNQ')}
        self.s.bind_tickers(old)
        for symbol in old:
            self.s.ingest(bar(symbol,self.end),self.end+1)
        process_one(self.s,self.end+2)
        before = self.s.state(self.end+3)['latest_context']
        self.s.bind_tickers(self.tickers)
        self.s.ingest(bar('NQ',self.end,(200,202,199,201)),self.end+4)
        self.assertEqual(before,self.s.state(self.end+5)['latest_context'])
        # Previous version's binding and two-market table remain compatible.
        self.s.bind_tickers(old)
        with self.s.connect() as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM metadata WHERE key='tickers'").fetchone()[0]),old)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],2)
        for key in ('ES','MNQ','NQ'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.s.bind_tickers(dict(self.tickers,**{key:'CME_MINI:'+key+'Z2026'}))

    def test_all_arrival_orders_queue_only_after_es_and_mnq(self):
        for i,order in enumerate(itertools.permutations(self.tickers)):
            s=Store(self.tmp.name+'/'+str(i));seen=set();queued=0
            for symbol in order:
                result=s.ingest(bar(symbol,self.end),self.end+1)
                seen.add(symbol);queued+=result['queued']
                self.assertEqual(len(s.state(self.end+2)['jobs']),int({'ES','MNQ'}<=seen))
                if symbol=='NQ':self.assertFalse(result['queued'])
            self.assertEqual(queued,1)

    def test_nq_cannot_complete_missing_mnq_pair(self):
        for symbol in ('ES','NQ'):
            self.s.ingest(bar(symbol,self.end),self.end+1)
        self.assertFalse(process_one(self.s,self.end+2))
        self.assertEqual(self.s.state(self.end+2)['jobs'],[])

    def test_nq_outlier_and_future_data_do_not_change_ai_packet(self):
        for symbol in ('ES','MNQ'):
            self.s.ingest(bar(symbol,self.end),self.end+1)
        # Direct fixture insert bypasses live freshness validation intentionally.
        for end in (self.end,self.end+60):
            self.s.ingest(bar('NQ',end,(90000,91000,80000,90001)),end+2)
        process_one(self.s,self.end+70)
        p=self.s.state(self.end+70)['latest_context']['packet']
        with tempfile.TemporaryDirectory() as directory:
            baseline=Store(directory)
            for symbol in ('ES','MNQ'):baseline.ingest(bar(symbol,self.end),self.end+1)
            process_one(baseline,self.end+70)
            self.assertEqual(p,baseline.state(self.end+70)['latest_context']['packet'])
        self.assertEqual(self.s.state(self.end+70)['market_roles']['fidelity_status'],'ANALYSIS_SOURCE_MISMATCH')

    def test_nq_retry_conflict_restart_and_export_are_isolated(self):
        b=bar('NQ',self.end,(200,202,199,201))
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.s.ingest(b,self.end+1),range(8)))
        self.assertEqual(sum(r['status']=='stored' for r in results),1)
        with self.assertRaises(Conflict):self.s.ingest(dict(b,volume=99),self.end+2)
        s=Store(self.tmp.name)
        self.assertEqual(s.history('NQ',self.end,self.end+1),[b])
        self.assertEqual(s.history('MNQ',self.end,self.end+1),[])
        self.assertEqual(s.history('NQ',self.end,self.end),[])
        exported=list(csv.DictReader(io.StringIO(s.export_csv('NQ'))))
        self.assertEqual(exported[0]['symbol'],'NQ');self.assertEqual(exported[0]['open'],'200')
        self.assertEqual(s.state(self.end+3)['feeds']['NQ']['count'],1)

    def test_live_validation_requires_actual_nq_ticker_and_closed_bar(self):
        b=bar('NQ',self.end)
        validate_bar(b,self.tickers,self.end+1)
        for updates in ({'ticker':self.tickers['MNQ']},{'confirmed':False},{'bar_close_ms':(self.end+60)*1000}):
            with self.assertRaises(InvalidBar):validate_bar(dict(b,**updates),self.tickers,self.end+1)

    def test_authenticated_http_feed_and_export(self):
        http=fixtures.TestService()
        http.setUp()
        try:
            result=http.request('/feed/'+http.config['TANJA_FEED_TOKEN'],'POST',bar('NQ',http.end))
            self.assertTrue(result['status'].startswith('200'))
            self.assertFalse(json.loads(result['body'])['queued'])
            self.assertTrue(http.request('/api/bars/NQ.csv')['status'].startswith('401'))
            result=http.request('/api/bars/NQ.csv',auth=True)
            self.assertTrue(result['status'].startswith('200'))
            self.assertEqual(list(csv.DictReader(io.StringIO(result['body'])))[0]['symbol'],'NQ')
        finally:
            http.tearDown()
