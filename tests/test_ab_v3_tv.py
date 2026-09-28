import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask
import numpy as np
import pandas as pd

import ab_v3_live as live
import ab_v3_policy as policy
import ab_v3_tv as bridge
import tv_seconds_feed as feed
from test_tv_seconds_feed import packet, START, CURRENT


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, TV_1S_ENABLED='1',TV_1S_TOKEN=packet()['feed_token'],
            TV_1S_SYMBOL='TEST:MNQZ26',TV_1S_DB=str(Path(self.tmp.name)/'tv.sqlite3'),
            AB_V3_DB=str(Path(self.tmp.name)/'v3.sqlite3'),AB_V3_MODE='SHADOW',
            AB_V3_CONTRACT='MNQZ26',AB_V3_TV_MAPPING_VERIFIED='1')
        self.env.start()
        self.clocks = [patch.object(feed,'now_ms',return_value=CURRENT),
                       patch.object(live,'now_ms',return_value=CURRENT),patch.object(live,'_tick')]
        for p in self.clocks: p.start()
        live._M1_CALLBACK = Mock()
        app = Flask(__name__)
        feed.register(app,on_batch=bridge.on_batch)
        self.client = app.test_client()

    def tearDown(self):
        for p in reversed(self.clocks): p.stop()
        self.env.stop()
        self.tmp.cleanup()
        live._M1_CALLBACK = None
        feed._BATCH_CALLBACK = None

    def test_authenticated_batch_goes_to_manager_not_detector(self):
        r = self.client.post('/bars/1s',json=packet())
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['v3_data_bridge'],'CONNECTED')
        live._M1_CALLBACK.assert_not_called()
        with live.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bars WHERE tf='1s'").fetchone()[0],60)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bars WHERE tf='M1'").fetchone()[0],1)
        self.assertEqual(self.client.get('/feed/1s/data').json['v3_bridge']['v3_contract'],'MNQZ26')

    def test_no_mapping_means_only_collector(self):
        os.environ['AB_V3_TV_MAPPING_VERIFIED']='0'
        r = self.client.post('/bars/1s',json=packet())
        self.assertEqual(r.json['v3_data_bridge'],'WAITING_EXPLICIT_CONTRACT_MAPPING')
        self.assertFalse(Path(os.environ['AB_V3_DB']).exists())

    def test_continuous_symbol_is_not_adopted(self):
        b=packet();b['symbol']='TEST:MNQ1!';os.environ['TV_1S_SYMBOL']=b['symbol']
        self.assertEqual(self.client.post('/bars/1s',json=b).json['v3_data_bridge'],'WAITING_EXPLICIT_CONTRACT_MAPPING')
        self.assertFalse(Path(os.environ['AB_V3_DB']).exists())

    def test_off_preserves_collector_without_manager(self):
        os.environ['AB_V3_MODE']='OFF'
        self.assertEqual(self.client.post('/bars/1s',json=packet()).json['v3_data_bridge'],'OFF')
        self.assertEqual(feed.status()['total_stored_seconds'],60)

    def test_gap_remains_gap_in_manager(self):
        self.client.post('/bars/1s',json=packet(40))
        with live.connect() as c:
            self.assertFalse(live._verified_seconds(live.frame(c,'1s',live.contract()),live.frame(c,'M1',live.contract())))

    def test_bridge_failure_visible_without_losing_collector_data(self):
        with patch.object(live,'ingest',side_effect=ValueError('private diagnostic')):
            r=self.client.post('/bars/1s',json=packet())
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['v3_data_bridge'],'ERROR')
        self.assertNotIn('private diagnostic',r.get_data(as_text=True))
        self.assertEqual(feed.status()['total_stored_seconds'],60)

    def test_release_live_gates_cannot_be_enabled_by_variables(self):
        os.environ['AB_V3_MODE']='LIVE'
        self.assertIn('release_not_live_validated',live.setting_blockers())
        with patch('requests.post') as post:
            live.dispatch_exits()
            post.assert_not_called()

    def test_many_source_triggers_do_not_hide_confirmed_candidates(self):
        import continuation_shadow as shadow
        with patch.object(shadow,'DB_PATH',Path(self.tmp.name)/'shadow.sqlite3'):
            shadow._init_db()
            with shadow._connect() as c:
                for i in range(151):
                    strategy='AB_DIRECTIONAL' if i==0 else 'CONTINUATION'
                    event='CANONICAL_OUTPUT' if i==0 else 'CLOSE_THROUGH'
                    body=json.dumps(dict(candidate_id=str(i),strategy=strategy))
                    c.execute('''INSERT INTO continuation_candidates
                        (candidate_id,strategy,event_kind,decision_ms,stage,status,payload_json,first_seen_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)''',(str(i),strategy,event,START+i,'TEST','TEST',body,'test','test'))
            app=Flask(__name__);live.register(app,route_callback=lambda:'test-route')
            rows=app.test_client().get('/ab/v3/data').json['candidates']
            self.assertEqual(sum(c.get('strategy')=='AB_DIRECTIONAL' for c in rows),1)
            self.assertEqual(len(rows),61)


class FeatureTests(unittest.TestCase):
    def data(self,direction='LONG'):
        start=pd.Timestamp('2026-01-01T12:00Z')
        idx=pd.date_range(start,periods=240,freq='s')
        price=100+np.linspace(0,.5,240)
        seconds=pd.DataFrame(dict(open=price,high=price+.1,low=price-.1,close=price,volume=1),index=idx)
        before=pd.date_range(start-pd.Timedelta(hours=25),start-pd.Timedelta(minutes=1),freq='min')
        m1=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=60.),index=before)
        m1=pd.concat([m1,seconds.resample('min').agg(dict(open='first',high='max',low='min',close='last',volume='sum'))])
        order=dict(direction=direction,fill_ms=int(start.timestamp()*1000),activation_ms=int(start.timestamp()*1000)-60000,
            entry_price=100.,risk_points=10.,snapshot=dict(source_level=98. if direction=='LONG' else 102.,
                fvg_edge=99. if direction=='LONG' else 101.,frozen_dol=dict(price=125. if direction=='LONG' else 75.)))
        return order,seconds,m1,start+pd.Timedelta(minutes=4)

    def test_actual_feature_evaluation_long_and_short_is_json_serializable(self):
        for direction in ('LONG','SHORT'):
            o,s,m,t=self.data(direction)
            result=policy.evaluate(o,s,m,t)
            self.assertIn('measurements',result)
            self.assertIn('rv',result['measurements'])
            self.assertFalse(result['exit'])
            json.dumps(result,allow_nan=False)

    def test_future_seconds_and_unclosed_m1_cannot_change_decision(self):
        o,s,m,t=self.data()
        expected=policy.evaluate(o,s,m,t)
        extra=pd.DataFrame(dict(open=150.,high=151.,low=149.,close=150.,volume=10),
            index=pd.date_range(t,periods=60,freq='s'))
        future=extra.resample('min').agg(dict(open='first',high='max',low='min',close='last',volume='sum'))
        self.assertEqual(policy.evaluate(o,pd.concat([s,extra]),pd.concat([m,future]),t),expected)

    def test_missing_second_holds_without_repair(self):
        o,s,m,t=self.data()
        self.assertEqual(policy.evaluate(o,s.drop(s.index[70]),m,t)['cause'],'HOLD_DATA_GAP')

    def test_unknown_prefill_fill_second_extreme_is_not_evidence(self):
        o,s,m,t=self.data()
        s.iloc[0,s.columns.get_loc('high')]=130
        r=policy.evaluate(o,s,m,t)
        self.assertFalse(r['measurements']['dol_delivered'])
        self.assertLess(r['measurements']['mfe'],.1)

    def test_closed_pre_fill_seconds_can_mark_dol_delivered(self):
        o,s,m,t=self.data()
        pre=pd.DataFrame(dict(open=100.,high=130.,low=99.,close=100.,volume=1.),
            index=[s.index[0]-pd.Timedelta(seconds=1)])
        r=policy.evaluate(o,pd.concat([pre,s]),m,t)
        self.assertTrue(r['measurements']['dol_delivered'])


if __name__ == '__main__':
    unittest.main()
