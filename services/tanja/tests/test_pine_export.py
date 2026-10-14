import sys
from pathlib import Path
import copy
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pine_export import export_plan
from auto_selector import select
from auto_fixture import fixture

class PineExportTests(unittest.TestCase):
    def record(self):
        markets,t=fixture()
        r=select(markets,cutoff=t,now=t+1,risk_budget_usd=100,max_contracts=1)
        r['frozen_at']=t+1
        return r
    def test_causal_reference_not_fill(self):
        r=self.record();script=export_plan(r,'CME_MINI:MNQ1!')
        self.assertIn(f'int planAvailable = {(r["cutoff"]+1)*1000}',script)
        self.assertIn('NOT a filled trade',script)
        self.assertIn('runtime.error',script)
        self.assertNotIn('strategy.entry',script)
        self.assertNotIn('alert(',script)
    def test_abstention_cannot_be_trade(self):
        r=self.record();r['state']='ABSTAIN'
        with self.assertRaises(ValueError):export_plan(r,'CME_MINI:MNQ1!')
    def test_bad_levels_rejected(self):
        for stop in [float('nan'),float('inf'),-1,999999]:
            r=self.record();r['compiled']['price_review']['stop']=stop
            with self.assertRaises(ValueError):export_plan(r,'CME_MINI:MNQ1!')
    def test_expiry_and_market_checks(self):
        r=self.record();r['compiled']['plan']['context_valid_until']=r['cutoff']
        with self.assertRaises(ValueError):export_plan(r,'CME_MINI:MNQ1!')
        r=self.record();r['compiled']['plan']['symbol']='NQ'
        with self.assertRaises(ValueError):export_plan(r,'CME_MINI:MNQ1!')
