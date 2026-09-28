import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import ab_v3_live as live
import ab_v3_policy as policy


class PolicyTests(unittest.TestCase):
    def inputs(self):
        m = dict(speed=-.2,efficiency=-.4,dol_delivered=False,source_failed=True,
            mfe=.1,progress=-.4,failed_fvg=True,be_returned=False,failed_entry=True,
            age_seconds=180,weak_streak=2,m1_protect=False)
        c = {tf:dict(vote=0,broken=None) for tf in policy.TF}
        c['M5']['broken']=True
        return m,c

    def test_loss_needs_confirmed_context(self):
        m,c=self.inputs()
        self.assertTrue(policy.decide(m,c)['exit'])
        c['M5']['broken']=None
        self.assertFalse(policy.decide(m,c)['exit'])

    def test_missing_micro_never_adverse(self):
        m,c=self.inputs();m['speed']=None
        self.assertFalse(policy.decide(m,c)['exit'])

    def test_dol_alone_does_not_exit(self):
        m,c=self.inputs();m.update(dol_delivered=True,source_failed=False,failed_fvg=False)
        self.assertFalse(policy.decide(m,c)['exit'])

    def test_support_requires_three_but_profit_has_no_htf_veto(self):
        m,c=self.inputs();m.update(source_failed=False,progress=.4,mfe=1.2,m1_protect=True)
        c['M15']['vote']=c['H1']['vote']=1
        self.assertFalse(policy.decide(m,c)['exit'])
        m['weak_streak']=3
        self.assertEqual(policy.decide(m,c)['cause'],'M1_PROFIT_PROTECTION')

    def test_initial_sixty_seconds_observe(self):
        m,c=self.inputs();m['age_seconds']=59
        self.assertFalse(policy.decide(m,c)['exit'])

    def test_acceptance_requires_no_reclaim(self):
        self.assertTrue(policy.acceptance(np.repeat(-.1,60),0))
        self.assertFalse(policy.acceptance(np.r_[.06,np.repeat(-.1,59)],0))
        self.assertFalse(policy.acceptance(np.repeat(-.1,59),0))

    def test_htf_does_not_use_unclosed_or_missing_m1(self):
        idx=pd.date_range('2026-01-01T00:00Z',periods=5,freq='min')
        m=pd.DataFrame(dict(open=100,high=101,low=99,close=100,volume=1),index=idx)
        self.assertTrue(policy.closed_context(m,pd.Timestamp('2026-01-01T00:04Z'))['M5'].empty)
        self.assertEqual(len(policy.closed_context(m,pd.Timestamp('2026-01-01T00:05Z'))['M5']),1)
        self.assertTrue(policy.closed_context(m.drop(idx[2]),pd.Timestamp('2026-01-01T00:05Z'))['M5'].empty)

    def test_pivot_waits_for_two_right_closed_bars(self):
        idx=pd.date_range('2026-01-01T00:00Z',periods=5,freq='5min')
        m=pd.DataFrame(dict(open=100,high=110,low=[99,98,95,98,99],close=100,volume=1),index=idx)
        self.assertTrue(policy.pivots(m.iloc[:4],5)['pivot_1'].isna().all())
        out=policy.pivots(m,5)
        self.assertEqual(out['pivot_1'].iloc[-1],95)
        self.assertEqual(out['available_at'].iloc[-1],idx[-1]+pd.Timedelta(minutes=5))


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,dict(AB_V3_DB=str(Path(self.tmp.name)/'v3.sqlite3'),
            AB_V3_MODE='SHADOW',AB_V3_CONTRACT='MNQZTEST',EXEC_TICKER='MNQZTEST',
            ACCOUNT_LABEL='50K A',AB_V3_FEED_TOKEN='feed-test-token',AB_V3_BROKER_TOKEN='broker-test-token',
            AB_V3_EXCLUSIVE_ROUTE='1',AB_V3_DETECTOR_CONTRACT_VERIFIED='1',PRICE_OFFSET='0',
            EXEC_WEBHOOK='https://example.invalid/not-a-real-route'))
        self.env.start();live._ROUTE_CALLBACK=lambda:'route-A';live._M1_CALLBACK=None
        self.readiness=patch.object(live,'RELEASE_LIVE_READY',True);self.readiness.start()
        self.at=1767225600000
        self.order=dict(order_id='O1',candidate_id='C1',strategy='AB_DIRECTIONAL',direction='LONG',
            activation_ms=self.at,expiry_ms=self.at+600000,entry_price=100,stop_price=90,target_price=120,
            risk_points=10,payload_json=json.dumps(dict(v3_snapshot=dict(source_level=98,fvg_edge=99,
                frozen_dol=dict(price=125,id='DOL-1')))))

    def tearDown(self):
        self.readiness.stop();self.env.stop();self.tmp.cleanup();live._ROUTE_CALLBACK=None;live._M1_CALLBACK=None

    def sent(self):
        live.prepare(self.order)
        live.record_dispatch(self.order,dict(state='SENT',quantity=2))

    def event(self,kind='POSITION',**extra):
        e=dict(event_id='E1',event=kind,event_ms=self.at+120000,route_id='route-A',
            contract='MNQZTEST',account_label='50K A',order_id='O1',position_id='P1',
            direction='LONG',quantity=2,entry_price=100,stop_price=90,target_price=120,
            fill_ms=self.at+60000,exclusive=True)
        e.update(extra);return e

    def state(self):
        with live.connect() as c:return c.execute('SELECT state FROM orders WHERE order_id="O1"').fetchone()[0]

    def bars(self,tf='1s'):
        return dict(tf=tf,contract='MNQZTEST',bars=[dict(ts_event='2026-01-01T00:00:00Z',open=100,high=101,low=99,close=100,volume=1)])

    def test_http_sent_is_not_broker_fill(self):
        self.sent();self.assertEqual(self.state(),'WAITING_BROKER_FILL')
        with patch('ab_v3_live.now_ms',return_value=self.at+180000):live.monitor()
        with live.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],0)

    def test_authenticated_position_and_close(self):
        self.sent();live.broker_event(self.event(),self.at+120000)
        self.assertEqual(self.state(),'OPEN')
        live.broker_event(self.event('CLOSED',event_id='E2',event_ms=self.at+180000,quantity=0,realized_net_usd=33),self.at+180000)
        self.assertEqual(self.state(),'CLOSED')

    def test_foreign_account_contract_or_direction_is_rejected(self):
        self.sent()
        for change in ({'account_label':'50K B'},{'contract':'MNQOTHER'},{'direction':'SHORT'},{'order_id':'foreign'}):
            with self.assertRaises(ValueError):live.broker_event(self.event(**change),self.at+120000)

    def test_route_change_cannot_adopt_old_order(self):
        self.sent();live._ROUTE_CALLBACK=lambda:'route-B'
        with self.assertRaises(ValueError):live.broker_event(self.event(route_id='route-B'),self.at+120000)

    def test_duplicate_events_idempotent_conflict_rejected(self):
        self.sent();e=self.event();live.broker_event(e,self.at+120000)
        self.assertTrue(live.broker_event(e,self.at+120000)['duplicate'])
        with self.assertRaises(ValueError):live.broker_event(dict(e,quantity=1),self.at+120000)

    def test_partial_fill_stays_visible_but_cannot_exit(self):
        self.sent();live.broker_event(self.event(quantity=1),self.at+120000)
        self.assertEqual(self.state(),'OWNERSHIP_MISMATCH')

    def test_snapshot_is_immutable_after_dispatch(self):
        self.sent();changed=dict(self.order,payload_json=json.dumps(dict(v3_snapshot={'source_level':1000})))
        live.record_dispatch(changed,dict(state='SENT',quantity=2))
        with live.connect() as c:o=json.loads(c.execute('SELECT order_json FROM orders').fetchone()[0])
        self.assertEqual(o['snapshot']['source_level'],98)

    def test_conflicting_feed_unclosed_bar_and_contract_mismatch(self):
        b=self.bars();live.ingest(b,self.at+1000)
        self.assertEqual(live.ingest(b,self.at+1000)['duplicate'],1)
        with self.assertRaises(ValueError):live.ingest(self.bars(),self.at+999)
        b['bars'][0]['high']=102
        with self.assertRaises(ValueError):live.ingest(b,self.at+1000)
        with self.assertRaises(ValueError):live.ingest(dict(self.bars(),contract='OTHER'),self.at+1000)

    def test_one_second_bars_never_feed_existing_m1_detector(self):
        cb=Mock();live._M1_CALLBACK=cb
        live.ingest(self.bars(),self.at+1000);cb.assert_not_called()
        live.ingest(self.bars('M1'),self.at+60001);cb.assert_called_once()

    def test_gap_or_m1_volume_mismatch_disables_manager(self):
        idx=pd.date_range('2026-01-01T00:00Z',periods=60,freq='s')
        s=pd.DataFrame(dict(open=100,high=101,low=99,close=100,volume=1),index=idx)
        m=s.resample('min').agg(dict(open='first',high='max',low='min',close='last',volume='sum'))
        self.assertTrue(live._verified_seconds(s,m))
        self.assertFalse(live._verified_seconds(s.drop(idx[12]),m))
        m['volume']=59;self.assertFalse(live._verified_seconds(s,m))

    def test_shadow_never_submits_broker_action(self):
        with patch('requests.post') as post:live.dispatch_exits();post.assert_not_called()

    def test_flat_does_not_erase_unresolved_order(self):
        self.sent()
        with self.assertRaises(ValueError):live.broker_event(self.event('FLAT',quantity=0),self.at+120000)

    def test_exit_after_latency_requires_fresh_broker_and_is_once(self):
        self.sent();current=self.at+180002
        live.broker_event(self.event(event_ms=current),current)
        os.environ['AB_V3_MODE']='LIVE'
        with live.connect() as c:
            c.execute('INSERT INTO decisions VALUES(?,?,?)',('O1',self.at+180000,live.dump(dict(exit=True))))
        live.ingest(dict(self.bars(),bars=[dict(ts_event='2026-01-01T00:02:59Z',open=100,high=101,low=99,close=100,volume=1)]),current)
        response=Mock(status_code=200);response.json.return_value={'success':True}
        with patch('ab_v3_live.now_ms',return_value=current),patch('requests.post',return_value=response) as post:
            live.dispatch_exits();post.assert_not_called() # less than 1s since decision
        current=self.at+181100
        live.broker_event(self.event(event_id='E2',event_ms=current),current)
        with patch('ab_v3_live.now_ms',return_value=current),patch('requests.post',return_value=response) as post:
            live.dispatch_exits();live.dispatch_exits();post.assert_called_once()
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['action'],'exit');self.assertNotIn('sentiment',payload)
        self.assertEqual(self.state(),'EXIT_PENDING') # not CLOSED

    def test_unknown_exit_never_retries(self):
        self.sent();current=self.at+181100;live.broker_event(self.event(event_ms=current),current)
        os.environ['AB_V3_MODE']='LIVE'
        with live.connect() as c:c.execute('INSERT INTO decisions VALUES(?,?,?)',('O1',self.at+180000,live.dump(dict(exit=True))))
        live.ingest(dict(self.bars(),bars=[dict(ts_event='2026-01-01T00:03:00Z',open=100,high=101,low=99,close=100,volume=1)]),current)
        with patch('ab_v3_live.now_ms',return_value=current),patch('requests.post',side_effect=TimeoutError) as post:
            live.dispatch_exits();live.dispatch_exits();post.assert_called_once()
        self.assertEqual(self.state(),'EXIT_UNKNOWN')

    def test_live_rejects_non_directional_entry(self):
        os.environ['AB_V3_MODE']='LIVE'
        self.assertEqual(live.entry_blocker(dict(self.order,strategy='CONTINUATION')),'v3_exclusive_route_other_strategy')


class HttpTests(unittest.TestCase):
    setUp = LiveTests.setUp
    tearDown = LiveTests.tearDown
    bars = LiveTests.bars
    event = LiveTests.event
    def test_endpoint_auth_and_read_only_status(self):
        from flask import Flask
        app=Flask(__name__);live.register(app,route_callback=lambda:'route-A');client=app.test_client()
        self.assertEqual(client.post('/ab/v3/feed',json=self.bars()).status_code,401)
        self.assertEqual(client.post('/ab/v3/broker',json=self.event()).status_code,401)
        r=client.get('/ab/v3');self.assertEqual(r.status_code,200);self.assertIn('DOL',r.text)
        with patch('ab_v3_live.now_ms',return_value=self.at+1000):
            r=client.post('/ab/v3/feed',headers={'X-V3-Feed-Token':'feed-test-token'},json=self.bars())
        self.assertEqual(r.status_code,200)
        data=client.get('/ab/v3/data').text
        self.assertNotIn('feed-test-token',data);self.assertNotIn('broker-test-token',data)
        self.assertNotIn('https://example.invalid',data)


if __name__=='__main__':unittest.main()
