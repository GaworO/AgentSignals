import os
import unittest
from unittest import mock

import guardrails


class PremStopFilterTests(unittest.TestCase):
    def env(self, **extra):
        out = {
            'PREM_STOP_FILTER': '1',
            'PREM_STOP_FULL_MAX_PTS': '25',
            'PREM_STOP_HARD_MAX_PTS': '28',
            'PREM_STOP_MID_RISK_MULT': '0.5',
        }
        out.update(extra)
        return mock.patch.dict(os.environ, out, clear=False)

    @staticmethod
    def signal(stop_pts, sess='PREM', strat='A/B'):
        return {
            'sess': sess,
            '_strat': strat,
            'entry': 100.0,
            'SL': 100.0 + float(stop_pts),
            'dir': 'SHORT',
        }

    def test_at_or_below_25_points_keeps_full_risk(self):
        with self.env():
            policy = guardrails.prem_stop_policy(self.signal(25))
        self.assertEqual(policy['action'], 'full')
        self.assertEqual(policy['risk_mult'], 1.0)

    def test_25_to_28_points_scales_whole_group_to_half_risk(self):
        with self.env():
            policy = guardrails.prem_stop_policy(self.signal(26))
            edge = guardrails.prem_stop_policy(self.signal(28))
        self.assertEqual(policy['action'], 'scale')
        self.assertEqual(policy['risk_mult'], 0.5)
        self.assertEqual(edge['action'], 'scale')
        self.assertEqual(edge['risk_mult'], 0.5)

    def test_above_28_points_blocks(self):
        with self.env():
            policy = guardrails.prem_stop_policy(self.signal(28.25))
        self.assertEqual(policy['action'], 'block')
        self.assertEqual(policy['reason'], 'prem_stop_too_wide')
        self.assertEqual(policy['risk_mult'], 0.0)

    def test_other_sessions_and_strategies_are_unchanged(self):
        with self.env():
            nyam = guardrails.prem_stop_policy(self.signal(40, sess='NYAM'))
            model_c = guardrails.prem_stop_policy(self.signal(40, strat='C'))
        self.assertEqual(nyam['action'], 'pass')
        self.assertFalse(nyam['applies'])
        self.assertEqual(model_c['action'], 'pass')
        self.assertFalse(model_c['applies'])

    def test_filter_can_be_disabled(self):
        with self.env(PREM_STOP_FILTER='0'):
            policy = guardrails.prem_stop_policy(self.signal(40))
        self.assertEqual(policy['action'], 'pass')
        self.assertFalse(policy['enabled'])

    def test_invalid_thresholds_fail_closed(self):
        with self.env(PREM_STOP_FULL_MAX_PTS='30', PREM_STOP_HARD_MAX_PTS='28'):
            policy = guardrails.prem_stop_policy(self.signal(20))
        self.assertEqual(policy['action'], 'block')
        self.assertEqual(policy['reason'], 'prem_stop_filter_config')


if __name__ == '__main__':
    unittest.main()
