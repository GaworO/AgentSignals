import unittest
from copy import deepcopy
from unittest.mock import patch
from auto_selector import select,_select_direction
from auto_fixture import fixture
from entry_rules import compile_entry
from pine_export import export_plan


def short_fixture():
    markets,t=fixture()
    # Metamorphic numerical regression only, not evidence of Tanja fidelity.
    for rows in markets.values():
        for b in rows:
            o,h,l,c=(b[k] for k in ('open','high','low','close'))
            b.update(open=1000-o,high=1000-l,low=1000-h,close=1000-c)
    return markets,t

def run(m=None,t=None):
    if m is None:m,t=short_fixture()
    return select(m,cutoff=t,now=t+1,risk_budget_usd=60,max_contracts=1)

class ShortSelectionTests(unittest.TestCase):
    def test_short_pipeline_and_own_prices(self):
        r=run();self.assertEqual(r['state'],'PLAN_READY',r)
        p=r['compiled']['plan'];v=r['compiled']['price_review']
        self.assertEqual(p['direction'],'short')
        self.assertEqual(v['stop'],901.25);self.assertEqual(v['target'],874)
        self.assertGreater(v['planned_price_risk_before_costs'],0)
        self.assertEqual(r['selected']['selection']['initial_stop']['anchors'][0]['field'],'high')
        self.assertEqual(r['compiled']['recipe']['kind'],'momentum_short')
        self.assertFalse(r['orders_enabled']);self.assertFalse(r['semantic_fidelity_verified'])
    def test_mnq_basis_not_copied_from_nq(self):
        m,t=short_fixture()
        for b in m['MNQ']:
            for k in ('open','high','low','close'):b[k]+=25
        r=run(m,t);self.assertEqual(r['compiled']['price_review']['stop'],926.25)
        self.assertEqual(r['compiled']['price_review']['target'],899)
    def test_wick_only_and_weak_body_rejected(self):
        m,t=short_fixture();m['NQ'][-1]['close']=893
        self.assertEqual(run(m,t)['state'],'ABSTAIN')
        m,t=short_fixture();m['NQ'][-1]['open']=886
        self.assertEqual(run(m,t)['state'],'ABSTAIN')
    def test_future_and_late_data(self):
        m,t=short_fixture();before=run(m,t)
        for rows in m.values():rows.append(dict(time=t,open=800,high=999,low=500,close=900))
        self.assertEqual(before,run(m,t))
        m,t=short_fixture();m['NQ'][-1]['received_at']=t+20
        self.assertEqual(run(m,t)['state'],'ABSTAIN')
    def test_wrong_direction_and_wick_cannot_pass_compiler(self):
        r=run();s=deepcopy(r['selected']['selection']);s['direction']='long'
        c=compile_entry(r['packet'],s,r['selected']['observations'],now=r['cutoff']+1)
        self.assertIsNone(c['plan'])
        s=deepcopy(r['selected']['selection']);s['swing_break_mode']='wick'
        c=compile_entry(r['packet'],s,r['selected']['observations'],now=r['cutoff']+1)
        self.assertEqual(c['plan']['direction'],'short')
    def test_short_pine_not_fake_fill(self):
        r=run();r['frozen_at']=r['cutoff']+1
        p=export_plan(r,'CME_MINI:MNQ1!')
        self.assertIn('Planned short entry',p);self.assertIn('901.25',p);self.assertIn('874',p)
        self.assertNotIn('strategy.entry',p)
    def test_both_valid_directions_abstain(self):
        m,t=short_fixture();a=run(m,t);b=deepcopy(a);b['direction']='long'
        with patch('auto_selector._select_direction',side_effect=[a,b]):r=run(m,t)
        self.assertEqual(r['reasons'],['CONFLICTING_DIRECTIONAL_PLANS']);self.assertIsNone(r['selected'])
        self.assertNotIn('compiled',r)
    def test_gap_and_wrong_side_stop_rejected(self):
        m,t=short_fixture();del m['MNQ'][-2]
        self.assertEqual(run(m,t)['state'],'ABSTAIN')
        r=run();from plan_review import review_plan
        p=deepcopy(r['compiled']['plan']);p['invalidation']['price']=p['entry_reference']['price']-10
        self.assertIn('INVALID_DIRECTIONAL_STOP',review_plan(p,now=r['cutoff']+1)['errors'])

    def test_short_projection_and_htf_smt(self):
        m,t=short_fixture();m['NQ'][360]['low']=888;m['MNQ'][360]['low']=887
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertEqual(r['selected']['audit']['target']['price'],871)
        m,t=short_fixture();start=t-240
        for i in range(360,420):m['NQ'][i].update(open=915,high=920,low=910,close=915)
        for i in range(420,480):m['NQ'][i].update(open=900,high=910,low=890,close=895)
        for i in range(480,540):m['NQ'][i].update(open=890,high=900,low=887,close=889)
        m['MNQ']=[dict(b,**{k:b[k]-1 for k in ('open','high','low','close')}) for b in m['NQ']]
        previous=start-86400
        for sym,op,hi,lo in [('NQ',890,901,870),('MNQ',889,900,869),('ES',940,960,930)]:
            m[sym]=[dict(time=x,open=op,high=hi,low=lo,close=op) for x in range(previous,previous+390*60,60)]+m[sym]
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertEqual(r['selected']['audit']['recipe'],'htf_gap_smt')
        self.assertEqual(r['compiled']['plan']['direction'],'short')
        self.assertTrue(r['selected']['selection']['requires_smt'])
        m['ES'][-2]['high']=961
        r=run(m,t);self.assertEqual(r['selected']['audit']['recipe'],'liquidity_reclaim')
