import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import allview
import dashboard
import downside_manager_shadow_v1 as downside
import downside_manager_dashboard as downside_ui


class RetiredStrategyTests(unittest.TestCase):
    def test_retired_pages_are_removed_but_active_routes_remain(self):
        client = agent.app.test_client()
        for path in ('/c', '/c/how', '/a-cont-both-aligned', '/a-cont-both-aligned/data',
                     '/dol-reversal-manager', '/dol-reversal-manager/trades'):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)
        routes = {rule.rule for rule in agent.app.url_map.iter_rules()}
        for path in ('/guard/data', '/builder50/data', '/continuation/live',
                     '/dol-reversal-live', '/dol-reversal/readiness', '/downside-shadow'):
            self.assertIn(path, routes)

    def test_home_has_no_retired_tabs_or_links(self):
        page = dashboard.render_home()
        for value in ('dolrevmgr:', 'acba:', "c:{name:'C'", "f:{name:'F'",
                      '/dol-reversal-manager', '/a-cont-both-aligned', '/c/how',
                      'strategy-f-production', '__F__'):
            self.assertNotIn(value, page)
        for value in ('account100:', 'builder50:', 'continuation:', 'dolrev:'):
            self.assertIn(value, page)

    def test_old_satellite_environment_cannot_forward_bars(self):
        env = {'STRAT_C_FORWARD_URL': 'https://example.invalid/c/bars',
               'STRAT_F_FORWARD_URL': 'https://example.invalid/f/bars',
               'BUILDER50_URL': 'https://example.invalid/builder', 'BUILDER50_FORWARD_BARS': '1'}
        bar = dict(ts_event='2026-09-28T12:00:00Z', open=100, high=101, low=99, close=100)
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(agent.requests, 'post') as post, \
             mock.patch.object(agent.manage, 'check'), \
             mock.patch.object(agent.m15_shadow_strategy, 'on_bar', return_value={}), \
             mock.patch.object(agent.continuation_shadow, 'on_bar', return_value={}), \
             mock.patch.object(agent.downside_manager_shadow_v1, 'notify_bar'), \
             mock.patch.object(agent.dol_reversal_live, 'on_closed_bar', return_value={}):
            agent._after_bar_processed(bar, 1790596800000)
        post.assert_called_once_with('https://example.invalid/builder/bars', json=bar, timeout=3)

    def test_allview_has_only_local_sources(self):
        with mock.patch.object(allview, '_ab_trades', return_value=[]) as ab, \
             mock.patch.object(allview, '_continuation_trades', return_value=[]) as cont, \
             mock.patch.object(agent.requests, 'get') as get:
            self.assertEqual(allview._all_trades(), [])
        ab.assert_called_once()
        cont.assert_called_once()
        get.assert_not_called()

    def test_retired_executable_modules_are_absent(self):
        root = Path(agent.__file__).parent
        for name in ('a_cont_both_aligned_shadow', 'dol_reversal_manager_shadow_v1',
                     'dol_reversal_manager_dashboard', 'model_c_live', 'strategy_f', 'strategy_f_live', 'how_f'):
            self.assertFalse((root / (name + '.py')).exists(), name)
            self.assertNotIn(name, sys.modules)

    def test_downside_observer_registers_only_ab(self):
        with mock.patch.object(downside, 'ENABLED', False):
            self.assertEqual(set(downside.status()['strategies']), {'A/B'})

    def test_persisted_both_aligned_rows_are_archived_not_shown_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(downside, 'DATA_DIR', Path(tmp)), \
             mock.patch.object(downside, 'DB', Path(tmp) / 'shadow.sqlite3'), \
             mock.patch.object(downside, 'ENABLED', True):
            downside.migrate()
            with downside._connect() as con:
                for strategy in ('A/B', 'A_CONT_BOTH_ALIGNED'):
                    row = dict(source_key=strategy, trade_id=strategy, strategy_id=strategy,
                               signal_json='{}', direction=1, entry=100, initial_sl=95, fixed_tp=110,
                               quantity=1, initial_risk=5, signal_ms=1000, entry_anchor_ms=1000,
                               model_hash='test', threshold=0.5, policy_version='test',
                               feature_schema_version='test', dol_runtime_hash='test')
                    con.execute('INSERT INTO downside_shadow_trades (' + ','.join(row) + ') VALUES (' +
                                ','.join('?' for _ in row) + ')', list(row.values()))
            self.assertEqual([r['strategy_id'] for r in downside_ui._live_rows()], ['A/B'])
            self.assertEqual(downside.status()['total'], 1)
            with downside._connect() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM downside_shadow_trades').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
