import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from entry_sequence_fixture import fixture,decision_fixture
from entry_patterns import PROFILES
from entry_review import validate_decision,compile_review
from entry_rules import compile_entry,fingerprint
from paper_lifecycle import PaperLifecycle,losses_on_day
from source_rules import partial_proposal


class EntrySequencesTest(unittest.TestCase):
    def test_eight_family_direction_timing_combinations(self):
        for family in PROFILES:
            for direction in ('long','short'):
                for mode in ('immediate_after_confirmation','retracement'):
                    with self.subTest(family=family,direction=direction,mode=mode):
                        p,d=fixture(family,direction,mode)
                        r=validate_decision(p,d)
                        self.assertTrue(r['entry_setup_verified'],r)
                        decision,at,obj=decision_fixture(family,direction,mode)
                        with tempfile.TemporaryDirectory() as root:
                            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=.25,fee_per_side=.5)
                            sim.submit('submit',decision=decision,objective=obj,now=at+1)
                            limit=decision['compiled']['plan']['entry_reference']['price'];sign=1 if direction=='long' else -1
                            if mode=='retracement':
                                got=sim.quote('wait',at=at+2,received_at=at+2,market_price=limit+sign)
                                self.assertEqual(got['result'],'WAITING_FOR_RETRACEMENT')
                                self.assertEqual(sim.snapshot()['runs'][0]['quantity'],0)
                            sim.quote('fill',at=at+3,received_at=at+3,market_price=limit)
                            run=sim.snapshot()['runs'][0]
                            self.assertEqual(run['state'],'OPEN')
                            if mode=='retracement':self.assertEqual(run['base_entry'],limit)
                            self.assertEqual(sim.snapshot()['orders_sent'],0)

    def test_wrong_inversion_direction_and_missing_sequence_are_rejected(self):
        decision,at,_=decision_fixture()
        for mutation in ('direction','sequence','future-swing','unbroken-swing','no-retracement'):
            x=deepcopy(decision);s=x['selected']['selection'];p=x['packet']
            if mutation=='direction':s['direction']='short'
            elif mutation=='sequence':s.pop('setup_sequence')
            elif mutation=='future-swing':p['evidence']['swing']['available_at']=at
            elif mutation=='unbroken-swing':p['evidence']['swing']['price']=90
            else:s['setup_sequence']['retracement_bar_id']='a'
            p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});s['packet_id']=p['packet_id']
            for o in x['selected']['observations']:o['packet_id']=p['packet_id']
            self.assertIsNone(compile_entry(p,s,x['selected']['observations'],now=at+1)['plan'],mutation)

    def test_htf_objective_and_m15_gap_are_required_in_both_directions(self):
        for direction in ('long','short'):
            for mutation in ('timeframe','objective-unreached','late-retracement','wrong-gap-direction'):
                p,d=fixture(PROFILES[1],direction)
                if mutation=='timeframe':p['evidence']['h1-objective']['timeframe']=15
                elif mutation=='objective-unreached':d['entry_setup']['setup_sequence']['objective_touch_bar_id']='a'
                elif mutation=='late-retracement':d['entry_setup']['setup_sequence']['retracement_bar_id']='trigger-bar'
                else:p['evidence']['m15-gap']['direction']='bear' if direction=='long' else 'bull'
                p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'})
                d['context']['packet_id']=p['packet_id']
                with self.assertRaises(ValueError,msg=mutation):validate_decision(p,d)

    def test_retracement_cancellation_expiry_and_restart(self):
        for direction in ('long','short'):
            for cause in ('invalidation','target','expiry','news'):
                with self.subTest(direction=direction,cause=cause):
                    decision,at,obj=decision_fixture(direction=direction,mode='retracement');p=decision['compiled']['plan']
                    with tempfile.TemporaryDirectory() as root:
                        path=Path(root)/'paper.db';sim=PaperLifecycle(path,slippage_points=0,fee_per_side=0)
                        sim.submit('s',decision=decision,objective=obj,now=at+1)
                        sim=PaperLifecycle(path,slippage_points=0,fee_per_side=0)
                        now=p['entry_valid_until'] if cause=='expiry' else at+2
                        value=p['invalidation']['price'] if cause=='invalidation' else p['target']['anchor']['price'] if cause=='target' else p['entry_reference']['price']
                        news=[dict(at=now+100,known_at=at)] if cause=='news' else []
                        args=dict(at=now,received_at=now,market_price=value,news_events=news,acknowledgement='hold')
                        sim.quote('q',**args)
                        run=sim.snapshot()['runs'][0]
                        self.assertEqual(run['state'],'EXPIRED' if cause=='expiry' else 'CANCELLED')
                        self.assertEqual(run['quantity'],0)
                        self.assertTrue(sim.quote('q',**args)['duplicate'])

    def test_fraction_uses_original_filled_quantity_and_skips_small_positions(self):
        decision,at,obj=decision_fixture();spec=decision['compiled']['plan']['management_spec']
        args=dict(spec=spec,position_id='p',remaining_quantity=7,completed=[],now=at,
            quotes={'MNQ':dict(at=at,price=obj['price'])})
        self.assertEqual(partial_proposal(**args,original_quantity=10)['quantity'],3)
        self.assertEqual(partial_proposal(**args,original_quantity=2)['action'],'NONE')

    def test_partial_after_entry_uses_cost_adjusted_original_and_does_not_repeat(self):
        decision,at,obj=decision_fixture()
        with tempfile.TemporaryDirectory() as root:
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=.25,fee_per_side=.5)
            sim.submit('s',decision=decision,objective=obj,now=at+1)
            sim.quote('fill',at=at+2,received_at=at+2,market_price=decision['compiled']['plan']['entry_reference']['price'])
            original=sim.snapshot()['runs'][0]['original_quantity']
            got=sim.quote('touch',at=at+3,received_at=at+3,market_price=obj['price'])
            self.assertEqual(got['pending']['quantity'],original//3)
            sim.quote('partial-fill',at=at+4,received_at=at+4,market_price=obj['price'])
            sim.quote('same',at=at+5,received_at=at+5,market_price=obj['price'])
            run=sim.snapshot()['runs'][0]
            self.assertEqual(run['quantity'],original-original//3);self.assertIsNone(run['pending'])

    def test_retracement_limit_cannot_be_worse_than_confirmation_close(self):
        p,d=fixture(mode='retracement');d['entry_setup']['entry_anchor']=dict(evidence_id='trigger-bar',field='high')
        with self.assertRaises(ValueError):validate_decision(p,d)

    def test_atr_blocks_only_new_continuation_and_unknown_does_not_pass(self):
        for exhausted in (True,False,None):
            p,d=fixture();d['entry_setup']['trade_kind']='continuation'
            if exhausted is not None:
                p['daily_atr']=dict(symbol='MNQ',atr=100,range=100 if exhausted else 80,available_at=p['as_of'],
                    complete=True,source='synthetic-measurement',convention='explicit test convention')
                p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});d['context']['packet_id']=p['packet_id']
            r=validate_decision(p,d)
            self.assertEqual(bool(r.get('entry_setup_verified')),exhausted is False,r)

    def test_manipulation_retracement_can_use_a_selected_range(self):
        p,d=fixture()
        p['evidence']['range']=dict(kind='window',symbol='MNQ',complete=True,high=110,low=104,available_at=p['as_of']-600)
        p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});d['context']['packet_id']=p['packet_id']
        d['entry_setup']['setup_sequence']['retracement_zone_id']='range'
        self.assertTrue(validate_decision(p,d)['entry_setup_verified'])

    def test_directional_structure_break_role_is_distinct_from_a_sweep(self):
        for direction in ('long','short'):
            p,d=fixture(direction=direction)
            p['evidence']['swing'].update(side='high' if direction=='long' else 'low',price=103 if direction=='long' else 197)
            d['entry_setup']['setup_sequence']['structure']['role']='directional_break'
            p['packet_id']=fingerprint({k:v for k,v in p.items() if k!='packet_id'});d['context']['packet_id']=p['packet_id']
            self.assertTrue(validate_decision(p,d)['entry_setup_verified'])

    def test_entry_does_not_require_an_invented_local_partial_objective(self):
        p,d=fixture();d['entry_setup']['local_objective']=None
        decision=compile_review(p,d,selected_at=p['as_of']+1,now=p['as_of']+1,budget=250,cap=40)
        decision.update(frozen_at=p['as_of']+1,ai_context=dict(state='PERMITTED',direction='long'),
            scheduled_news=dict(state='CLEAR'),news_entry_reasons=[],account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
        with tempfile.TemporaryDirectory() as root:
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
            sim.submit('s',decision=decision,now=p['as_of']+1)
            sim.quote('fill',at=p['as_of']+2,received_at=p['as_of']+2,market_price=112)
            self.assertEqual(sim.snapshot()['runs'][0]['state'],'OPEN')

    def test_cumulative_two_loss_guard_ignores_break_even_and_resets_by_ny_date(self):
        decision,at,obj=decision_fixture()
        with tempfile.TemporaryDirectory() as root:
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
            for n in range(2):
                d=deepcopy(decision);s=d['selected']['selection'];s['id']=f'loss-{n}'
                d['compiled']=compile_entry(d['packet'],s,d['selected']['observations'],now=at+1+n*3)
                sim.submit(f's{n}',decision=d,objective=obj,now=at+1+n*3)
                sim.quote(f'fill{n}',at=at+2+n*3,received_at=at+2+n*3,market_price=d['compiled']['plan']['entry_reference']['price'])
                sim.quote(f'loss{n}',at=at+3+n*3,received_at=at+3+n*3,market_price=d['compiled']['price_review']['stop'])
            with self.assertRaisesRegex(ValueError,'TWO_LOSSES_NEW_YORK_DAY'):
                sim.submit('third',decision=decision,objective=obj,now=at+7)
            runs=sim.snapshot()['runs']
            runs.insert(1,dict(state='CLOSED',gross_usd=0,fees_usd=0,closed_at=at+4,ledger=[dict(at=at+4)]))
            self.assertEqual(losses_on_day(runs,at+7),2)
            self.assertEqual(losses_on_day(runs,at+86400),0)

    def test_old_selected_inversion_can_also_use_optional_retracement(self):
        from test_source_rules import source_decision
        d,at,obj,_=source_decision();s=d['selected']['selection'];entry=s['entry_reference']
        s.update(entry_mode='retracement',entry_valid_until=s['context_valid_until'],cancel_condition='Expiry or invalidation')
        s['entry_reference']=dict(entry,field='open')
        d['compiled']=compile_entry(d['packet'],s,d['selected']['observations'],now=at+1)
        self.assertIsNotNone(d['compiled']['plan'],d['compiled'])
        with tempfile.TemporaryDirectory() as root:
            sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
            sim.submit('s',decision=d,objective=obj,now=at+1)
            sim.quote('fill',at=at+2,received_at=at+2,market_price=d['compiled']['plan']['entry_reference']['price'])
            self.assertEqual(sim.snapshot()['runs'][0]['state'],'OPEN')

    def test_saved_model_choice_flows_automatically_to_plan_and_paper_without_extra_call(self):
        from store import Store
        from ai_review import AIReview
        from selection_observer import SelectionObserver
        from plan_observer import assess
        from unittest.mock import patch
        for direction in ('long','short'):
            p,d=fixture(PROFILES[1],direction,'retracement');source=p['as_of'];now=source+61
            with tempfile.TemporaryDirectory() as root:
                store=Store(root);ai=AIReview(store,dict(TANJA_AI_ENABLED='true',OPENAI_API_KEY='fixture',OPENAI_MODEL='fixture'))
                with store.connect() as db:
                    for symbol in ('ES','MNQ','NQ'):
                        v=112 if direction=='long' else 188
                        payload=dict(symbol=symbol,bar_open_ms=source*1000,bar_close_ms=(source+60)*1000,open=v,high=v+1,low=v-1,close=v)
                        table='collection_bars' if symbol=='NQ' else 'bars'
                        db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',(symbol,source,source+60,source+60,json.dumps(payload)))
                Path(root,'account_snapshot.json').write_text(json.dumps(dict(balance=50000,minimum_balance=48000,target_balance=53000,
                    daily_loss_remaining=1000,committed_risk_usd=0,committed_micro_contracts=0,as_of='synthetic',reported_at=source)))
                snapshot=dict(packet=p,frozen_at=source+1,processed_at=source+1)
                times=iter([source+2,source+2,source+25]);calls=[]
                def transport(payload,key):
                    calls.append(payload)
                    self.assertIn('entry_setup',payload['text']['format']['schema']['properties'])
                    return dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])
                with patch.object(ai,'latest_context',return_value=snapshot),patch.object(ai,'gate',return_value='READY'):
                    self.assertTrue(ai.process_one(transport=transport,clock=lambda:next(times)))
                with store.connect() as db:row=dict(db.execute('SELECT * FROM ai_runs').fetchone())
                self.assertEqual(row['status'],'validated',row['error'])
                self.assertEqual(assess(row)['state'],'ENTRY_SELECTION_VERIFIED')
                observer=SelectionObserver(store,{})
                from datetime import datetime
                from zoneinfo import ZoneInfo
                date=datetime.fromtimestamp(now,ZoneInfo('America/New_York')).isoformat()
                observer.news.refresh(fetch=lambda:[dict(title='Synthetic low-impact event',country='EUR',impact='Low',date=date)],clock=lambda:now)
                self.assertTrue(observer.process_one(now))
                saved=observer.audit(observer.revision,source+60)
                self.assertEqual(saved['state'],'PLAN_READY',saved['reasons'])
                self.assertEqual(saved['selected']['selection']['setup_profile'],PROFILES[1])
                self.assertEqual(len(calls),1)
                sim=PaperLifecycle(Path(root)/'paper.db',slippage_points=0,fee_per_side=0)
                obj=saved['compiled']['plan']['management_spec']['partials'][0]['objective']
                sim.submit('s',decision=saved,objective=obj,now=now+1)
                sim.quote('fill',at=now+2,received_at=now+2,market_price=saved['compiled']['plan']['entry_reference']['price'])
                self.assertEqual(sim.snapshot()['runs'][0]['state'],'OPEN')


if __name__=='__main__':unittest.main()
