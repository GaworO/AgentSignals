"""Synthetic whole-history sequences; no model calls, broker or market labels."""
from copy import deepcopy
from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo
from entry_patterns import PROFILES
from entry_rules import fingerprint
from context_watch import derive_watch,evaluate,validate_watch
from setup_scanner import SequenceScanner,attach
from test_nq_analysis import nq_fixture


def market_fixture(direction='long'):
    stamp=lambda h,m:int(datetime(2026,10,8,h,m,tzinfo=ZoneInfo('America/New_York')).timestamp())
    cutoff=stamp(10,19);rows=[]
    special={stamp(9,15):(102,104,97,102),stamp(9,20):(102,108,100,102),
        stamp(9,44):(102,104,96,102),stamp(10,15):(110,111,107,109),
        stamp(10,16):(106,106,104,105),stamp(10,17):(104.75,105,104,104.75),
        stamp(10,18):(105,112,105,111)}
    for t in range(stamp(5,0),cutoff,60):
        o,h,l,c=special.get(t,(102,104,100,102) if t<stamp(9,45)
            else (104,111,103,110) if t<stamp(10,0) else (111,113,108,112))
        if direction=='short':o,h,l,c=300-o,300-l,300-h,300-c
        rows.append(dict(time=t,received_at=t+60,open=o,high=h,low=l,close=c))
    nq=[dict(b,**{k:b[k]+7.25 for k in ('open','high','low','close')}) for b in rows]
    es=[dict(b,open=60,high=61,low=59,close=60) for b in rows]
    return dict(ES=es,NQ=nq,MNQ=rows),cutoff,stamp(10,5)


def approved_watch(family,direction,mode):
    p,d=nq_fixture(family,direction,mode);markets,cutoff,source=market_fixture(direction)
    watch=derive_watch(p,d)
    watch.update(valid_for_seconds=900,sequence_selector='two_family_v1',sequence_rule=None,
        stop_rule='retracement_extreme',stop_anchor=None,entry_limit_rule='confirmation_open',entry_anchor=None)
    p['as_of']=source;p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
    d['context'].update(packet_id=p['packet_id'],as_of=source,decision='wait',selected_trigger_id=None)
    d['context']['claims']['confirmation_accepted'].update(value=None,evidence_ids=[],reason='Waiting for M1')
    d.update(entry_setup=None,entry_watch=watch,initial_stop=None,invalidation=None,initial_quantity=None)
    d['management']['targets']=[]
    row=dict(id='synthetic-context',packet_id=p['packet_id'],cutoff=source,started=source+2,finished=source+25,
        status='validated',snapshot=json.dumps(dict(packet=p,frozen_at=source+1,processed_at=source+1)),decision=json.dumps(d))
    return row,markets,cutoff


class SetupScannerTests(unittest.TestCase):
    def test_both_families_directions_and_optional_entries_keep_separate_prices(self):
        for family in PROFILES:
            for direction in ('long','short'):
                for mode in ('immediate_after_confirmation','retracement'):
                    with self.subTest(family=family,direction=direction,mode=mode):
                        row,markets,t=approved_watch(family,direction,mode)
                        atr=dict(symbol='MNQ',available_at=t,complete=True,atr=100,range=40,source='synthetic',convention='synthetic')
                        r=evaluate(row,markets,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr,analysis_symbol='NQ')
                        self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))
                        plan=r['compiled']['plan'];self.assertEqual(plan['entry_reference']['symbol'],'MNQ')
                        self.assertEqual(plan['setup_profile'],family);self.assertTrue(plan['setup_id'])
                        self.assertEqual(r['compiled']['price_review']['target'],125 if direction=='long' else 175)
                        self.assertEqual(plan['entry_reference']['price'],(111 if direction=='long' else 189)
                            if mode=='immediate_after_confirmation' else (105 if direction=='long' else 195))
                        self.assertFalse(r['orders_enabled']);self.assertEqual(r['ai_context']['state'],'PERMITTED')

    def test_discovery_does_not_reinterpret_a_legacy_selected_anchor_watch(self):
        p,d=nq_fixture();w=derive_watch(p,d)
        w.pop('sequence_selector')
        self.assertTrue(validate_watch(p,d,w))
        w.update(sequence_selector='two_family_v1')
        with self.assertRaisesRegex(ValueError,'NULL_EXPLICIT_SEQUENCE'):validate_watch(p,d,w)

    def test_future_receipts_and_future_bars_cannot_change_current_scanner(self):
        markets,t,_=market_fixture();s=SequenceScanner(markets,t,t+1)
        e=next(x for x in s.events if x['direction']=='long')
        before=[s.sequence(e,f) for f in PROFILES]
        for rows in markets.values():rows.append(dict(rows[-1],time=t,received_at=t+60,close=999,high=999))
        future=SequenceScanner(markets,t,t+1)
        self.assertEqual(before,[future.sequence(e,f) for f in PROFILES])
        # A missing confirmation cannot be recreated from a later receipt.
        markets['NQ'][-2]['received_at']=t+2
        self.assertEqual(SequenceScanner(markets,t,t+1).events,[])

    def test_evidence_is_immutable_and_all_dependencies_are_causal(self):
        from context_v3 import build_v3_packet,check_packet
        from tanya_replay import Bar
        markets,t,_=market_fixture()
        data={s:[Bar(**{k:b[k] for k in ('time','open','high','low','close')}) for b in rows] for s,rows in markets.items()}
        p=build_v3_packet(data,t,max_bars=64);old=deepcopy(p['evidence'])
        attach(p,markets,cutoff=t,now=t+1);check_packet(p)
        self.assertTrue(all(p['evidence'][k]==v for k,v in old.items()))
        records=p['setup_scanner']['candidates']
        self.assertEqual({x['setup_profile'] for x in records if x['state']=='SEQUENCE_MEASURED_NEEDS_AI_CONTEXT'},set(PROFILES))
        self.assertTrue(all(x['executable'] is False for x in records))

    def test_wait_expiry_invalidation_and_atr_still_block_scanned_entries(self):
        for cause in ('unarmed','expired','invalidated','atr'):
            row,m,t=approved_watch(PROFILES[0],'long','immediate_after_confirmation')
            d=json.loads(row['decision']);atr=dict(symbol='MNQ',available_at=t,complete=True,atr=100,range=40,source='synthetic',convention='synthetic')
            if cause=='unarmed':d['entry_watch']['armed']=False
            elif cause=='expired':d['entry_watch']['valid_for_seconds']=60
            elif cause=='invalidated':m['NQ'][-1]['low']=90
            else:d['entry_watch']['trade_kind']='continuation';atr['range']=100
            row['decision']=json.dumps(d)
            r=evaluate(row,m,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr,analysis_symbol='NQ')
            self.assertNotEqual(r['state'],'PLAN_READY',cause)


if __name__=='__main__':unittest.main()
