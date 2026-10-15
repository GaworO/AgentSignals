"""Distinct synthetic NQ/MNQ series; no relabelled historical market data."""
from copy import deepcopy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from entry_sequence_fixture import fixture
from entry_review import validate_decision,compile_review
from entry_rules import fingerprint
from entry_patterns import PROFILES
from store import Store
from worker import process_one
from test_service import bar
from paper_lifecycle import PaperLifecycle


def nq_fixture(family=PROFILES[0],direction='long',mode='immediate_after_confirmation'):
    p,d=fixture(family,direction,mode);mapping={k:'NQ:'+k for k,e in p['evidence'].items() if e['symbol']=='MNQ'}
    for key,new in mapping.items():
        e=deepcopy(p['evidence'][key]);e['symbol']='NQ'
        for field in ('open','high','low','close','price','lower','upper'):
            if field in e:e[field]+=7.25
        if 'evidence_ids' in e:e['evidence_ids']=[mapping.get(ref,ref) for ref in e['evidence_ids']]
        p['evidence'][new]=e
    p['market_roles']=dict(analysis_inputs=['ES','NQ'],execution_symbol='MNQ',execution_prices_present=True)
    p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
    def refs(obj):
        if isinstance(obj,dict):return {k:refs(v) for k,v in obj.items()}
        if isinstance(obj,list):return [refs(v) for v in obj]
        return mapping.get(obj,obj) if isinstance(obj,str) else obj
    d['context']=refs(d['context']);d['context']['packet_id']=p['packet_id'];d['analysis_symbol']='NQ'
    d['poi_evidence_ids']=refs(d['poi_evidence_ids']);d['selected_swing']=refs(d['selected_swing'])
    d['invalidation']=refs(d['invalidation'])
    d['entry_setup']['setup_sequence']=refs(d['entry_setup']['setup_sequence'])
    d['entry_setup']['assessments']=refs(d['entry_setup']['assessments'])
    return p,d


class NQAnalysisTests(unittest.TestCase):
    def test_all_eight_entries_analyze_nq_but_every_order_price_is_mnq(self):
        for family,direction,mode in itertools.product(PROFILES,('long','short'),('immediate_after_confirmation','retracement')):
            with self.subTest(family=family,direction=direction,mode=mode):
                p,d=nq_fixture(family,direction,mode)
                self.assertTrue(validate_decision(p,d)['entry_setup_verified'])
                r=compile_review(p,d,selected_at=p['as_of']+1,now=p['as_of']+1,budget=250,cap=40)
                self.assertEqual(r['state'],'PLAN_READY',r['reasons']);plan=r['compiled']['plan']
                self.assertEqual(r['selected']['selection']['analysis_symbol'],'NQ')
                self.assertTrue(r['compiled']['analysis_source_matches_tanja'])
                for point in (plan['entry_reference'],plan['invalidation'],plan['target']['anchor']):self.assertEqual(point['symbol'],'MNQ')
                self.assertEqual(plan['thesis_invalidation']['symbol'],'NQ')
                self.assertNotEqual(p['evidence']['trigger-bar']['close'],p['evidence']['NQ:trigger-bar']['close'])

    def test_missing_execution_bar_or_nq_order_price_cannot_pass(self):
        for mutation in ('missing-MNQ','NQ-stop','NQ-target','NQ-limit'):
            p,d=nq_fixture(mode='retracement')
            if mutation=='missing-MNQ':del p['evidence']['trigger-bar']
            elif mutation=='NQ-stop':d['initial_stop']['anchor']['evidence_id']='NQ:'+d['initial_stop']['anchor']['evidence_id']
            elif mutation=='NQ-target':d['management']['targets'][0]['anchor']['evidence_id']='NQ:'+d['management']['targets'][0]['anchor']['evidence_id']
            else:d['entry_setup']['entry_anchor']['evidence_id']='NQ:'+d['entry_setup']['entry_anchor']['evidence_id']
            p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});d['context']['packet_id']=p['packet_id']
            with self.assertRaises(ValueError,msg=mutation):validate_decision(p,d)

    def test_new_nq_inversions_reuse_nq_context_and_resolve_mnq_prices(self):
        from context_watch import derive_watch,evaluate
        from test_context_watch import watch_fixture
        for family,direction,mode in itertools.product(PROFILES,('long','short'),('immediate_after_confirmation','retracement')):
            with self.subTest(family=family,direction=direction,mode=mode):
                p,d=nq_fixture(family,direction,mode);source=p['as_of'];w=derive_watch(p,d)
                w.update(valid_for_seconds=900,entry_limit_rule='confirmation_open',entry_anchor=None,stop_rule='retracement_extreme',stop_anchor=None)
                d['entry_watch']=w
                row,m,t,atr=watch_fixture(family,direction,mode)
                for b in m['NQ']:
                    for k in ('open','high','low','close'):b[k]+=7.25
                row.update(packet_id=p['packet_id'],snapshot=json.dumps(dict(packet=p,frozen_at=source+1,processed_at=source+1)),decision=json.dumps(d))
                r=evaluate(row,m,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr,analysis_symbol='NQ')
                self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))
                self.assertTrue(all(c['candidate_id'].startswith('NQ:') for c in r['candidate_evaluations']))
                self.assertEqual(r['compiled']['plan']['entry_reference']['symbol'],'MNQ')
                self.assertEqual(r['ai_context']['invalidation_symbol'],'NQ')
                # A legacy approved MNQ review cannot authorize this new pipeline.
                old,m,t,atr=watch_fixture(family,direction,mode)
                blocked=evaluate(old,m,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr,analysis_symbol='NQ')
                self.assertEqual(blocked['ai_context']['reasons'],['AI_ANALYSIS_SOURCE_MISMATCH'])

    def test_all_feed_arrival_orders_require_three_distinct_series(self):
        for order in itertools.permutations(('ES','NQ','MNQ')):
            with tempfile.TemporaryDirectory() as root:
                s=Store(root,analysis_symbol='NQ');queued=0
                for symbol in order:
                    r=s.ingest(bar(symbol,1779807600,(200,202,199,201) if symbol=='NQ' else (100,102,99,101)),1779807601)
                    queued+=r['queued']
                self.assertEqual(queued,1);process_one(s,1779807602)
                p=s.state(1779807603)['latest_context']['packet']
                self.assertEqual(p['market_roles']['analysis_inputs'],['ES','NQ'])
                self.assertEqual(p['evidence']['NQ:M1:1779807540']['close'],201)
                self.assertEqual(p['evidence']['MNQ:M1:1779807540']['close'],101)

    def test_old_jobs_and_audits_are_preserved_and_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            old=Store(root)
            for symbol in ('ES','MNQ'):old.ingest(bar(symbol,1779807600),1779807601)
            process_one(old,1779807602);saved=old.state(1779807603)['latest_context']
            new=Store(root,analysis_symbol='NQ')
            self.assertIsNone(new.state(1779807603)['latest_context'])
            self.assertEqual(Store(root).state(1779807603)['latest_context'],saved)

    def test_ai_prompt_and_history_gate_require_nq_not_mnq_substitution(self):
        from ai_review import AIReview
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,analysis_symbol='NQ')
            ai=AIReview(s,dict(TANJA_AI_ENABLED='true',OPENAI_API_KEY='synthetic',OPENAI_MODEL='synthetic'))
            p,d=nq_fixture();text=ai.make_payload(p)['input'][0]['content']
            self.assertIn('analyze ES and NQ',text);self.assertIn('never copy an NQ price',text)
            context=dict(packet=p,frozen_at=p['as_of'],processed_at=p['as_of'],
                coverage={symbol:{'240':3,'60':3} for symbol in ('ES','MNQ')})
            self.assertEqual(ai.gate(p['as_of']+1,context),'WAITING_FOR_HISTORY')

    def test_smt_legacy_wire_name_references_actual_nq_windows(self):
        from context_geometry import measure_smt
        p,d=nq_fixture();t=p['as_of'];ev=p['evidence']
        for phase,start,end in (('reference',t-120,t-60),('current',t-60,t)):
            for symbol in ('ES','NQ','MNQ'):
                value=100 if phase=='reference' else 99 if symbol=='ES' else 101
                ev[phase+symbol]=dict(kind='window',symbol=symbol,start=start,end=end,available_at=end,complete=True,
                    low=value,high=value+10,low_times=[start],high_times=[start])
        selected=dict(reference_es='referenceES',current_es='currentES',reference_mnq='referenceNQ',current_mnq='currentNQ',side='low')
        result=measure_smt(p,selected,'long')
        self.assertTrue(result['intact']);self.assertIn('NQ',result['extrema']);self.assertNotIn('MNQ',result['extrema'])
        selected['current_mnq']='currentMNQ'
        with self.assertRaises(ValueError):measure_smt(p,selected,'long')

    def test_three_market_packet_retains_analysis_without_exceeding_ai_budget(self):
        import math
        from context_v3 import build_v3_packet
        from tanya_replay import Bar
        from ai_review import AIReview,encoded,MAX_REQUEST_BYTES
        cutoff=1791466860;rows=[]
        for i,t in enumerate(range(cutoff-12000*60,cutoff,60)):
            v=1000+round((math.sin(i*.14)*8+math.sin(i*.039)*30)*4)/4
            rows.append(Bar(t,v,v+2,v-2,v+.25))
        p=build_v3_packet({s:rows for s in ('ES','NQ','MNQ')},cutoff)
        self.assertEqual(p['coverage']['NQ']['retained_bars_per_timeframe']['1'],80)
        self.assertEqual(p['coverage']['MNQ']['retained_bars_per_timeframe']['1'],32)
        self.assertTrue(all(e['kind'] in ('bar','window','level') for e in p['evidence'].values() if e['symbol']=='MNQ'))
        with tempfile.TemporaryDirectory() as root:
            ai=AIReview(Store(root,analysis_symbol='NQ'),{})
            self.assertLess(len(encoded(ai.make_payload(p)).encode()),MAX_REQUEST_BYTES)

    def test_default_app_uses_nq_and_legacy_mode_is_explicit(self):
        from app import App
        with tempfile.TemporaryDirectory() as root:
            a=App(dict(DATA_DIR=root,TANJA_FEED_TOKEN='a'*40,TANJA_DASHBOARD_PASSWORD='b'*20))
            self.assertEqual(a.store.analysis_symbol,'NQ')

    def test_pending_entry_requires_fresh_nq_quote_and_cancels_nq_invalidation(self):
        p,d=nq_fixture();now=p['as_of'];r=compile_review(p,d,selected_at=now+1,now=now+1,budget=250,cap=40)
        r.update(frozen_at=now+1,ai_context=dict(state='PERMITTED',direction='long'),scheduled_news=dict(state='CLEAR'),
            news_entry_reasons=[],account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
        with tempfile.TemporaryDirectory() as root:
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
            sim.submit('submit',decision=r,now=now+1);entry=r['compiled']['plan']['entry_reference']['price']
            self.assertEqual(sim.quote('missing',at=now+2,received_at=now+2,market_price=entry)['result'],'WAITING_FOR_FRESH_NQ_QUOTE')
            result=sim.quote('invalid',at=now+3,received_at=now+3,market_price=entry,nq_quote=dict(at=now+3,price=r['compiled']['plan']['thesis_invalidation']['price']))
            self.assertEqual(result['result'],'NQ_THESIS_INVALIDATED');self.assertEqual(sim.snapshot()['orders_sent'],0)


if __name__=='__main__':unittest.main()
