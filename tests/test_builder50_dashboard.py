import datetime as dt
import os
import unittest
from unittest import mock

import dashboard
import guardrails


BUILDER_ENV = {
    'ACCOUNT_PLAN': 'builder50',
    'ACCOUNT_LABEL': 'Builder 50K',
    'ACCOUNT_PHASE': 'evaluation',
    'ACCOUNT': '50000',
    'START_BALANCE': '50000',
    'TARGET_BALANCE': '53000',
    'START_EQUITY': '50000',
    'DD_FLOOR': '48000',
    'DD_TRAIL_USD': '2000',
    'DD_FLOOR_CAP': '50100',
    'SETUP_GROUP_RISK_USD': '500',
    'EXEC_MAX_QTY': '15',
    'INACTIVITY_DAYS': '7',
    'ACCOUNT_STARTED_ON': '2026-09-01',
    'SKIP_SESSIONS': 'ASIA,LO,NYL',
}


class Builder50DashboardTests(unittest.TestCase):
    def test_builder_profile_matches_reviewed_rules(self):
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False):
            profile = guardrails.account_profile()
        self.assertEqual(profile['plan'], 'builder50')
        self.assertEqual(profile['label'], 'Builder 50K')
        self.assertTrue(profile['config_ok'])
        self.assertEqual(profile['rules']['profit_target'], 3000)
        self.assertEqual(profile['rules']['max_eod_loss'], 2000)
        self.assertEqual(profile['rules']['daily_soft_pause'], 1000)
        self.assertEqual(profile['rules']['max_micros'], 40)
        self.assertEqual(profile['rules']['consistency'], 'none during evaluation')
        self.assertEqual(profile['rules']['floor_locks_at'], 50100)
        self.assertEqual(profile['rules']['funded_buffer'], 2100)
        self.assertEqual(profile['rules']['funded_consistency'], '50% per payout cycle')
        self.assertEqual(profile['rules']['payout_max'], 2000)
        self.assertEqual(profile['rules']['payout_split'], '80/20')
        self.assertEqual(profile['configured_group_risk'], 500)
        self.assertIn('PREM', [s['code'] for s in profile['active_sessions']])

    def test_eval_percentage_uses_configured_target_not_hardcoded_6000(self):
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False), \
             mock.patch.object(guardrails, '_state', return_value={'equity': 51500}), \
             mock.patch.object(guardrails, '_day_stats', return_value={'net': 0}), \
             mock.patch.object(guardrails, '_modeled_equity', return_value=51500.0), \
             mock.patch.object(guardrails, '_dd_floor', return_value=49500.0):
            progress = guardrails.eval_progress()
        self.assertEqual(progress['pnl'], 1500)
        self.assertEqual(progress['pct'], 50.0)
        self.assertEqual(progress['target_profit'], 3000)
        self.assertEqual(progress['target_pct'], 6.0)

    def test_inactivity_counts_confirmed_sent_trade_not_manual_or_open_order(self):
        rows = [
            {'decision': 'manual', 'date': '2026-09-04', 'outcome': 'win'},
            {'decision': 'sent', 'date': '2026-09-05', 'outcome': 'open'},
            {'decision': 'sent', 'date': '2026-09-03', 'outcome': 'win',
             'reconciled': True, 'et': '2026-09-03 08:15', 'strat': 'A/B',
             'dir': 'LONG', 'qty': 2, 'net': 300},
        ]
        now = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False):
            status = guardrails.inactivity_status(rows, now=now)
        self.assertEqual(status['last_trade_date'], '2026-09-03')
        self.assertEqual(status['days_without_trade'], 5)
        self.assertEqual(status['days_left'], 2)
        self.assertEqual(status['status'], 'warn')
        self.assertEqual(status['source'], 'broker')

    def test_trade_summary_deduplicates_ab_siblings_as_one_setup(self):
        rows = [
            {'decision': 'sent', 'setup_group_id': 'g1', 'key': 'deep',
             'outcome': 'win', 'net': 200},
            {'decision': 'sent', 'setup_group_id': 'g1', 'key': 'shallow',
             'outcome': 'loss', 'net': -100},
            {'decision': 'manual', 'setup_group_id': 'g2', 'key': 'manual',
             'outcome': 'win', 'net': 999},
        ]
        summary = guardrails.trade_summary(rows)
        self.assertEqual(summary['sent_orders'], 2)
        self.assertEqual(summary['sent_setups'], 1)
        self.assertEqual(summary['confirmed_setups'], 1)
        self.assertEqual(summary['manual_reviews'], 1)
        self.assertEqual(summary['model_net'], 100)

    def test_trading_desk_shows_100k_and_builder_as_separate_executors(self):
        with mock.patch.object(dashboard, '_ACCOUNT_LABEL', '100K Challenge'), \
             mock.patch.object(dashboard, '_BUILDER50', 'https://builder.example'):
            page = dashboard.render_home()
        self.assertIn('ACCOUNT_LABEL="100K Challenge"', page)
        self.assertIn('BUILDER50="https://builder.example"', page)
        self.assertIn("account100:{name:ACCOUNT_LABEL+' — Auto-Executor'", page)
        self.assertIn("builder50:{name:'Builder 50K — Auto-Executor'", page)
        self.assertIn("['Auto-Executors'", page)
        self.assertIn("['builder50','Builder 50K','grid']", page)
        self.assertIn("BUILDER50+'/all/trades'", page)
        self.assertIn("BUILDER50+'/all/candidates'", page)
        self.assertIn("Number(e.target||0).toLocaleString()", page)

    def test_guard_page_has_account_rules_and_activity_widgets(self):
        self.assertIn('id=account_name', guardrails._HTML)
        self.assertIn('id=rulebar', guardrails._HTML)
        self.assertIn('Sent setups · confirmed', guardrails._HTML)
        self.assertIn("['Inactivity'", guardrails._HTML)
        self.assertIn('<b>Funded / payout:</b>', guardrails._HTML)


if __name__ == '__main__':
    unittest.main()
