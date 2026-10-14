import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_fixture import lower_timeframe_fixture
from auto_selector import select, Policy
from entry_rules import compile_entry
from paper_lifecycle import PaperLifecycle
import source_rules as rules


def source_decision(markets=None,t=None):
    if markets is None:markets,t=lower_timeframe_fixture()
    d=select(markets,cutoff=t,now=t+1,risk_budget_usd=250,max_contracts=40,
        policy=Policy(discovery_only=True,timeframe_priority=(1,2,3,5)))
    s=d['selected']['selection'];p=d['packet']
    s.update(confirmation_policy=rules.VERSION,setup_profile='selected_inversion',selection_origin='explicit_context_review')
    s['assessments']={field:dict(accepted=True,reason='Explicit synthetic context annotation, not a historical AI decision',
        origin='human_review',available_at=t,evidence_ids=[s['trigger_bar_id']])
        for field in ('setup_context','confirmation_strength','entry_timing','stop_invalidation','target_priority')}
    # Explicit selections for this fixture, not automatic preferred prices.
    s['initial_stop']['reason']='Selected invalidation below the fixture liquidity sweep'
    s['target']['reason']='Selected fixture upside liquidity objective'
    entry=p['evidence'][s['entry_reference']['evidence_id']]['close']
    objective=dict(symbol='MNQ',price=entry+3,available_at=t,source='synthetic-local-objective',direction='above')
    s['management_spec']=dict(version=rules.VERSION,trail='explicit_confirmed_swing',additions='separate_review_required',
        partials=[dict(id='local',quantity=2,objective=objective,reason='Synthetic two-contract selection')])
    compiled=compile_entry(p,s,d['selected']['observations'],now=t+1)
    if not compiled['plan']:raise AssertionError(compiled)
    d.update(compiled=compiled,state='PLAN_READY',frozen_at=t+1,
        ai_context=dict(state='PERMITTED',direction='long'),scheduled_news=dict(state='CLEAR'),news_entry_reasons=[],
        account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
    return d,t,objective,markets


class SourceRulesTests(unittest.TestCase):
    def test_m1_discovery_cannot_become_an_automatic_trade(self):
        markets,t=lower_timeframe_fixture()
        d=select(markets,cutoff=t,now=t+1,risk_budget_usd=250,max_contracts=40,
            policy=Policy(discovery_only=True,timeframe_priority=(1,2,3,5)))
        self.assertEqual(d['state'],'NEEDS_SOURCE_SELECTION')
        self.assertEqual(d['selected']['selection']['timeframe_minutes'],1)
        self.assertIsNone(d['compiled']['plan'])

    def test_explicit_m1_compiles_without_universal_m5_rule(self):
        d,t,_,_=source_decision()
        self.assertEqual(d['compiled']['plan']['strategy_policy'],rules.VERSION)
        s=d['selected']['selection'];s['setup_profile']='m5_momentum'
        self.assertIn('PROFILE_TIMEFRAME_MISMATCH',compile_entry(d['packet'],s,d['selected']['observations'],now=t+1)['missing'])

    def test_weak_or_future_strength_annotation_blocks_otherwise_valid_geometry(self):
        for change in (dict(accepted=False),dict(available_at=10**12),dict(evidence_ids=['missing'])):
            d,t,_,_=source_decision();s=d['selected']['selection']
            s['assessments']['confirmation_strength'].update(change)
            r=compile_entry(d['packet'],s,d['selected']['observations'],now=t+1)
            self.assertIsNone(r['plan']);self.assertIn('CONFIRMATION_STRENGTH_REVIEW_REQUIRED',r['missing'])

    def test_missing_target_review_cannot_use_nearest_level_default(self):
        d,t,_,_=source_decision();s=d['selected']['selection'];del s['assessments']['target_priority']
        self.assertIsNone(compile_entry(d['packet'],s,d['selected']['observations'],now=t+1)['plan'])

    def test_es_selected_objective_two_contracts_not_twenty_percent(self):
        spec=dict(version=rules.VERSION,trail='explicit_confirmed_swing',additions='separate_review_required',
            partials=[dict(id='es-weekly-low',quantity=2,reason='Case-specific reduction',
                objective=dict(symbol='ES',price=7363.25,available_at=100,source='Selected previous weekly low',direction='below'))])
        args=dict(spec=spec,position_id='May19-component',remaining_quantity=4,completed=[],now=200)
        # A new session low above the selected weekly low must not trigger.
        self.assertEqual(rules.partial_proposal(**args,quotes={'ES':dict(at=200,price=7366)})['action'],'NONE')
        got=rules.partial_proposal(**args,quotes={'ES':dict(at=200,price=7363.25)})
        self.assertEqual(got['quantity'],2)
        args['completed']=['es-weekly-low']
        self.assertEqual(rules.partial_proposal(**args,quotes={'ES':dict(at=200,price=7360)})['action'],'NONE')

    def test_partial_needs_fresh_quote_and_preserves_runner(self):
        d,t,objective,_=source_decision();spec=d['compiled']['plan']['management_spec']
        args=dict(spec=spec,position_id='p',remaining_quantity=3,completed=[],now=t+10)
        for quote in (dict(at=t+11,price=objective['price']),dict(at=t+1,price=objective['price'])):
            self.assertEqual(rules.partial_proposal(**args,quotes={'MNQ':quote})['action'],'NONE')
        args['remaining_quantity']=2
        self.assertEqual(rules.partial_proposal(**args,quotes={'MNQ':dict(at=t+10,price=objective['price'])})['action'],'NONE')

    def test_structural_trail_waits_for_selected_new_high_and_mirrors(self):
        bars=[dict(time=120,open=103,high=104,low=100,close=102),
              dict(time=180,open=102,high=106,low=101,close=105)]
        selection=dict(selected_at=240,anchor_time=120,confirmation_time=180,buffer_ticks=2,
            confirmation='break_prior_extreme',prior_extreme=dict(price=104.5,available_at=100,source='selected earlier high'),reason='Dip then new high')
        for direction in ('long','short'):
            sign=1 if direction=='long' else -1;rows=deepcopy(bars);s=deepcopy(selection)
            if sign==-1:
                rows=[dict(b,open=210-b['open'],high=210-b['low'],low=210-b['high'],close=210-b['close']) for b in rows]
                s['prior_extreme']['price']=210-s['prior_extreme']['price']
            args=dict(selection=s,bars=rows,direction=direction,opened_at=60,now=240,current_stop=98 if sign==1 else 112,market_price=106 if sign==1 else 104)
            self.assertEqual(rules.structural_stop(**args)['stop_price'],99.5 if sign==1 else 110.5)
            args['now']=239
            self.assertEqual(rules.structural_stop(**args)['action'],'NONE')
            args['now']=240;args['selection']=None
            self.assertEqual(rules.structural_stop(**args)['action'],'NONE')

    def test_default_lifecycle_consumes_source_plan_and_exact_selected_partial(self):
        d,t,objective,_=source_decision()
        with tempfile.TemporaryDirectory() as folder:
            sim=PaperLifecycle(Path(folder)/'source.sqlite3',slippage_points=.25,fee_per_side=.5)
            sim.submit('entry',decision=d,objective=objective,now=t+1)
            entry=d['compiled']['plan']['entry_reference']['price']
            sim.quote('fill',at=t+2,received_at=t+2,market_price=entry)
            run=sim.snapshot()['runs'][0];base=run['quantity']
            r=sim.quote('partial',at=t+3,received_at=t+3,market_price=objective['price'])
            self.assertEqual(r['pending']['quantity'],2)
            sim.quote('partial-fill',at=t+4,received_at=t+4,market_price=objective['price'])
            self.assertEqual(sim.snapshot()['runs'][0]['quantity'],base-2)
            sim.quote('same-level',at=t+5,received_at=t+5,market_price=objective['price'])
            self.assertIsNone(sim.snapshot()['runs'][0]['pending'])
            sim.quote('target',at=t+6,received_at=t+6,market_price=run['target'])
            self.assertEqual(sim.snapshot()['runs'][0]['state'],'CLOSED')
            self.assertEqual(sim.snapshot()['orders_sent'],0)

    def test_source_lifecycle_does_not_accept_relabelled_legacy_plan(self):
        from test_paper_lifecycle import observer_decision
        d,t=observer_decision();p=d['compiled']['plan'];p['strategy_policy']=rules.VERSION
        with tempfile.TemporaryDirectory() as folder:
            sim=PaperLifecycle(Path(folder)/'reject.sqlite3',slippage_points=0,fee_per_side=0)
            with self.assertRaises(ValueError):sim.submit('s',decision=d,objective={},now=t+1)

    def test_default_observer_requires_source_review(self):
        from test_selection_observer import SelectionObserverTests
        from selection_observer import SelectionObserver
        case=SelectionObserverTests();case.setUp()
        try:
            case.add_review();observer=SelectionObserver(case.s,{})
            observer.process_one(case.t+1)
            d=observer.audit(observer.revision,case.t)
            self.assertEqual(d['state'],'NEEDS_SOURCE_SELECTION')
            self.assertIsNone(d['compiled']['plan'])
        finally:case.doCleanups()

    def test_explicit_review_passes_observer_gates_and_enters_source_simulator(self):
        from test_selection_observer import SelectionObserverTests
        from selection_observer import SelectionObserver
        case=SelectionObserverTests();case.setUp()
        try:
            case.add_review();observer=SelectionObserver(case.s,{})
            d,t,objective,_=source_decision(case.m,case.t)
            source_review=dict(selection=d['selected']['selection'],observations=d['selected']['observations'])
            observer.process_one(t+1,source_review=source_review)
            got=observer.audit(observer.revision,t)
            self.assertEqual(got['state'],'PLAN_READY',got.get('reasons'))
            sim=PaperLifecycle(Path(case.tmp.name)/'positive.sqlite3',slippage_points=0,fee_per_side=0)
            sim.submit('entry',decision=got,objective=objective,now=t+1)
            self.assertEqual(sim.snapshot()['runs'][0]['state'],'ENTRY_PENDING')
        finally:case.doCleanups()

    def test_future_mutation_cannot_change_structural_decision(self):
        rows=[dict(time=120,open=103,high=104,low=100,close=102),dict(time=180,open=102,high=106,low=101,close=105)]
        selection=dict(selected_at=240,anchor_time=120,confirmation_time=180,buffer_ticks=1,confirmation='rebound',reason='Selected recovery')
        args=dict(selection=selection,direction='long',opened_at=60,now=240,current_stop=98,market_price=106)
        before=rules.structural_stop(bars=rows,**args)
        after=rules.structural_stop(bars=rows+[dict(time=240,open=105,high=1000,low=1,close=900)],**args)
        self.assertEqual(before,after)
        # Missing intervening minutes cannot prove that the selected anchor held.
        args['now']=360
        self.assertEqual(rules.structural_stop(bars=rows,**args)['action'],'NONE')


class TeachingLibraryTests(unittest.TestCase):
    def test_only_earlier_sessions_are_supplied(self):
        from decision_library import teaching_context,library
        at=int(datetime(2026,5,22,10,tzinfo=ZoneInfo('America/New_York')).timestamp())
        text=teaching_context(at)
        self.assertIn('5_19-es-partial',text)
        self.assertNotIn('5_22-m5-momentum',text)
        self.assertNotIn('10_2-headline-adds',text)
        self.assertEqual(library()['unseen_holdout_sessions'],[])

    def test_payload_uses_teaching_library_without_changing_market_packet(self):
        from ai_review import AIReview
        from store import Store
        with tempfile.TemporaryDirectory() as folder:
            review=AIReview(Store(folder),{})
            p=dict(as_of=int(datetime(2026,10,9,tzinfo=ZoneInfo('America/New_York')).timestamp()),marker='unchanged')
            payload=review.make_payload(p)
            self.assertIn('10_2-headline-adds',payload['input'][0]['content'])
            self.assertEqual(json.loads(payload['input'][1]['content']),p)
