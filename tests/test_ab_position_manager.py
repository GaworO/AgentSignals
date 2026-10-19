import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import ab_position_manager as manager
import ab_native_entry as entry
from test_ab_routing import ab_env
import test_builder_crosstrade as native_fixtures
from test_builder_crosstrade import ROOT, functions


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.start=int(pd.Timestamp('2026-10-19T14:00:00Z').timestamp()*1000)
        self.known=self.start+4*60000
        self.now=self.known+1000
        self.order=dict(order_id='ab-test',strategy='A/B',lifecycle=manager.LIFECYCLE,direction='LONG',entry_price=100.,stop_price=90.,
                        target_price=130.,baseline_target_price=120.,risk_points=10.,runner_enabled=True)
        self.position=dict(fill_ms=self.start+1000,entry_price=100.)
        self.minutes=pd.DataFrame([dict(open=100.,high=105.,low=99.,close=102.,volume=1) for _ in range(64)],
            index=pd.date_range(pd.Timestamp(self.start-60*60000,unit='ms',tz='UTC'),periods=64,freq='min'))
        self.context=dict(owner='A/B',state='ALLOW',review_id='review',cutoff=self.start/1000-60,
                          available_at=self.start/1000-30,expires_at=self.start/1000+3600)

    def assess(self,**kw):
        return manager.assess(self.order,self.position,self.minutes,self.known,self.now,self.context,**kw)

    def test_current_model_files_are_verified_and_wrong_year_is_blocked(self):
        self.assertIsNone(manager.readiness(self.now))
        self.assertIsNotNone(manager.readiness(int(pd.Timestamp('2027-01-02T14:00Z').timestamp()*1000)))

    def test_ai_changes_only_target_in_real_ab_and_missing_context_uses_2r(self):
        for direction in ('LONG','SHORT'):
            z=1 if direction=='LONG' else -1
            for state,multiple in [('ALLOW',3),('WAIT',2),('VETO',2),('INVALIDATED',2),('UNAVAILABLE',2)]:
                with patch.dict(os.environ,{**ab_env(),'AB_MANAGER_MODE':'real'}):
                    s=dict(_strat='A/B',dir=direction,entry=100.,SL=100-z*10,TP=100+z*20)
                    manager.plan_signal(s,dict(self.context,state=state),self.now/1000)
                    self.assertEqual(s['TP'],100+z*multiple*10)
                    self.assertEqual((s['entry'],s['SL']),(100,100-z*10))
                    self.assertIsNone(manager.validate_plan(s,self.now/1000))
                    if state=='ALLOW':self.assertIn('expired',manager.validate_plan(s,self.context['expires_at']))
        with patch.dict(os.environ,{**ab_env(),'AB_MANAGER_MODE':'shadow'}):
            s=dict(_strat='A/B',dir='LONG',entry=100,SL=90,TP=120)
            manager.plan_signal(s,self.context,self.now/1000)
            self.assertNotIn('_ab_manager',s)

    def test_no_context_or_no_local_model_never_selects_runner(self):
        self.minutes.iloc[-1,self.minutes.columns.get_loc('high')]=121
        with patch.object(manager.runner,'decide',return_value=dict(selected=True,probability=.9)):
            self.assertEqual(self.assess()['decision'],'HOLD')
            self.context['state']='INVALIDATED'
            self.assertEqual(self.assess()['decision'],'CLOSE')
        self.context['state']='ALLOW'
        with patch.object(manager.runner,'decide',return_value=dict(selected=False,probability=.2)):
            self.assertEqual(self.assess()['decision'],'CLOSE')
        with patch.object(manager.runner,'decide',side_effect=ValueError('gap')):
            self.assertEqual(self.assess()['reason'],'RUNNER_DATA_FALLBACK_EXIT_AFTER_2R')

    def test_real_model_reads_only_pre_touch_closed_minutes(self):
        self.minutes.iloc[-1,self.minutes.columns.get_loc('high')]=121
        result=self.assess()
        self.assertIn(result['decision'],('HOLD','CLOSE'))
        self.assertNotIn('error',result['runner'])
        self.assertLess(result['runner']['features']['prebar_mfe_r'],2)
        self.assertEqual(result['runner']['touch_bar_ms'],self.known-60000)

    def test_m3_requires_current_rule_and_ignores_ai_veto_before_2r(self):
        self.context['state']='VETO'
        self.assertEqual(self.assess()['decision'],'HOLD')
        with patch.object(manager.frozen,'evaluate',return_value=dict(signals=dict(primary_manager='M3',primary_reason='F2'))):
            self.assertEqual(self.assess()['reason'],'F2')
            self.assertEqual(self.assess()['decision'],'CLOSE')
            self.now=self.known+16000
            self.assertEqual(self.assess()['decision'],'HOLD')

    def test_stops_or_targets_and_fill_minute_are_never_simulated_closures(self):
        with patch.object(manager.runner,'decide',side_effect=AssertionError('must not infer before broker')):
            self.minutes.iloc[-1,self.minutes.columns.get_loc('high')]=131
            self.assertEqual(self.assess()['reason'],'BRACKET_TOUCHED_WAIT_BROKER')
            self.minutes.iloc[-1,self.minutes.columns.get_loc('high')]=105
            self.minutes.iloc[-1,self.minutes.columns.get_loc('low')]=89
            self.assertEqual(self.assess()['reason'],'BRACKET_TOUCHED_WAIT_BROKER')
        self.minutes.iloc[-1,self.minutes.columns.get_loc('low')]=99
        self.minutes.loc[pd.Timestamp(self.start,unit='ms',tz='UTC'),'high']=140
        self.assertEqual(self.assess()['decision'],'HOLD')

    def test_short_is_symmetric_and_runner_decision_survives_restart(self):
        self.order.update(direction='SHORT',stop_price=110,target_price=70,baseline_target_price=80)
        self.minutes[['high','low']]=[101,95]
        self.minutes.iloc[-1,self.minutes.columns.get_loc('low')]=79
        with patch.object(manager.runner,'decide',return_value=dict(selected=True,probability=.9)):
            result=self.assess();self.assertEqual(result['decision'],'HOLD')
        with patch.object(manager.runner,'decide',side_effect=AssertionError('must not rerun frozen choice')):
            self.context['state']='VETO'
            self.assertEqual(self.assess(previous_runner=result['runner'])['reason'],'RUNNER_DECISION_ALREADY_FROZEN')


class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=native_fixtures.NativeTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.env=patch.dict(os.environ,{**ab_env(),'AB_MANAGER_MODE':'real'});self.env.start();self.addCleanup(self.env.stop)
        f=self.fixture
        f.order.update(strategy='A/B',lifecycle=manager.LIFECYCLE,baseline_target_price=30020.,runner_enabled=True,
                       manager_plan=dict(policy=manager.POLICY,lifecycle=manager.LIFECYCLE,runner_enabled=True,
                                         baseline_target_price=30020.,context_expires_at=f.now+3600))
        f.prepare()
        # Extracted function requires module globals; bind frame separately.
        ns=dict(pd=pd,json=json);functions(ROOT/'ab_v3_live.py',{'frame'},ns);f.api.frame=ns['frame']
        self.context=lambda signal:dict(owner='A/B',state='ALLOW',review_id='review',cutoff=f.now-30,available_at=f.now-20,expires_at=f.now+3600)
        self.close=lambda oid,aid:f.t.action(oid,'close',aid)

    def feed(self,touch=False):
        f=self.fixture;fill=int(json.loads(f.local()['position_json'])['fill_ms'])//60000*60000
        known=fill+4*60000;f.now=(known+1000)/1000
        with f.api.connect() as c:
            for k in range(-60,4):
                at=fill+k*60000
                bar=dict(open=30000.,high=30021. if touch and k==3 else 30005.,low=29999.,close=30002.,volume=1.)
                c.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?)',('M1','MNQZ2026',at,json.dumps(bar),int(f.now*1000)))
        f.t.reconcile_one(f.t.row('order-one'),f.snapshot([f.entry],2))

    def run_manager(self):
        manager.monitor(self.fixture.api,self.context,self.close)

    def test_waits_for_fill_then_m3_closes_exact_owned_position_only_once(self):
        f=self.fixture;f.submit()
        self.run_manager();self.assertEqual(len(f.posts),1)
        f.open();self.assertEqual(f.local()['state'],'BROKER_CONFIRMED_OPEN')
        self.feed()
        with patch.object(manager.frozen,'evaluate',return_value=dict(signals=dict(primary_manager='M3',primary_reason='F2'))):
            self.run_manager();self.run_manager()
        self.assertEqual(len(f.posts),2)
        self.assertTrue(f.posts[-1][0].endswith('/positions/close'))
        self.assertEqual(f.posts[-1][1],{'instrument':'MNQZ6'})
        self.assertNotEqual(f.local()['state'],'CLOSED')
        with f.api.connect() as c:
            row=c.execute('SELECT * FROM ab_manager_closes').fetchone()
            self.assertEqual(row['state'],'ACKNOWLEDGED')

    def test_planned_signal_prepares_the_same_managed_bracket_sent_to_native_api(self):
        f=self.fixture
        s=dict(_strat='A/B',dir='LONG',entry=30000.,SL=29990.,TP=30020.,bos_ms=int(f.now*1000)-60000)
        manager.plan_signal(s,self.context(s),f.now)
        entry.configure_signal(s)
        payload=f.payload();payload['takeProfit']['limitPrice']=s['TP']
        oid=entry.prepare(s,payload,'fresh-managed-signal',f.api)
        reply=f.t.submit(payload,oid)
        self.assertTrue(reply['sent'])
        self.assertEqual(f.posts[-1][1]['takeProfit'],30030.)
        self.assertEqual(f.posts[-1][1]['stopLoss'],29990.)
        with f.api.connect() as c:
            row=c.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
            self.assertIsNone(row['position_json'])
            self.assertEqual(json.loads(row['order_json'])['lifecycle'],manager.LIFECYCLE)

    def test_better_limit_fill_is_owned_and_can_be_managed(self):
        f=self.fixture;f.submit()
        original_fill=f.fill
        def improved(identity,oid,qty,price,action,offset=1):
            return original_fill(identity,oid,qty,29999. if identity==101 else price,action,offset)
        with patch.object(f,'fill',side_effect=improved):
            f.open()
        self.assertEqual(f.local()['state'],'BROKER_CONFIRMED_OPEN')
        self.assertEqual(json.loads(f.local()['position_json'])['entry_price'],29999.)

    def test_partial_fill_and_directional_are_never_managed(self):
        f=self.fixture;f.submit();f.open(qty=1);self.run_manager();self.assertEqual(len(f.posts),1)
        with f.api.connect() as c:
            o=copy.deepcopy(f.order);o['strategy']='AB_DIRECTIONAL'
            c.execute("UPDATE orders SET state='BROKER_CONFIRMED_OPEN',order_json=?,position_json=?",(json.dumps(o),json.dumps({})))
        self.run_manager();self.assertEqual(len(f.posts),1)

    def test_timeout_cannot_send_duplicate_exit_on_next_tick_or_restart(self):
        f=self.fixture;f.submit();f.open();self.feed()
        f.timeout=True
        with patch.object(manager.frozen,'evaluate',return_value=dict(signals=dict(primary_manager='M3',primary_reason='B11'))):
            self.run_manager();self.run_manager()
        self.assertEqual(len(f.posts),2)
        with f.api.connect() as c:self.assertEqual(c.execute('SELECT state FROM ab_manager_closes').fetchone()[0],'UNKNOWN')

    def test_runner_rejects_and_closes_using_real_native_transport(self):
        f=self.fixture;f.submit();f.open();self.feed(touch=True)
        with patch.object(manager.runner,'decide',return_value=dict(selected=False,probability=.1)):
            self.run_manager()
        self.assertEqual(len(f.posts),2)
        self.assertTrue(f.posts[-1][0].endswith('/positions/close'))

    def test_shadow_mode_writes_decision_but_never_dispatches(self):
        f=self.fixture;f.submit();f.open();self.feed()
        with patch.dict(os.environ,AB_MANAGER_MODE='shadow'),patch.object(manager.frozen,'evaluate',return_value=dict(signals=dict(primary_manager='M3',primary_reason='F2'))):
            self.run_manager()
        self.assertEqual(len(f.posts),1)

    def test_foreign_broker_position_at_send_blocks_manager_close(self):
        f=self.fixture;f.submit();f.open();self.feed()
        f.values['/accounts/Builder%20account/positions']=[dict(accountId=2214,contractId=100,netPos=3)]
        with patch.object(manager.frozen,'evaluate',return_value=dict(signals=dict(primary_manager='M3',primary_reason='F2'))):
            self.run_manager()
        self.assertEqual(len(f.posts),1)


if __name__=='__main__':unittest.main()
