import unittest
from copy import deepcopy
from datetime import datetime
from dataclasses import replace
from auto_selector import select,Policy,NY,aggregate


def fixture():
    day=datetime(2026,10,8,tzinfo=NY)
    origin=int(day.timestamp());start=origin+570*60
    nq=[]
    for t in range(origin,start,60):
        b=dict(time=t,open=111,high=112,low=110,close=111)
        if t==origin+360*60:b.update(high=125)
        if t==start-5*60:b.update(low=100)
        if t==start-60:b.update(high=113)
        nq.append(b)
    for i,(o,h,l,c) in enumerate([(111,112,108,109),(106,108,98,103),(102,104,100,103),(103,115,102,114.75)]):
        nq.append(dict(time=start+i*60,open=o,high=h,low=l,close=c))
    mnq=[dict(b,**{k:b[k]+1 for k in ('open','high','low','close')}) for b in nq]
    es=[dict(time=b['time'],open=60,high=61,low=59,close=60) for b in nq]
    es[570-5]['low']=50
    for b in es[570:]:b['low']=51
    return dict(NQ=nq,MNQ=mnq,ES=es),start+4*60


def run(markets=None,cutoff=None,**kwargs):
    if markets is None:markets,cutoff=fixture()
    return select(markets,cutoff=cutoff,now=kwargs.pop('now',cutoff+1),risk_budget_usd=kwargs.pop('risk_budget_usd',60),max_contracts=1,**kwargs)


class AutomaticSelectionTests(unittest.TestCase):
    def test_raw_bars_choose_setup_strength_stop_target_without_annotations(self):
        r=run();self.assertEqual(r['state'],'PLAN_READY',r.get('reasons'))
        a=r['selected']['audit']
        self.assertEqual(a['recipe'],'liquidity_reclaim');self.assertEqual(a['timeframe'],1)
        self.assertEqual(a['stop'],98.75);self.assertEqual(a['target']['price'],126)
        self.assertEqual(r['compiled']['state'],'HYPOTHETICAL_PLAN_COMPLETE')
        self.assertFalse(r['orders_enabled']);self.assertFalse(r['semantic_fidelity_verified'])

    def test_future_bars_cannot_change_existing_decision(self):
        m,t=fixture();original=run(m,t)
        for s in m:m[s].append(dict(time=t,open=200,high=500,low=1,close=400))
        later=run(m,t)
        self.assertEqual(original,later)

    def test_late_receipt_is_unavailable(self):
        m,t=fixture();m['NQ'][-1]['received_at']=t+10
        self.assertIn('MISSING_SYNCHRONIZED_ES_NQ_MNQ',run(m,t)['reasons'])

    def test_missing_es_or_mnq_or_nq_blocks(self):
        for s in ('ES','MNQ','NQ'):
            m,t=fixture();m[s]=[]
            self.assertEqual(run(m,t)['state'],'ABSTAIN')

    def test_gap_in_current_session_blocks(self):
        m,t=fixture();del m['ES'][-3]
        self.assertIn('INCOMPLETE_RTH_HISTORY',run(m,t)['reasons'])

    def test_incomplete_reference_cannot_invent_overnight_low(self):
        m,t=fixture();del m['NQ'][50]
        self.assertEqual(run(m,t)['state'],'ABSTAIN')

    def test_weak_candle_is_rejected_even_with_inversion(self):
        m,t=fixture();m['NQ'][-1]['open']=114
        r=run(m,t)
        self.assertEqual(r['state'],'ABSTAIN')
        self.assertTrue(any('WEAK_CONFIRMATION_PROXY' in c['reasons'] for c in r['candidates']))

    def test_wick_above_gap_does_not_trigger(self):
        m,t=fixture();m['NQ'][-1]['close']=107
        self.assertEqual(run(m,t)['state'],'ABSTAIN')

    def test_no_sweep_no_supported_setup(self):
        m,t=fixture();m['NQ'][565]['low']=97
        self.assertEqual(run(m,t)['state'],'ABSTAIN')

    def test_smt_not_universally_required_for_reclaim(self):
        m,t=fixture();m['ES'][-2]['low']=49
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertFalse(r['selected']['audit']['smt'])
        self.assertFalse(r['selected']['selection']['requires_smt'])

    def test_mnq_stop_and_target_use_own_prices(self):
        m,t=fixture()
        for b in m['MNQ']:
            for k in ('open','high','low','close'):b[k]+=50
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertEqual(r['selected']['audit']['stop'],148.75)
        self.assertEqual(r['selected']['audit']['target']['price'],176)

    def test_no_hindsight_pivot_confirmation(self):
        m,t=fixture();m['NQ'][569]['high']=112
        r=run(m,t);self.assertEqual(r['state'],'ABSTAIN')
        self.assertTrue(any('SELECTED_HIGH_NOT_BROKEN_ON_CLOSE' in c['reasons'] for c in r['candidates']))

    def test_budget_cannot_force_one_contract(self):
        r=run(risk_budget_usd=1)
        self.assertEqual(r['state'],'ABSTAIN')
        self.assertTrue(any('RISK_BUDGET_TOO_SMALL' in c['reasons'] for c in r['candidates']))

    def test_stale_decision(self):
        m,t=fixture();self.assertIn('STALE_DECISION',run(m,t,now=t+16)['reasons'])

    def test_aggregate_missing_minute_never_fills(self):
        m,t=fixture();rows=m['NQ'][:5]
        self.assertEqual(len(aggregate(rows,5)),1)
        self.assertEqual(aggregate(rows[:2]+rows[3:],5),[])

    def test_invalid_bar_or_duplicate_rejected(self):
        m,t=fixture();m['NQ'][-1]['low']=200
        with self.assertRaises(ValueError):run(m,t)
        m,t=fixture();m['NQ'].append(deepcopy(m['NQ'][-1]))
        with self.assertRaises(ValueError):run(m,t)

    def test_projection_fallback_when_prior_highs_are_consumed(self):
        m,t=fixture()
        m['NQ'][360]['high']=112;m['MNQ'][360]['high']=113
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        target=r['selected']['audit']['target']
        self.assertEqual(target['price'],129)
        self.assertEqual(target['projection'],dict(ratio=1.0,origin=114,extreme=99))

    def test_htf_gap_smt_ranks_above_plain_liquidity_reclaim(self):
        m,t=fixture();start=t-240;origin=start-570*60
        for i in range(360,420):m['NQ'][i].update(open=85,high=90,low=80,close=85)
        for i in range(420,480):m['NQ'][i].update(open=100,high=110,low=90,close=105)
        for i in range(480,540):m['NQ'][i].update(open=110,high=113,low=100,close=111)
        m['MNQ']=[dict(b,**{k:b[k]+1 for k in ('open','high','low','close')}) for b in m['NQ']]
        previous=start-86400
        for sym,op,hi,lo in [('NQ',110,130,99),('MNQ',111,131,100),('ES',60,70,40)]:
            m[sym]=[dict(time=x,open=op,high=hi,low=lo,close=op) for x in range(previous,previous+390*60,60)]+m[sym]
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertEqual(r['selected']['audit']['recipe'],'htf_gap_smt')
        self.assertTrue(r['selected']['selection']['requires_smt'])
        m['ES'][-2]['low']=39
        r=run(m,t);self.assertEqual(r['state'],'PLAN_READY')
        self.assertEqual(r['selected']['audit']['recipe'],'liquidity_reclaim')

    def test_policy_is_explicit_versioned_and_changes_are_audited(self):
        r=run(policy=replace(Policy(),body_fraction=.95))
        self.assertEqual(r['state'],'ABSTAIN');self.assertEqual(r['policy']['body_fraction'],.95)
        with self.assertRaises(ValueError):run(policy=replace(Policy(),timeframe_priority=(30,)))

if __name__=='__main__':unittest.main()
