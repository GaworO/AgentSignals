import unittest

import pandas as pd

import ab_directional_engine as engine


class AbDirectionalEngineTests(unittest.TestCase):
    def _raw(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        return pd.DataFrame([
            (base, 7, 100, 101, 99, 100, 1),
            (base + pd.Timedelta(minutes=1), 7, 100, 102, 98, 101, 1),
        ], columns=["ts_event", "instrument_id", "open", "high", "low", "close", "volume"])

    def _output(self, side):
        return {"instrument_id": 7, "bos_ms": 1767268800000, "entry_ms": 1767268800000,
                "cat": "TEST", "fvg_bar": 1, "entry": 100.0,
                "SL": 95.0 if side == "LONG" else 105.0, "dir": side, "epoch": 0,
                "source_event": {"bsl_name" if side == "LONG" else "ssl_name": "TEST"}}

    def test_long_fixed_2r_geometry_has_no_htf_or_dol_gate(self):
        candidates, orders, _ = engine.build_manifests(self._raw(), [self._output("LONG")], "LONG")
        self.assertTrue(candidates[0]["eligible"])
        self.assertEqual(101.0, orders[0]["entry_price"])
        self.assertEqual(95.0, orders[0]["structural_sl_price"])
        self.assertEqual(113.0, orders[0]["policy_B_target"])
        self.assertEqual("FIXED_2R", orders[0]["dol_id"])

    def test_short_is_exact_directional_mirror(self):
        candidates, orders, _ = engine.build_manifests(self._raw(), [self._output("SHORT")], "SHORT")
        self.assertTrue(candidates[0]["eligible"])
        self.assertEqual(99.0, orders[0]["entry_price"])
        self.assertEqual(105.0, orders[0]["structural_sl_price"])
        self.assertEqual(87.0, orders[0]["policy_B_target"])
        self.assertEqual("SHORT", orders[0]["direction"])

    def test_same_live_geometry_creates_only_one_order(self):
        first = self._output("LONG")
        second = dict(first, cat="OTHER_LIQUIDITY_NAME")
        second["source_event"] = {"bsl_name": "OTHER"}
        candidates, orders, funnel = engine.build_manifests(self._raw(), [first, second], "LONG")
        self.assertEqual(1, len(candidates))
        self.assertEqual(1, len(orders))
        self.assertEqual(1, funnel["physical_geometry_duplicates_removed"])


if __name__ == "__main__":
    unittest.main()
