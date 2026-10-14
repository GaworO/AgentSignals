import unittest
from copy import deepcopy
from management_policy import local_partial_proposal,contract
from management_review import propose_stop
from auto_selector import select
from auto_fixture import fixture

class CoreManagementTests(unittest.TestCase):
    def inputs(self,**updates):
        value=dict(position_id='synthetic-confirmed-position',direction='long',original_quantity=5,remaining_quantity=5,entry=100,opened_at=1000,
          objective=dict(price=105,available_at=990,source='selected historical swing'),final_target=120,now=1100,market_price=105,price_known_at=1100,first_partial_done=False,partial_pending=False)
        value.update(updates);return value
    def test_twenty_percent_original_floor_and_reached_objective(self):
        r=local_partial_proposal(**self.inputs());self.assertEqual(r['quantity'],1);self.assertEqual(r['remaining_after_fill'],4)
        for qty in (1,2,3,4):
            self.assertEqual(local_partial_proposal(**self.inputs(original_quantity=qty,remaining_quantity=qty))['reason'],'PARTIAL_LESS_THAN_ONE_CONTRACT')
        r=local_partial_proposal(**self.inputs(original_quantity=14,remaining_quantity=9))
        self.assertEqual(r['quantity'],2) # original, not remaining
        self.assertEqual(local_partial_proposal(**self.inputs(market_price=104.75))['reason'],'LOCAL_OBJECTIVE_NOT_REACHED')
        self.assertFalse(r['is_order'])
    def test_short_direction_and_final_target_priority(self):
        d=self.inputs(direction='short',objective=dict(price=95,available_at=990,source='selected low'),final_target=80,market_price=95)
        self.assertEqual(local_partial_proposal(**d)['quantity'],1)
        self.assertEqual(local_partial_proposal(**dict(d,market_price=80))['reason'],'FINAL_TARGET_TAKES_PRIORITY')
        self.assertEqual(local_partial_proposal(**self.inputs(market_price=120))['reason'],'FINAL_TARGET_TAKES_PRIORITY')
    def test_causal_quote_and_objective_and_invalid_geometry(self):
        for update in ({'price_known_at':1097},{'price_known_at':1101},{'opened_at':1101}):
            self.assertEqual(local_partial_proposal(**self.inputs(**update))['action'],'NONE')
        o=dict(price=105,available_at=1101,source='future')
        self.assertEqual(local_partial_proposal(**self.inputs(objective=o))['reason'],'OBJECTIVE_NOT_KNOWN_AT_QUOTE')
        for price in (99,121,105.1,float('nan')):
            self.assertEqual(local_partial_proposal(**self.inputs(objective=dict(price=price,available_at=990,source='invalid')))['action'],'NONE')
    def test_idempotent_intent_and_pending_or_done_suppression(self):
        a=local_partial_proposal(**self.inputs());b=local_partial_proposal(**self.inputs(now=1101,price_known_at=1101,market_price=106))
        self.assertEqual(a['intent_id'],b['intent_id'])
        for update in ({'partial_pending':True},{'first_partial_done':True},{'remaining_quantity':1}):
            self.assertEqual(local_partial_proposal(**self.inputs(**update))['action'],'NONE')
    def test_contract_no_news_only_partial_or_rejection_exit(self):
        c=contract();self.assertFalse(c['local_partial']['news_alone_triggers'])
        self.assertFalse(c['separate_wick_rejection_exit']);self.assertTrue(c['additions']['enabled']);self.assertEqual(c['additions']['maximum'],1);self.assertFalse(c['extra_es_atr_partial']['atr_enabled'])
    def test_m5_plan_to_partial_and_stop_proposals_with_explicit_position(self):
        # Software integration only: position/quote inputs are synthetic, not
        # broker acknowledgements or claims about a historical trade.
        m,t=fixture();r=select(m,cutoff=t,now=t+1,risk_budget_usd=250,max_contracts=5)
        self.assertEqual(r['selected']['audit']['timeframe'],5)
        p=r['compiled']['plan'];entry=p['entry_reference']['price'];target=p['target']['anchor']['price'];qty=p['initial_quantity']
        self.assertEqual(qty,5)
        objective=dict(price=entry+2,source='synthetic selected local objective',available_at=t)
        d=self.inputs(position_id=p['plan_id'],entry=entry,opened_at=t+1,now=t+20,price_known_at=t+20,market_price=entry+2,objective=objective,final_target=target)
        partial=local_partial_proposal(**d);self.assertEqual(partial['quantity'],1)
        pending=local_partial_proposal(**dict(d,now=t+21,price_known_at=t+21,partial_pending=True));self.assertEqual(pending['action'],'NONE')
        stop=propose_stop([],direction='long',entry=entry,current_stop=r['selected']['audit']['stop'],opened_at=t+1,as_of=t+120,market_price=entry+3,price_known_at=t+120,news_at=t+180,news_known_at=t)
        self.assertEqual(stop['stop_price'],entry-1.5);self.assertFalse(stop['is_order'])
        final=local_partial_proposal(**dict(d,now=t+130,price_known_at=t+130,market_price=target))
        self.assertEqual(final['reason'],'FINAL_TARGET_TAKES_PRIORITY')

if __name__=='__main__':unittest.main()
