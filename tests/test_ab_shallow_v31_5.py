import unittest

import ab_shallow
import live_emit


class ABShallowTests(unittest.TestCase):
    def base_signal(self, direction="SHORT"):
        return {
            "date": "2026-07-31",
            "model": "Reversal",
            "cat": "F.P.FVG",
            "dir": direction,
            "bos": "09:42",
            "bos_ms": 1785505320000,
            "entry": 28545.0 if direction == "SHORT" else 28545.0,
            "SL": 28576.0 if direction == "SHORT" else 28514.0,
            "TP": 28456.5 if direction == "SHORT" else 28633.5,
            "_signal_close": 28478.5 if direction == "SHORT" else 28611.5,
            "sl_src": "fvg_edge",
            "tp_src": "swing",
        }

    def env(self, **extra):
        out = {
            "AB_SHALLOW_ENABLED": "1",
            "AB_SHALLOW_FRACTION": "0.25",
            "AB_SHALLOW_RR": "2",
            "AB_SHALLOW_RISK_PCT": "0.5",
            "RISK_PCT": "0.5",
            "ACCOUNT": "100000",
            "POINT_VALUE": "2",
            "EXEC_TICK": "0.25",
            "AB_SHALLOW_MIN_SL_PTS": "5",
            "AB_SHALLOW_MAX_SL_PTS": "0",
        }
        out.update(extra)
        return out

    def test_short_shallow_entry_and_2r_target(self):
        child = ab_shallow.build_shallow_signal(self.base_signal("SHORT"), self.env())
        self.assertEqual(child["_strat"], "A/B-shallow")
        self.assertEqual(child["entry"], 28495.0)
        self.assertEqual(child["SL"], 28576.0)
        self.assertEqual(child["TP"], 28333.0)
        self.assertEqual(child["tp_src"], "shallow_2R")
        self.assertEqual(child["_risk_pct_override"], 0.5)
        self.assertTrue(child["_strict_risk_budget"])

    def test_long_shallow_entry_and_3r_target(self):
        child = ab_shallow.build_shallow_signal(
            self.base_signal("LONG"), self.env(AB_SHALLOW_RR="3")
        )
        self.assertEqual(child["entry"], 28595.0)
        self.assertEqual(child["SL"], 28514.0)
        self.assertEqual(child["TP"], 28838.0)
        self.assertEqual(child["tp_src"], "shallow_3R")

    def test_independent_risk_metadata(self):
        deep = self.base_signal("SHORT")
        child = ab_shallow.build_shallow_signal(deep, self.env())
        meta = ab_shallow.risk_metadata(deep, child, self.env())
        self.assertEqual(meta["deep_risk_pct"], 0.5)
        self.assertEqual(meta["shallow_risk_pct"], 0.5)
        self.assertEqual(meta["combined_max_risk_pct"], 1.0)
        self.assertEqual(meta["deep_budget"], 500.0)
        self.assertEqual(meta["shallow_budget"], 500.0)

    def test_refuses_one_contract_over_budget(self):
        sig = self.base_signal("SHORT")
        sig["SL"] = 28800.0
        with self.assertRaisesRegex(ValueError, "cannot fit one contract"):
            ab_shallow.build_shallow_signal(sig, self.env())

    def test_size_for_risk_override(self):
        qty, slpts, per_contract, real, pct = live_emit.size_for(100.0, 120.0, 0.5)
        self.assertEqual(qty, 12)
        self.assertEqual(slpts, 20.0)
        self.assertEqual(per_contract, 40)
        self.assertEqual(real, 480)
        self.assertEqual(pct, 0.48)


if __name__ == "__main__":
    unittest.main()
