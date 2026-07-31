import unittest

import ab_shallow
import guardrails
import live_emit
from unittest.mock import patch


class ABShallowTests(unittest.TestCase):
    def signal(self):
        return {
            'date': '2026-07-31', 'model': 'Cont', 'cat': 'PDL',
            'dir': 'SHORT', 'bos_ms': 1_000_000, 'entry': 110.0,
            'SL': 120.0, 'TP': 90.0, '_signal_close': 100.0,
        }

    def test_build_is_causal_and_fixed_2r(self):
        env = {'AB_SHALLOW_FRACTION': '0.25', 'AB_SHALLOW_RR': '2', 'EXEC_TICK': '0.25'}
        child = ab_shallow.build_shallow_signal(self.signal(), env)
        self.assertEqual(child['_strat'], 'A/B-shallow')
        self.assertEqual(child['entry'], 102.5)
        self.assertEqual(child['SL'], 120.0)
        self.assertEqual(child['TP'], 67.5)
        self.assertEqual(child['tp_src'], 'shallow_2R')
        self.assertEqual(child['_setup_group_id'], ab_shallow.setup_group_id(self.signal()))

    def test_3r_target(self):
        child = ab_shallow.build_shallow_signal(
            self.signal(), {'AB_SHALLOW_FRACTION': '0.25', 'AB_SHALLOW_RR': '3', 'EXEC_TICK': '0.25'})
        self.assertEqual(child['TP'], 50.0)

    def test_independent_risk_budgets(self):
        deep = self.signal()
        env = {
            'ACCOUNT': '100000', 'RISK_PCT': '0.5',
            'AB_SHALLOW_RISK_PCT': '0.5', 'POINT_VALUE': '2',
            'AB_SHALLOW_FRACTION': '0.25', 'AB_SHALLOW_RR': '2',
            'EXEC_TICK': '0.25',
        }
        shallow = ab_shallow.build_shallow_signal(deep, env)
        meta = ab_shallow.independent_risk_metadata(deep, shallow, env)
        self.assertEqual(shallow['_risk_pct_override'], 0.5)
        self.assertEqual(meta['deep_risk_pct'], 0.5)
        self.assertEqual(meta['shallow_risk_pct'], 0.5)
        self.assertEqual(meta['combined_max_risk_pct'], 1.0)
        self.assertEqual(meta['deep_budget'], 500.0)
        self.assertEqual(meta['shallow_budget'], 500.0)

    def test_size_for_accepts_separate_risk_pct(self):
        with patch.dict('os.environ', {'ACCOUNT': '100000', 'POINT_VALUE': '2'}, clear=False):
            deep = live_emit.size_for(110.0, 120.0, 0.5)
            shallow = live_emit.size_for(102.5, 120.0, 0.5)
        self.assertEqual(deep[0], 25)
        self.assertEqual(shallow[0], 14)

    def test_guard_groups_sibling_rows_as_one_trade(self):
        original = guardrails._today_sent
        try:
            guardrails._today_sent = lambda: [
                {'setup_group_id': 'g', 'strat': 'A/B', 'outcome': 'win', 'net': 200},
                {'setup_group_id': 'g', 'strat': 'A/B-shallow', 'outcome': 'loss', 'net': -100},
            ]
            stats = guardrails._day_stats()
            self.assertEqual(stats['sent'], 1)
            self.assertEqual(stats['losses'], 0)
            self.assertEqual(stats['net'], 100)
            self.assertFalse(stats['openpos'])
        finally:
            guardrails._today_sent = original

    def test_signal_keys_keep_strategies_separate(self):
        original = guardrails.shadow
        class FakeShadow:
            @staticmethod
            def _key(strategy, direction, ms, entry):
                return f'{strategy}|{direction}|{ms}|{entry}'
        try:
            guardrails.shadow = FakeShadow()
            deep = self.signal(); deep['_strat'] = 'A/B'
            shallow = dict(deep); shallow['_strat'] = 'A/B-shallow'; shallow['entry'] = 102.5
            self.assertNotEqual(guardrails._skey(deep), guardrails._skey(shallow))
        finally:
            guardrails.shadow = original


if __name__ == '__main__':
    unittest.main()
