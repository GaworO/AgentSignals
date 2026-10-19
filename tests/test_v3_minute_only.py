"""M1-only V3 behavior with real SQLite/candle handling and mocked model outputs."""
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock,patch

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]


def functions(path,names,scope):
    tree=ast.parse(path.read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),scope)


class MinuteOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1792400400000 # An exact minute close.
        self.env=patch.dict(os.environ,dict(AB_V3_DB=str(Path(self.tmp.name)/'v3.sqlite3'),
            AB_V3_MODE='LIVE',AB_V3_CONTRACT='MNQZ2026',EXEC_TICKER='MNQZ2026',
            AB_V3_DETECTOR_CONTRACT_VERIFIED='1',ACCOUNT_LABEL='test'))
        self.env.start();self.addCleanup(self.env.stop)
        self.api=types.ModuleType('minute_test_api')
        scope=self.api.__dict__
        scope.update(Path=Path,sqlite3=sqlite3,os=os,json=json,pd=pd,math=math,hashlib=hashlib,HERE=Path(self.tmp.name),
            now_ms=lambda:self.now,contract=lambda:'MNQZ2026',mode=lambda:'LIVE',route=lambda:'test-route',
            exit_mode=lambda:'real',_M1_CALLBACK=None,
            builder_broker_sync=types.SimpleNamespace(entry_blocker=lambda **kw:None),
            exit_blockers=lambda:[],frozen_parity_ok=lambda:True,runner_parity_ok=lambda:True,runner=types.SimpleNamespace(model_ok=lambda at:True))
        functions(ROOT/'ab_v3_live.py',{'db_path','connect','dump','_exit_transition','_bar','ingest','frame','m1_fresh',
            'prepared_entry_blocker','_runner_first_touch','monitor','dispatch_exits','dispatch_frozen_exits'},scope)
        self.boundary=Mock();self.frozen=Mock();self.runner=Mock()
        self.boundary.evaluate.side_effect=lambda o,p,m,k:[dict(known_ms=k,classification='TEST')]
        self.boundary.manager.return_value=dict(decision='HOLD',reason='test')
        self.frozen.evaluate.return_value={};self.runner.decide.return_value=dict(selected=False,probability=.4,threshold=.75)
        self.manager=types.ModuleType('minute_test_manager')
        self.manager.__dict__.update(live=lambda:self.api,pd=pd,json=json,os=os,threading=threading,Path=Path,
            boundary=self.boundary,frozen=self.frozen,runner=self.runner,M3=dict(M1=False,M2=False,M3=True),
            LIFECYCLE='ASSUMED_BOUNDARY_M3_RUNNER_V1',ACTIVE=('ASSUMED_OPEN','BROKER_CONFIRMED_OPEN'))
        functions(ROOT/'v3_position_manager.py',{'_touches','_monitor_one','_request','_close','on_m1'},self.manager.__dict__)
        self.api.position_manager=self.manager
        self.order=dict(order_id='O1',contract='MNQZ2026',route_id='test-route',account_label='test',direction='LONG',
            entry_price=100.,stop_price=90.,target_price=130.,baseline_target_price=120.,risk_points=10.,quantity=1,
            activation_ms=self.now-600000,expiry_ms=self.now+600000,lifecycle=self.manager.LIFECYCLE,runner_enabled=True)
        self.position=dict(fill_ms=self.now-180000+1000,event_ms=self.now-180000+1000,entry_price=100.,quantity=1,
            assumed=False,position_id='p1',contract='MNQZ2026',route_id='test-route',account_label='test',direction='LONG',exclusive=True)

    def bar(self,start,*,low=99.,high=101.,close=100.,time_text=None):
        return dict(ts_event=time_text or pd.Timestamp(start,unit='ms',tz='UTC').isoformat(),open=100.,high=high,low=low,close=close,volume=1.)

    def add(self,start,**kw):
        self.api.ingest(dict(contract='MNQZ2026',tf='M1',bars=[self.bar(start,**kw)]),current=self.now,notify_m1=False)

    def save(self,state='BROKER_CONFIRMED_OPEN'):
        with self.api.connect() as c:
            c.execute('INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?)',('O1',state,json.dumps(self.order),json.dumps(self.position),self.now))
            c.execute('INSERT OR IGNORE INTO exit_trades(trade_id,exit_mode,status,updated_ms) VALUES(?,?,?,?)',('O1','real','OPEN',self.now))
        return self.row()

    def row(self):
        with self.api.connect() as c:return c.execute('SELECT * FROM orders WHERE order_id=?',('O1',)).fetchone()

    def test_seconds_are_rejected_and_bridge_does_not_ingest_anything(self):
        with self.assertRaisesRegex(ValueError,'M1'):
            self.api.ingest(dict(contract='MNQZ2026',tf='1s',bars=[self.bar(self.now-1000)]),current=self.now)
        namespace={};exec((ROOT/'ab_v3_tv.py').read_text(),namespace)
        self.assertEqual(namespace['on_batch']({'bars':[self.bar(self.now-1000)]})['v3_data_bridge'],'DISABLED_M1_ONLY')
        with self.api.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM bars').fetchone()[0],0)

    def test_entry_uses_fresh_completed_m1_with_no_second_bars(self):
        with self.api.connect() as c:
            c.execute('INSERT INTO orders VALUES(?,?,?,?,?)',('O1','PREPARED',json.dumps(self.order),None,self.now))
        signal=dict(_v3_directional=True,_continuation_order_id='O1',entry=100.,SL=90.,TP=130.,dir='LONG')
        self.assertEqual(self.api.prepared_entry_blocker(signal),'v3:m1_feed_not_fresh')
        self.add(self.now-60000)
        self.assertIsNone(self.api.prepared_entry_blocker(signal))
        self.now+=75001
        self.assertEqual(self.api.prepared_entry_blocker(signal),'v3:m1_feed_not_fresh')

    def test_unclosed_m1_is_not_accepted_or_a_freshness_source(self):
        with self.assertRaisesRegex(ValueError,'unclosed'):
            self.add(self.now)
        with self.api.connect() as c:self.assertFalse(self.api.m1_fresh(c,'MNQZ2026',self.now))

    def test_long_short_touches_use_completed_m1_and_exclude_fill_minute(self):
        self.add(self.now-180000,high=140,low=60)
        with self.api.connect() as c:self.assertEqual(self.manager._touches(c,self.order,self.position,self.now),{})
        self.add(self.now-120000,high=121,low=99)
        with self.api.connect() as c:self.assertEqual(self.manager._touches(c,self.order,self.position,self.now)['2R'],self.now-60000)
        self.order.update(direction='SHORT',stop_price=110,target_price=70,baseline_target_price=80)
        self.add(self.now-60000,low=79,high=101)
        with self.api.connect() as c:self.assertEqual(self.manager._touches(c,self.order,self.position,self.now)['2R'],self.now)

    def test_runner_uses_pre_touch_m1_and_fresh_close_time_not_an_intraminute_fill(self):
        self.add(self.now-120000,high=110)
        self.add(self.now-60000,high=121)
        self.manager._monitor_one(self.save())
        with self.api.connect() as c:
            r=c.execute('SELECT * FROM runner_decisions').fetchone()
            e=c.execute('SELECT signal_time,requested_price,primary_manager FROM exit_trades').fetchone()
        self.assertEqual((r['touch_ms'],r['touch_bar_ms']),(self.now,self.now-60000))
        self.assertEqual(json.loads(r['payload_json'])['touch_basis'],'COMPLETED_M1')
        self.assertEqual(e['signal_time'],self.now);self.assertEqual(e['primary_manager'],'RUNNER')
        passed=self.runner.decide.call_args.args[2]
        self.assertTrue((passed.index<pd.Timestamp(self.now-60000,unit='ms',tz='UTC')).all())
        self.frozen.evaluate.assert_not_called() # No post-touch M3 exit.
        self.manager._monitor_one(self.row());self.assertEqual(self.runner.decide.call_count,1)

    def test_m3_still_evaluates_and_can_signal_before_any_2r_touch(self):
        self.add(self.now-120000,high=105);self.add(self.now-60000,high=106)
        self.boundary.manager.return_value=dict(decision='CLOSE',reason='M3_TEST')
        self.manager._monitor_one(self.save())
        self.frozen.evaluate.assert_called_once()
        with self.api.connect() as c:r=c.execute('SELECT primary_manager,signal_time FROM exit_trades').fetchone()
        self.assertEqual((r[0],r[1]),('M3',self.now))

    def test_stale_touch_does_not_send_a_catchup_exit(self):
        self.add(self.now-120000,high=121);self.add(self.now-60000,high=122)
        self.manager._monitor_one(self.save())
        with self.api.connect() as c:r=c.execute('SELECT status,signal_time,error_reason FROM exit_trades').fetchone()
        self.assertEqual(r['status'],'EXIT_BLOCKED');self.assertIsNone(r['signal_time']);self.assertEqual(r['error_reason'],'runner_stale_touch')

    def test_same_bar_stop_target_does_not_invent_a_broker_closure(self):
        self.add(self.now-60000,low=89,high=131)
        self.manager._monitor_one(self.save())
        self.assertEqual(self.row()['state'],'EXIT_PENDING')
        self.assertFalse(json.loads(self.row()['position_json'])['assumed'])
        with self.api.connect() as c:self.assertIsNone(c.execute('SELECT exit_pnl FROM exit_trades').fetchone()[0])

    def test_same_bar_assumed_bracket_order_remains_unknown(self):
        self.position['assumed']=True;self.add(self.now-60000,low=89,high=131)
        self.manager._monitor_one(self.save('ASSUMED_OPEN'))
        self.assertEqual(self.row()['state'],'ASSUMED_OPEN')
        with self.api.connect() as c:self.assertEqual(c.execute('SELECT error_reason FROM exit_trades').fetchone()[0],'m1_bracket_order_ambiguous_wait_broker')

    def test_legacy_intrasecond_executor_cannot_be_reenabled(self):
        with patch.dict(os.environ,dict(AB_V3_LEGACY_EXIT_ENABLED='true')):
            self.assertIsNone(self.api.monitor());self.assertIsNone(self.api.dispatch_exits())
        for name in ('ab_v3_live.py','v3_position_manager.py'):
            tree=ast.parse((ROOT/name).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='frame':
                    self.assertFalse(any(isinstance(a,ast.Constant) and a.value=='1s' for a in node.args))

    def test_existing_naive_utc_minute_intake_is_normalized_without_shifting(self):
        text=pd.Timestamp(self.now-60000,unit='ms',tz='UTC').strftime('%Y-%m-%d %H:%M:%S')
        self.api._tick=lambda:None
        with patch.object(threading,'Thread') as thread:
            self.manager.on_m1(self.bar(self.now-60000,time_text=text))
            thread.return_value.start.assert_called_once()
        with self.api.connect() as c:
            row=c.execute('SELECT tf,ts_ms,bar_json FROM bars').fetchone()
        self.assertEqual((row['tf'],row['ts_ms']),('M1',self.now-60000))
        self.assertEqual(pd.Timestamp(json.loads(row['bar_json'])['ts_event']).tzinfo,pd.Timestamp('2026-01-01T00:00Z').tzinfo)

    def test_m3_exit_dispatch_uses_m1_freshness_without_second_bars(self):
        self.add(self.now-60000);self.save()
        with self.api.connect() as c:
            c.execute("UPDATE exit_trades SET status='EXIT_SIGNALLED',signal_time=?,primary_manager='M3',primary_reason='TEST'",(self.now,))
        self.now+=2000;self.manager.send_exit=Mock()
        self.api.dispatch_frozen_exits()
        self.manager.send_exit.assert_called_once()
        payload=self.manager.send_exit.call_args.args[2]
        self.assertEqual(payload['action'],'exit');self.assertEqual(payload['extras']['v3OrderId'],'O1')
        with self.api.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM bars WHERE tf='1s'").fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM actions').fetchone()[0],1)


if __name__=='__main__':unittest.main()
