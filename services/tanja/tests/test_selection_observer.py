import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from auto_fixture import fixture
from selection_observer import SelectionObserver
from store import Store


class SelectionObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.balance_file=Path(self.tmp.name)/'account_snapshot.json'
        self.balance_file.write_text(json.dumps(dict(balance=50000,minimum_balance=48000,target_balance=53000,as_of='test snapshot')))
        self.s=Store(self.tmp.name);self.m,self.t=fixture()
        with self.s.connect() as db:
            for sym,rows in self.m.items():
                table='collection_bars' if sym=='NQ' else 'bars'
                for b in rows:
                    payload=dict(symbol=sym,bar_open_ms=b['time']*1000,bar_close_ms=(b['time']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                    db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',(sym,b['time'],b['time']+60,b['time']+60,json.dumps(payload)))
        self.o=SelectionObserver(self.s,{})

    def test_saved_automatic_plan_and_restart_dedup(self):
        self.assertTrue(self.o.process_one(self.t+1))
        self.assertFalse(SelectionObserver(self.s,{}).process_one(self.t+2))
        state=self.o.state();self.assertEqual(len(state['records']),1)
        self.assertEqual(state['records'][0]['state'],'PLAN_READY')
        self.assertFalse(state['orders_enabled']);self.assertEqual(state['extra_ai_calls'],0)
        self.assertNotIn('packet',state['records'][0])
        self.assertIn('packet',self.o.audit(self.o.revision,self.t))
        with self.s.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_concurrent_workers_only_write_once(self):
        with ThreadPoolExecutor(2) as pool:res=list(pool.map(lambda _:self.o.process_one(self.t+1),range(2)))
        self.assertEqual(sum(res),1)

    def test_late_correction_cannot_rewrite_frozen_decision(self):
        self.o.process_one(self.t+1);old=self.o.audit(self.o.revision,self.t)
        with self.s.connect() as db:db.execute("UPDATE bars SET payload='{}' WHERE symbol='ES'")
        self.assertFalse(self.o.process_one(self.t+2))
        self.assertEqual(old,self.o.audit(self.o.revision,self.t))

    def test_missing_nq_cannot_reuse_mnq_as_analysis(self):
        with self.s.connect() as db:db.execute('DELETE FROM collection_bars')
        self.assertFalse(self.o.process_one(self.t+1));self.assertEqual(self.o.state()['records'],[])

    def test_restart_cannot_create_fresh_entry_from_stale_bars(self):
        self.o.process_one(self.t+500)
        r=self.o.state()['records'][0];self.assertEqual(r['state'],'ABSTAIN')
        self.assertIn('STALE_DECISION',r['reasons'])

    def test_disabled_and_risk_validation(self):
        self.assertFalse(SelectionObserver(self.s,{'TANJA_AUTO_SELECTION_ENABLED':'false'}).process_one(self.t+1))
        for limit in ('nan','-1','41'):
            with self.assertRaises(ValueError):SelectionObserver(self.s,{'TANJA_ACCOUNT_MAX_MNQ':limit})

    def test_balance_controls_size_and_old_dollar_config_is_ignored(self):
        self.balance_file.write_text(json.dumps(dict(balance=49000,minimum_balance=48000,target_balance=53000,as_of='test snapshot')))
        o=SelectionObserver(self.s,{'TANJA_AUTO_PAPER_RISK_USD':'100','TANJA_AUTO_PAPER_MAX_CONTRACTS':'1'})
        o.process_one(self.t+1)
        r=o.state()['records'][0]
        self.assertEqual(r['sizing']['budget_usd'],245.0)
        self.assertEqual(r['selected']['audit']['quantity'],7)
        self.assertLessEqual(r['compiled']['price_review']['planned_price_risk_before_costs'],245.0)
        self.assertFalse(r['sizing']['broker_verified'])
        old=o.audit(o.revision,self.t)
        self.balance_file.write_text(json.dumps(dict(balance=40000,minimum_balance=38000,target_balance=53000,as_of='new snapshot')))
        self.assertEqual(o.sizing_context()['budget_usd'],200)
        self.assertFalse(o.process_one(self.t+2))
        self.assertEqual(o.audit(o.revision,self.t),old)

    def test_no_balance_no_fallback_budget(self):
        self.balance_file.unlink()
        self.o.process_one(self.t+1)
        r=self.o.state()['records'][0]
        self.assertEqual(r['reasons'],['CURRENT_BALANCE_REQUIRED'])
        self.assertIsNone(r['sizing']['budget_usd'])

    def test_api_requires_auth_and_has_no_execution_route(self):
        from app import App
        import base64,io
        app=App(dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='a'*40,TANJA_DASHBOARD_PASSWORD='test_dashboard_password'))
        def request(path,auth=False,method='GET'):
            env=dict(PATH_INFO=path,REQUEST_METHOD=method,CONTENT_LENGTH='0',**{'wsgi.input':io.BytesIO()})
            if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test_dashboard_password').decode()
            status=[]
            body=b''.join(app(env,lambda code,headers:status.append(code)))
            return status[0],json.loads(body)
        self.o.process_one(self.t+1)
        for path in ('/api/automatic-selection',f'/api/automatic-selection/audit/{self.o.revision}/{self.t}'):
            self.assertTrue(request(path)[0].startswith('401'))
            self.assertTrue(request(path,True)[0].startswith('200'))
            self.assertTrue(request(path,True,'POST')[0].startswith('405'))
        self.assertFalse(request('/health')[1]['orders_enabled'])

    def test_delayed_feed_arrival_excluded(self):
        with self.s.connect() as db:db.execute('UPDATE collection_bars SET received=? WHERE end=?',(self.t+20,self.t))
        self.o.process_one(self.t+1)
        r=self.o.state()['records'][0]
        self.assertNotEqual(r['state'],'PLAN_READY')
        self.assertLess(r['cutoff'],self.t)

    def test_short_saved_plan_exports_authenticated_pine(self):
        from test_short_selector import short_fixture
        from app import App
        import base64,io
        markets,_=short_fixture()
        with self.s.connect() as db:
            for sym,rows in markets.items():
                table='collection_bars' if sym=='NQ' else 'bars'
                for b in rows:
                    payload=dict(symbol=sym,bar_open_ms=b['time']*1000,bar_close_ms=(b['time']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                    db.execute(f'UPDATE {table} SET payload=? WHERE symbol=? AND start=?',(json.dumps(payload),sym,b['time']))
        self.o.process_one(self.t+1)
        self.assertEqual(self.o.state()['records'][0]['compiled']['plan']['direction'],'short')
        app=App(dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='a'*40,TANJA_DASHBOARD_PASSWORD='test_dashboard_password'))
        env=dict(PATH_INFO=f'/api/automatic-selection/pine/{self.o.revision}/{self.t}',REQUEST_METHOD='GET',CONTENT_LENGTH='0',**{'wsgi.input':io.BytesIO()})
        status=[]
        b''.join(app(env,lambda code,headers:status.append(code)))
        self.assertTrue(status[-1].startswith('401'))
        env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test_dashboard_password').decode()
        body=b''.join(app(env,lambda code,headers:status.append(code))).decode()
        self.assertTrue(status[-1].startswith('200'))
        self.assertIn('Planned short entry',body)
        self.assertNotIn('strategy.entry',body)

if __name__=='__main__':unittest.main()
