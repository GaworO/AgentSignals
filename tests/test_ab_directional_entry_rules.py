import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ab_directional_entry_rules as rules
import ab_directional_engine as engine

BOS = 1767268800000
ACT = BOS + 60000


class EntryRuleTests(unittest.TestCase):
    def check(self, side='LONG', close=105, **kw):
        return rules.evaluate(kw.get('category', 'PDH'), side,
                              kw.get('ohlc', dict(open=100, high=110, low=100, close=close)),
                              kw.get('bos', BOS), kw.get('activation', ACT))

    def test_exact_half_passes_and_below_half_fails(self):
        self.assertTrue(self.check()['eligible'])
        self.assertFalse(self.check(close=104.99)['eligible'])
        self.assertTrue(self.check(close=105.01)['eligible'])

    def test_short_requires_directional_not_absolute_body(self):
        bar = dict(open=110, high=110, low=100, close=105)
        self.assertTrue(self.check('SHORT', ohlc=bar)['eligible'])
        self.assertFalse(self.check('LONG', ohlc=bar)['eligible'])
        self.assertFalse(self.check('SHORT')['eligible'])

    def test_dib_also_excludes_orphan_combinations_in_both_directions(self):
        for side in ['LONG', 'SHORT']:
            for category in ['AL+DIB', 'PDH+DIB+ORPH', 'DIB', 'AL+ORPH+DIB']:
                with self.subTest(side=side, category=category):
                    self.assertEqual('V3_DIB_DISABLED', self.check(side, category=category)['rejection_reason'])
        self.assertTrue(self.check(category='PDH+ORPH')['eligible'])

    def test_unclosed_missing_zero_range_and_invalid_bars_rejected(self):
        self.assertEqual('V3_BOS_NOT_CLOSED', self.check(activation=ACT-1)['rejection_reason'])
        for ohlc in [None, {}, dict(open=100, high=100, low=100, close=100),
                     dict(open=100, high=110, low=100, close=111),
                     dict(open=100, high=110, low=100, close=float('nan'))]:
            with self.subTest(ohlc=ohlc):
                self.assertFalse(self.check(ohlc=ohlc)['eligible'])

    def test_dispatch_recomputes_evidence_instead_of_trusting_pass_flag(self):
        evidence = self.check()
        row = dict(strategy='AB_DIRECTIONAL', direction='LONG', activation_ms=ACT,
                   setup_category='PDH', entry_rules=evidence)
        self.assertIsNone(rules.dispatch_blocker(row))
        bad = copy.deepcopy(row)
        bad['entry_rules']['bos_ohlc']['close'] = 104
        self.assertEqual('v3_entry:V3_BOS_BODY_BELOW_50PCT', rules.dispatch_blocker(bad))
        bad = copy.deepcopy(row)
        bad['activation_ms'] += 60000
        self.assertEqual('v3_entry:activation_mismatch', rules.dispatch_blocker(bad))
        self.assertIsNotNone(rules.dispatch_blocker(dict(row, payload_json='[]')))
        self.assertIsNone(rules.dispatch_blocker(dict(strategy='CONTINUATION')))
        payload = json.dumps(dict(setup_category='PDH', entry_rules=evidence))
        self.assertIsNone(rules.dispatch_blocker(dict(row, payload_json=payload)))


class EngineGateIntegrationTests(unittest.TestCase):
    def test_changed_rule_dependency_fails_source_integrity_check(self):
        import continuation_shadow as shadow
        original = Path.read_bytes
        def read(path):
            return b'changed rule' if path.name == 'ab_directional_entry_rules.py' else original(path)
        with patch.object(Path, 'read_bytes', read):
            with self.assertRaisesRegex(RuntimeError, 'entry-rule source lock mismatch'):
                shadow._verify_freeze()

    def raw(self):
        return pd.DataFrame([
            [pd.Timestamp(BOS, unit='ms', tz='UTC'), 7, 100, 110, 100, 105, 1],
            [pd.Timestamp(ACT, unit='ms', tz='UTC'), 7, 105, 108, 98, 100, 1],
        ], columns=['ts_event', 'instrument_id', 'open', 'high', 'low', 'close', 'volume'])

    def output(self):
        return dict(instrument_id=7, bos_ms=BOS, entry_ms=ACT, cat='PDH',
                    fvg_bar=0, bos_bar=0, entry=100., SL=95., dir='LONG', epoch=0,
                    source_event=dict(bsl_name='PDH'))

    def test_gate_preserves_original_prices_expiry_ids_and_source(self):
        source = self.output()
        candidates, orders, funnel = engine.build_manifests(self.raw(), [source], 'LONG')
        self.assertEqual(1, len(orders))
        o = orders[0]
        self.assertEqual((101., 95., 113.), (o['entry_price'], o['structural_sl_price'], o['policy_B_target']))
        self.assertEqual(600, (o['expiry_timestamp'] - o['activation_timestamp']).total_seconds())
        self.assertEqual(source['source_event'], candidates[0]['source_event'])
        self.assertEqual(.5, o['entry_rules']['bos_body_fraction'])
        self.assertIsNone(rules.dispatch_blocker(dict(o, activation_ms=ACT)))
        self.assertEqual(1, funnel['resting_orders'])

    def test_future_bars_and_fill_hints_do_not_change_gate(self):
        raw = self.raw(); source = self.output()
        source['estimated_fill'] = True
        first = engine.build_manifests(raw.iloc[:1], [source], 'LONG')
        raw.loc[1, ['open', 'high', 'low', 'close']] = [200, 1000, 1, 500]
        later = engine.build_manifests(raw, [dict(source, pnl_usd=-9999)], 'LONG')
        self.assertEqual(first[0][0]['entry_rules'], later[0][0]['entry_rules'])
        self.assertEqual(first[1][0]['order_id'], later[1][0]['order_id'])

    def test_wrong_contract_missing_or_duplicate_bos_bar_creates_no_order(self):
        for raw in [self.raw().iloc[1:], self.raw().assign(instrument_id=8),
                    pd.concat([self.raw().iloc[:1], self.raw()], ignore_index=True)]:
            candidates, orders, _ = engine.build_manifests(raw, [self.output()], 'LONG')
            self.assertFalse(candidates[0]['eligible'])
            self.assertEqual([], orders)

    def test_rejected_setup_remains_visible_and_never_becomes_an_order(self):
        for category in ['PDH+DIB', 'PDH+DIB+ORPH']:
            candidates, orders, funnel = engine.build_manifests(self.raw(), [dict(self.output(), cat=category)], 'LONG')
            self.assertEqual([], orders)
            self.assertEqual('V3_DIB_DISABLED', candidates[0]['rejection_reason'])
            self.assertEqual(1, funnel['principal_rejection_reasons']['V3_DIB_DISABLED'])


if __name__ == '__main__':
    unittest.main()
