import json
import os
import tempfile
import unittest
from unittest import mock

import guardrails


class GuardShallowGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        guardrails.DATA_DIR = self.tmp.name
        guardrails.GLOG = os.path.join(self.tmp.name, 'guard_log.json')
        guardrails.GSTATE = os.path.join(self.tmp.name, 'guard_state.json')
        self.env = mock.patch.dict(os.environ, {
            'GUARD_TRADE_ALERTS': '0',
            'START_EQUITY': '100000',
            'POINT_VALUE': '2',
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_sibling_notes_increment_ramp_once(self):
        base = {
            '_setup_group_id': 'g1', 'bos_ms': 1000, 'dir': 'SHORT',
            'entry': 100.0, 'SL': 110.0, 'TP': 80.0,
            '_sent_qty': 2, 'tp_src': 'swing', 'sl_src': 'struct',
        }
        deep = dict(base, _strat='A/B')
        shallow = dict(base, _strat='A/B-shallow', entry=95.0, TP=65.0, tp_src='shallow_2R')
        guardrails.note(deep, 'sent')
        guardrails.note(shallow, 'sent')
        with open(guardrails.GSTATE) as f:
            state = json.load(f)
        with open(guardrails.GLOG) as f:
            rows = json.load(f)
        self.assertEqual(state['sent_total'], 1)
        self.assertEqual([r['strat'] for r in rows], ['A/B', 'A/B-shallow'])
        self.assertEqual(rows[0]['setup_group_id'], 'g1')
        self.assertEqual(rows[1]['setup_group_id'], 'g1')
        self.assertNotEqual(rows[0]['key'], rows[1]['key'])

    def test_day_stats_counts_setup_once_but_each_losing_leg(self):
        sample = [
            {'setup_group_id': 'g1', 'strat': 'A/B', 'outcome': 'loss', 'net': -100},
            {'setup_group_id': 'g1', 'strat': 'A/B-shallow', 'outcome': 'loss', 'net': -200},
            {'setup_group_id': 'g2', 'strat': 'A/B', 'outcome': 'no_fill', 'net': 0},
            {'setup_group_id': 'g2', 'strat': 'A/B-shallow', 'outcome': 'no_fill', 'net': 0},
        ]
        with mock.patch.object(guardrails, '_today_sent', return_value=sample):
            stats = guardrails._day_stats()
        self.assertEqual(stats['sent'], 1)
        self.assertEqual(stats['losses'], 2)
        self.assertEqual(stats['net'], -300)
        self.assertFalse(stats['openpos'])


if __name__ == '__main__':
    unittest.main()
