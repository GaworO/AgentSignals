import json
import os
import tempfile
import unittest

import broker_feedback
import guardrails


class BrokerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        guardrails.DATA_DIR = self.tmp.name
        guardrails.GLOG = os.path.join(self.tmp.name, 'guard_log.json')
        guardrails.GSTATE = os.path.join(self.tmp.name, 'guard_state.json')
        guardrails.BUNMATCH = os.path.join(self.tmp.name, 'broker_unmatched.json')
        row = {
            'key': 'sig-1', 'plan_id': 'plan-1', 'strat': 'A/B',
            'date': guardrails._today(), 'decision': 'sent',
            'entry': 100.0, 'sl': 95.0, 'tp': 110.0, 'qty': 2,
            'active_from_ms': 1_785_000_000_000, 'valid_until_ms': 1_785_000_600_000,
            'relay_accepted_ms': 1_785_000_001_000, 'broker_status': None,
            'broker_orders': [], 'ts': 2_000,
        }
        guardrails._save(guardrails.GLOG, [row])
        guardrails._save(guardrails.GSTATE, dict(guardrails._DEF_STATE))

    def tearDown(self):
        self.tmp.cleanup()

    def load_row(self):
        with open(guardrails.GLOG, encoding='utf-8') as f:
            return json.load(f)[0]

    def test_normalize_common_aliases(self):
        event = broker_feedback.normalize({
            'orderId': 'abc', 'orderStatus': 'partially_filled',
            'filledQty': 1, 'timestamp': 10,
        })
        self.assertEqual(event.status, 'partial')
        self.assertEqual(event.order_id, 'abc')
        self.assertEqual(event.event_ms, 10_000)

    def test_broker_ack_sets_true_active_time_and_open_slot(self):
        result = guardrails.apply_broker_event({
            'plan_id': 'plan-1', 'order_id': 'o-1', 'status': 'accepted',
            'event_ms': 1_785_000_005_000, 'quantity': 2,
        })
        self.assertTrue(result['matched'])
        row = self.load_row()
        self.assertEqual(row['broker_active_from_ms'], 1_785_000_005_000)
        self.assertEqual(row['broker_status'], 'accepted')
        self.assertTrue(guardrails._day_stats()['openpos'])

    def test_closed_event_drives_guard_pnl_and_frees_slot(self):
        guardrails.apply_broker_event({
            'plan_id': 'plan-1', 'order_id': 'o-1', 'status': 'filled',
            'event_ms': 1_785_000_005_000, 'filled_quantity': 2, 'avg_fill_price': 100,
        })
        result = guardrails.apply_broker_event({
            'plan_id': 'plan-1', 'order_id': 'o-1', 'status': 'closed',
            'event_ms': 1_785_000_008_000, 'realized_pnl': 200, 'exit_price': 105,
        })
        self.assertEqual(result['broker_outcome'], 'win')
        stats = guardrails._day_stats()
        self.assertFalse(stats['openpos'])
        self.assertEqual(stats['net'], 200)

    def test_unmatched_event_is_persisted(self):
        result = guardrails.apply_broker_event({'order_id': 'missing', 'status': 'accepted'})
        self.assertFalse(result['matched'])
        self.assertTrue(os.path.exists(guardrails.BUNMATCH))


if __name__ == '__main__':
    unittest.main()
