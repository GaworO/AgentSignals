import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from entry_sequence_fixture import fixture
from context_watch import derive_watch,evaluate,validate_watch
from entry_review import validate_decision
from entry_patterns import PROFILES
from daily_atr import calculate
from test_daily_atr import history


def watch_fixture(family=PROFILES[0],direction='long',mode='immediate_after_confirmation',future=False):
    p,d=fixture(family,direction,mode);source=p['as_of'];w=derive_watch(p,d)
    w.update(valid_for_seconds=900,entry_limit_rule='inverted_gap_edge',entry_anchor=None,
        stop_rule='retracement_extreme',stop_anchor=None)
    if future and family==PROFILES[1]:w['sequence_rule']['retracement_zone_id']=None
    d['entry_watch']=w
    row=dict(id='watch-'+direction,packet_id=p['packet_id'],cutoff=source,started=source+2,finished=source+25,status='validated',
        snapshot=json.dumps(dict(packet=p,frozen_at=source+1,processed_at=source+1)),decision=json.dumps(d))
    values=[(112,113,110,111),(111,112,109,110),(110,111,109,110),
        (110,112,109,110),(107,108,105,106),(106,107,105,106),(106,113,105,112)]
    rows=[dict(time=source+i*60,received_at=source+(i+1)*60,open=o,high=h,low=l,close=c) for i,(o,h,l,c) in enumerate(values)]
    if direction=='short':rows=[dict(b,open=300-b['open'],high=300-b['low'],low=300-b['high'],close=300-b['close']) for b in rows]
    markets=dict(MNQ=rows,NQ=deepcopy(rows),ES=[dict(b,open=60,high=61,low=59,close=60) for b in rows])
    cutoff=source+len(rows)*60
    atr=dict(symbol='MNQ',available_at=cutoff,complete=True,atr=100,range=40,source='synthetic',convention='synthetic test baseline')
    return row,markets,cutoff,atr


class ContextWatchTest(unittest.TestCase):
    def evaluate(self,row,m,t,atr):return evaluate(row,m,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr)

    def test_new_inversion_uses_saved_context_for_both_families_directions_modes(self):
        for family in PROFILES:
            for direction in ('long','short'):
                for mode in ('immediate_after_confirmation','retracement'):
                    with self.subTest(family=family,direction=direction,mode=mode):
                        row,m,t,atr=watch_fixture(family,direction,mode)
                        r=self.evaluate(row,m,t,atr)
                        self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))
                        self.assertEqual(r['packet']['as_of'],t)
                        self.assertNotEqual(r['selected']['selection']['trigger_event_id'],'trigger')
                        self.assertEqual(r['compiled']['plan']['context_provenance']['ai_run_id'],row['id'])
                        self.assertTrue(any(x['state']=='PLAN_READY' for x in r['candidate_evaluations']))
                        self.assertEqual(r['compiled']['plan']['entry_mode'],mode)

    def test_watch_can_wait_for_trigger_without_faking_confirmation_or_ai_entry(self):
        row,m,t,atr=watch_fixture();d=json.loads(row['decision']);d['entry_setup']=None
        d['context'].update(decision='wait',selected_trigger_id=None)
        d['context']['claims']['confirmation_accepted'].update(value=None,evidence_ids=[],reason='Waiting for a future inversion')
        d.update(initial_stop=None,invalidation=None,initial_quantity=None);d['management']['targets']=[]
        row['decision']=json.dumps(d);p=json.loads(row['snapshot'])['packet']
        self.assertTrue(validate_decision(p,d)['context_watch_verified'])
        r=self.evaluate(row,m,t,atr)
        self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))

    def test_each_new_opposite_inversion_is_blocked_by_saved_direction(self):
        row,m,t,atr=watch_fixture()
        values=[(112,113,110,111),(111,112,109,110),(110,111,109,110),
            (105,106,104,105),(109,110,108,109),(111,112,110,111),(110,111,104,105)]
        m['MNQ']=[dict(b,open=o,high=h,low=l,close=c) for b,(o,h,l,c) in zip(m['MNQ'],values)]
        r=self.evaluate(row,m,t,atr)
        self.assertEqual(r['state'],'CONTEXT_CANDIDATES_BLOCKED')
        self.assertIn('AI_CONTEXT_DIRECTION_CONFLICT',r['reasons'])
        self.assertTrue(r['candidate_evaluations'])

    def test_new_candidate_uses_current_atr_not_old_review_measurement(self):
        row,m,t,atr=watch_fixture();d=json.loads(row['decision']);d['entry_watch']['trade_kind']='continuation';row['decision']=json.dumps(d)
        self.assertEqual(self.evaluate(row,m,t,atr)['state'],'PLAN_READY')
        atr['range']=100;r=self.evaluate(row,m,t,atr)
        self.assertIsNone(r.get('compiled',{}).get('plan'))
        self.assertIn('DAILY_ATR_REACHED_CONTINUATION_BLOCKED',r['reasons'])

    def test_context_invalidation_target_gap_expiry_and_future_receipt_block(self):
        for cause in ('invalidation','target','gap','expiry','late-answer'):
            row,m,t,atr=watch_fixture()
            if cause=='invalidation':m['MNQ'][0]['low']=96
            elif cause=='target':m['MNQ'][0]['high']=125
            elif cause=='gap':m['MNQ'].pop(0)
            elif cause=='expiry':
                d=json.loads(row['decision']);d['entry_watch']['valid_for_seconds']=60;row['decision']=json.dumps(d)
            else:row['finished']=t+2
            r=self.evaluate(row,m,t,atr)
            self.assertNotEqual(r['state'],'PLAN_READY',cause)
            if cause=='expiry':self.assertEqual(r['reasons'],['AI_CONTEXT_EXPIRED'])

    def test_selected_stop_rule_and_frozen_target_are_not_nearest_level_proxies(self):
        row,m,t,atr=watch_fixture();r=self.evaluate(row,m,t,atr)
        self.assertEqual(r['compiled']['price_review']['target'],125)
        self.assertEqual(r['compiled']['price_review']['stop'],104.75)
        self.assertEqual(r['selected']['selection']['assessments']['confirmation_strength']['origin'],'saved_context_rule')

    def test_htf_watch_accepts_a_new_m15_gap_formed_after_the_review(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from entry_rules import fingerprint
        ny=ZoneInfo('America/New_York');stamp=lambda h,m:int(datetime(2026,10,8,h,m,tzinfo=ny).timestamp())
        row,_,_,_=watch_fixture(PROFILES[1]);p=json.loads(row['snapshot'])['packet'];d=json.loads(row['decision'])
        source=stamp(10,5);cutoff=stamp(10,19);p['as_of']=source
        p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
        d['context'].update(packet_id=p['packet_id'],as_of=source,decision='wait',selected_trigger_id=None)
        d.update(entry_setup=None,initial_stop=None,invalidation=None,initial_quantity=None);d['management']['targets']=[]
        d['entry_watch']['sequence_rule'].update(events_since=stamp(9,40),retracement_zone_id=None)
        row.update(cutoff=source,started=source+2,finished=source+25,packet_id=p['packet_id'],
            snapshot=json.dumps(dict(packet=p,frozen_at=source+1,processed_at=source+1)),decision=json.dumps(d))
        rows=[]
        values={stamp(10,15):(110,111,107,109),stamp(10,16):(106,106,104,105),
            stamp(10,17):(104.75,105,104,104.75),stamp(10,18):(105,112,105,111)}
        for t in range(stamp(9,30),cutoff,60):
            o,h,l,c=values.get(t,(102,104,100,102) if t<stamp(9,45) else (104,111,103,110) if t<stamp(10,0) else (111,113,108,112))
            rows.append(dict(time=t,received_at=t+60,open=o,high=h,low=l,close=c))
        m=dict(MNQ=rows,NQ=deepcopy(rows),ES=[dict(b,open=60,high=61,low=59,close=60) for b in rows])
        atr=dict(symbol='MNQ',available_at=cutoff,complete=True,atr=100,range=40,source='synthetic',convention='synthetic')
        r=self.evaluate(row,m,cutoff,atr)
        self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))
        selected=r['selected']['selection']['setup_sequence']['retracement_zone_id']
        self.assertGreater(r['packet']['evidence'][selected]['available_at'],source)

    def test_newest_unarmed_review_supersedes_a_previously_armed_watch(self):
        row,m,t,atr=watch_fixture();d=json.loads(row['decision']);d['entry_watch']['armed']=False
        row['decision']=json.dumps(d)
        r=self.evaluate(row,m,t,atr)
        self.assertEqual(r['state'],'AI_CONTEXT_BLOCKED');self.assertEqual(r['reasons'],['AI_CONTEXT_WATCH_NOT_ARMED'])
        self.assertTrue(all(c['state']=='AI_CONTEXT_BLOCKED' for c in r['candidate_evaluations']))

    def test_observer_background_path_with_real_calculated_atr_and_restart(self):
        from store import Store
        from selection_observer import SelectionObserver
        from ai_review import AIReview
        from paper_lifecycle import PaperLifecycle
        from datetime import datetime
        from zoneinfo import ZoneInfo
        row,m,t,atr=watch_fixture(mode='retracement');source=row['cutoff']
        long_history=history(t)
        lookup={b['time']:b for b in long_history};lookup.update({b['time']:b for b in m['MNQ']});m['MNQ']=[lookup[k] for k in sorted(lookup)]
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);AIReview(store,{})
            with store.connect() as db:
                for sym,rows in m.items():
                    for b in rows:
                        payload=dict(symbol=sym,bar_open_ms=b['time']*1000,bar_close_ms=(b['time']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                        db.execute(f'INSERT INTO {"collection_bars" if sym=="NQ" else "bars"} VALUES(?,?,?,?,?)',(sym,b['time'],b['time']+60,b['received_at'],json.dumps(payload)))
                keys=list(row);db.execute('INSERT INTO ai_runs('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])
            Path(root,'account_snapshot.json').write_text(json.dumps(dict(balance=50000,minimum_balance=48000,target_balance=53000,daily_loss_remaining=1000,committed_risk_usd=0,committed_micro_contracts=0,as_of='synthetic',reported_at=source)))
            o=SelectionObserver(store,{})
            date=datetime.fromtimestamp(t,ZoneInfo('America/New_York')).isoformat()
            o.news.refresh(fetch=lambda:[dict(title='Synthetic',country='EUR',impact='Low',date=date)],clock=lambda:t)
            with patch('ai_review.call_openai',side_effect=AssertionError('Unexpected paid call')):
                self.assertTrue(o.process_one(t+1));r=o.audit(o.revision,t)
                self.assertEqual(r['state'],'PLAN_READY',r['reasons']);self.assertTrue(r['daily_atr']['complete'])
                self.assertFalse(SelectionObserver(store,{}).process_one(t+2))
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
            sim.submit('s',decision=r,now=t+2)
            sim.quote('fill',at=t+3,received_at=t+3,market_price=r['compiled']['plan']['entry_reference']['price'])
            self.assertEqual(sim.snapshot()['runs'][0]['state'],'OPEN')


if __name__=='__main__':unittest.main()
