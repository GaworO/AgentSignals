import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import guardrails


class GuardDuplicateBookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'guard_log.json'
        for name, value in [('DATA_DIR', tmp.name), ('GLOG', str(self.path)),
                            ('GSTATE', str(Path(tmp.name) / 'state.json'))]:
            ctx = patch.object(guardrails, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        for ctx in [patch.object(guardrails.portfolio_guard, 'record_note'),
                    patch.object(guardrails, '_trade_alert'),
                    patch.dict(os.environ, {'GUARD_TRADE_ALERTS': '0'})]:
            ctx.start()
            self.addCleanup(ctx.stop)
        self.signal = dict(_strat='Continuation LONG', _continuation_order_id='order-1',
                           dir='LONG', bos_ms=1790604000000, entry=25000, SL=24990, TP=25020)

    def rows(self):
        return json.loads(self.path.read_text())

    def test_repeated_continuation_and_directional_blocks_are_saved_once(self):
        for plan in ('pro100', 'builder50'):
            for strategy in ('Continuation LONG', 'A/B Directional LONG'):
                with self.subTest(plan=plan, strategy=strategy), patch.dict(os.environ, {'ACCOUNT_PLAN': plan}):
                    self.path.write_text('[]')
                    signal = dict(self.signal, _strat=strategy)
                    for _ in range(3):
                        guardrails.note(signal, 'blocked', 'mode_off')
                    self.assertEqual(len(self.rows()), 1)
                    self.assertEqual(self.rows()[0]['key'], 'CONT_ORDER|order-1')

    def test_duplicate_block_folds_into_sent_continuation(self):
        guardrails.note(self.signal, 'sent')
        guardrails.note(self.signal, 'blocked', 'duplicate')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['duplicate_count'], 1)

    def test_old_block_is_found_beyond_last_100_and_out_of_order_dates(self):
        signal = dict(self.signal)
        signal.pop('_continuation_order_id')
        signal['_strat'] = 'A/B'
        guardrails.note(signal, 'blocked', 'mode_off')
        original = self.rows()
        self.path.write_text(json.dumps(original + [dict(key=str(i), date='2000-01-01') for i in range(110)]))
        guardrails.note(signal, 'blocked', 'mode_off')
        self.assertEqual(len(self.rows()), 111)

    def test_duplicate_counts_stay_with_the_correct_sibling(self):
        deep = dict(self.signal, _strat='A/B', _setup_group_id='g1')
        deep.pop('_continuation_order_id')
        shallow = dict(deep, _strat='A/B-shallow', entry=25001)
        guardrails.note(deep, 'sent')
        guardrails.note(shallow, 'sent')
        guardrails.note(deep, 'blocked', 'duplicate')
        self.assertEqual(self.rows()[0]['duplicate_count'], 1)
        self.assertNotIn('duplicate_count', self.rows()[1])

    def test_historical_blocks_show_latest_reason_without_rewriting_audit(self):
        guardrails.note(self.signal, 'blocked', 'mode_off')
        row = self.rows()[0]
        rows = [row, dict(row), dict(row, reason='news')]
        before = copy.deepcopy(rows)
        visible = guardrails._trade_book_rows(rows)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['reason'], 'news')
        self.assertEqual(rows, before)

    def test_sent_replaces_blocked_display_but_preserves_real_orders(self):
        guardrails.note(self.signal, 'blocked', 'mode_off')
        row = self.rows()[0]
        sent = dict(row, decision='sent', qty=2, reconciled=True, ext_net=123)
        rows = [row, sent, dict(row, reason='duplicate'), dict(sent, ts=sent['ts'] + 1000)]
        visible = guardrails._trade_book_rows(rows)
        self.assertEqual(len(visible), 2)
        self.assertEqual(visible[0]['ext_net'], 123)
        self.assertTrue(all(r['decision'] == 'sent' for r in visible))

    def test_distinct_orders_siblings_days_and_unidentified_rows_survive(self):
        guardrails.note(self.signal, 'blocked', 'mode_off')
        row = self.rows()[0]
        rows = [row, dict(row, continuation_order_id='order-2'),
                dict(row, strat='A/B-shallow'), dict(row, date='2000-01-01'),
                {'decision': 'blocked'}, {'decision': 'blocked'}]
        self.assertEqual(guardrails._trade_book_rows(rows), rows)

    def test_guard_endpoint_collapses_old_blocks_before_80_row_limit(self):
        from flask import Flask
        guardrails.note(self.signal, 'blocked', 'mode_off')
        row = self.rows()[0]
        rows = [dict(row, key='CONT_ORDER|older', continuation_order_id='older')] + [row] * 100
        self.path.write_text(json.dumps(rows))
        app = Flask(__name__)
        guardrails.register(app)
        with patch.object(guardrails, 'backfill_trade_classifications'), \
             patch.object(guardrails, '_shadow_by_key', return_value={}), \
             patch.object(guardrails, 'health', return_value={}), \
             patch.object(guardrails, 'eval_progress', return_value={}), \
             patch.object(guardrails, 'inactivity_status', return_value={}):
            response = app.test_client().get('/guard/data')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data['book']), 2)
        self.assertEqual(data['book'][1]['continuation_order_id'], 'older')
        self.assertEqual(data['trade_summary']['sent_orders'], 0)
        self.assertEqual(len(self.rows()), 101)


if __name__ == '__main__':
    unittest.main()
