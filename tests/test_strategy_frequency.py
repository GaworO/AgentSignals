"""Focused guards against falsely reporting model frequency or profitability."""
import copy
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from services.tanja.ict_agent.strategy_frequency import projected,candidate_projected,exposure_group,FrequencyOnlyKnowledge,verify_event_batch
from services.tanja.ict_agent.scenarios import update_setups

class FrequencyTests(unittest.TestCase):
    def test_same_close_reference_not_list_order_and_no_future(self):
        events=[{'id':'equal','available_at':60,'evidence_ids':['pivot']},
                {'id':'pivot','available_at':60,'evidence_ids':[]}]
        self.assertEqual(verify_event_batch(events,{},60),1)
        events[1]['available_at']=120
        with self.assertRaisesRegex(ValueError,'FUTURE_EVENT'):verify_event_batch(events,{},60)
        events[1]['available_at']=61
        with self.assertRaisesRegex(ValueError,'MISSING_OR_FUTURE'):verify_event_batch(events,{},61)

    def test_source_window_is_half_open_and_ny_dst_aware(self):
        row={'directions':['SHORT'],'geometric_projection':{'event_kinds':['FVG'],'session_ny':{'start':'10:00','end':'11:00'}}}
        for date in ('2026-05-20','2026-11-23'):
            at=int(datetime.fromisoformat(date+'T10:30').replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
            e={'kind':'FVG','direction':'SHORT','available_at':at,'timeframe':1}
            self.assertTrue(projected(e,row));e['available_at']=at+1800;self.assertFalse(projected(e,row))

    def test_ifvg_timeframes_do_not_cross_promote(self):
        row={'directions':['LONG'],'geometric_projection':{'event_kinds':['IFVG'],'timeframes':[5]}}
        e={'kind':'IFVG','direction':'LONG','timeframe':1,'available_at':1000}
        self.assertFalse(projected(e,row));e['timeframe']=5;self.assertTrue(projected(e,row))
        e['kind']='FVG';self.assertFalse(projected(e,row))

    def test_ote_and_ifvg_are_not_sweep_selector_candidates(self):
        setup={'direction':'SHORT','created_at':1000,'plan':{'entry':100},'sequence':['FVG']}
        self.assertFalse(candidate_projected(setup,{'family':'ote','geometric_projection':{'event_kinds':['STRUCTURE_BREAK']}}))
        self.assertFalse(candidate_projected(setup,{'family':'ifvg','geometric_projection':{'event_kinds':['IFVG']}}))

    def test_nasdaq_clusters_retain_direction_and_time(self):
        a=exposure_group('NQ','FVG',600,'LONG',1)
        self.assertEqual(a,exposure_group('MNQ','FVG',600,'LONG',1))
        self.assertNotEqual(a,exposure_group('ES','FVG',600,'LONG',1))
        self.assertNotEqual(a,exposure_group('MNQ','FVG',600,'SHORT',1))
        self.assertNotEqual(a,exposure_group('MNQ','FVG',660,'LONG',1))

    def test_frequency_adapter_does_not_relax_m5_root(self):
        event=dict(id='root',symbol='NQ',contract='test',available_at=600,direction='LONG',kind='SWEEP',timeframe=1,level_timeframe=1,price=100,extreme=99)
        ctx={'NQ':{'latest':{'close':100},'contract':'test','stale':False,'ranges':{}}}
        setups={};update_setups(setups,[event],ctx,600,FrequencyOnlyKnowledge(),('NQ',))
        self.assertEqual(setups,{})
        event['level_timeframe']=5;update_setups(setups,[event],ctx,600,FrequencyOnlyKnowledge(),('NQ',))
        self.assertEqual(len(setups),1);s=next(iter(setups.values()))
        self.assertEqual(s['source_rule_ids'],[]);self.assertIsNone(s['plan'])

if __name__=='__main__': unittest.main()
