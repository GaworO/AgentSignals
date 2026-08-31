import datetime as dt
import os
import unittest
from unittest import mock

import dashboard
import guardrails
import agent
import ab_shallow
import live_emit


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

    def test_builder_rejects_bar_fanout_variables_copied_from_100k(self):
        bad = dict(BUILDER_ENV, BUILDER50_URL='https://builder.example',
                   STRAT_C_FORWARD_URL='https://strategy-c.example/bars')
        with mock.patch.dict(os.environ, bad, clear=True):
            profile = guardrails.account_profile()
        self.assertFalse(profile['config_ok'])
        self.assertTrue(any('BUILDER50_URL belongs only' in w for w in profile['config_warnings']))
        self.assertTrue(any('must not fan out bars' in w for w in profile['config_warnings']))

    def test_execution_route_fingerprint_detects_same_webhook_without_exposing_it(self):
        webhook = 'https://webhooks.example/secret-route'
        with mock.patch.dict(os.environ, {'EXEC_WEBHOOK': webhook}, clear=True):
            route_id = guardrails._exec_route_id()
        self.assertEqual(len(route_id), 16)
        self.assertNotIn('secret-route', route_id)

    def test_traderspost_stale_signal_guard_is_bounded(self):
        with mock.patch.dict(os.environ, {'EXEC_REJECT_AFTER_SEC': '15'}, clear=True):
            self.assertEqual(agent._signal_reject_after_sec(), 15)
        with mock.patch.dict(os.environ, {'EXEC_REJECT_AFTER_SEC': '999'}, clear=True):
            self.assertEqual(agent._signal_reject_after_sec(), 30)

    def test_executor_test_signal_can_never_reach_the_broker(self):
        response = mock.Mock(status_code=200, text='ok')
        with mock.patch.dict(os.environ, {
            'EXEC_WEBHOOK': 'https://webhooks.example/test', 'EXEC_TICKER': 'MNQU2026',
            'EXEC_QTY': '1', 'PARTIAL_AT_1R': '0', 'EXEC_TIF': 'day'
        }, clear=True), mock.patch.object(agent.requests, 'post', return_value=response) as post:
            result = agent._exec_order({'dir': 'LONG', 'entry': 20000, 'SL': 19990,
                                        '_test_signal': True})
        self.assertTrue(result['sent'])
        self.assertIs(post.call_args.kwargs['json']['test'], True)
        self.assertIn('route_id', result)
        self.assertNotIn('path_tail', result)

    def test_builder_ab_and_shallow_share_one_500_dollar_ceiling(self):
        env = dict(BUILDER_ENV, AB_SHALLOW_ENABLED='1', AB_SHALLOW_FRACTION='0.25',
                   AB_SHALLOW_RR='2', AB_SHALLOW_MIN_SL_PTS='5',
                   SETUP_GROUP_RT_COST_USD='2.24', POINT_VALUE='2', EXEC_TICK='0.25')
        deep = {'date': '2026-08-31', 'model': 'Reversal', 'cat': 'F.P.FVG',
                'dir': 'SHORT', 'bos_ms': 1785505320000, 'entry': 28545.0,
                'SL': 28576.0, 'TP': 28456.5, '_signal_close': 28478.5}
        child = ab_shallow.build_shallow_signal(deep, env)
        ab_shallow.apply_shared_group_budget(deep, child, env)
        sized = [live_emit.size_for_budget(x['entry'], x['SL'], x['_risk_budget_usd'], 2.24)
                 for x in (deep, child)]
        self.assertLessEqual(sum(x[3] for x in sized), 500.0)
        self.assertEqual(deep['_risk_budget_usd'], 250.0)
        self.assertEqual(child['_risk_budget_usd'], 250.0)

    def test_eval_percentage_uses_configured_target_not_hardcoded_6000(self):
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False), \
             mock.patch.object(guardrails, '_state', return_value={'equity': 51500}), \
             mock.patch.object(guardrails, '_day_stats', return_value={'net': 0}), \
             mock.patch.object(guardrails, '_modeled_equity', return_value=51500.0), \
             mock.patch.object(guardrails, '_dd_floor', return_value=49500.0), \
             mock.patch.object(guardrails, 'confirmed_trading_days', return_value=['2026-09-01']):
            progress = guardrails.eval_progress()
        self.assertEqual(progress['pnl'], 1500)
        self.assertEqual(progress['pct'], 50.0)
        self.assertEqual(progress['target_profit'], 3000)
        self.assertEqual(progress['target_pct'], 6.0)

    def test_target_alone_is_not_pass_without_minimum_trading_day(self):
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False), \
             mock.patch.object(guardrails, '_state', return_value={'equity': 53000}), \
             mock.patch.object(guardrails, '_day_stats', return_value={'net': 0}), \
             mock.patch.object(guardrails, '_modeled_equity', return_value=53000.0), \
             mock.patch.object(guardrails, '_dd_floor', return_value=50100.0), \
             mock.patch.object(guardrails, 'confirmed_trading_days', return_value=[]):
            progress = guardrails.eval_progress()
        self.assertTrue(progress['target_reached'])
        self.assertFalse(progress['passed'])
        self.assertEqual(progress['trading_days'], 0)

    def test_target_and_confirmed_day_mark_pass(self):
        with mock.patch.dict(os.environ, BUILDER_ENV, clear=False), \
             mock.patch.object(guardrails, '_state', return_value={'equity': 53000}), \
             mock.patch.object(guardrails, '_day_stats', return_value={'net': 0}), \
             mock.patch.object(guardrails, '_modeled_equity', return_value=53000.0), \
             mock.patch.object(guardrails, '_dd_floor', return_value=50100.0), \
             mock.patch.object(guardrails, 'confirmed_trading_days', return_value=['2026-09-02']):
            progress = guardrails.eval_progress()
        self.assertTrue(progress['passed'])
        self.assertEqual(progress['trading_days'], 1)

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
        self.assertIn('SAME WEBHOOK', page)
        self.assertIn("routeMain===routeBuilder", page)

    def test_guard_page_has_account_rules_and_activity_widgets(self):
        self.assertIn('id=account_name', guardrails._HTML)
        self.assertIn('id=rulebar', guardrails._HTML)
        self.assertIn('Sent setups · confirmed', guardrails._HTML)
        self.assertIn("['Inactivity'", guardrails._HTML)
        self.assertIn('<b>Funded / payout:</b>', guardrails._HTML)


if __name__ == '__main__':
    unittest.main()
