import unittest

from execution_plan import (
    ExecutionConfig,
    ExecutionPlan,
    PlanValidationError,
    attach_plan,
    build_execution_plan,
)


class ExecutionPlanTests(unittest.TestCase):
    def signal(self, direction="LONG"):
        return {
            "date": "2026-07-30",
            "model": "Cont",
            "cat": "NYAML + PDL",
            "dir": direction,
            "bos": "09:45",
            "bos_ms": 1_000_000,
            "entry_ms": 1_060_000,
            "entry": 20000.13 if direction == "LONG" else 20000.12,
            "SL": 19980.11 if direction == "LONG" else 20020.11,
            "TP": 20035.37 if direction == "LONG" else 19965.37,
        }

    def test_detector_target_is_canonical_target(self):
        cfg = ExecutionConfig(tick_size=0.25, fill_window_min=10)
        p = build_execution_plan(self.signal(), 5, config=cfg, now_ms=2_000_000, signal_key="k")
        self.assertEqual(p.entry, 20000.25)
        self.assertEqual(p.stop_loss, 19980.0)
        self.assertEqual(p.primary_take_profit, 20035.25)
        self.assertNotEqual(p.primary_take_profit, p.entry + 2 * p.risk_points)
        self.assertEqual(p.active_from_ms, 1_060_000)
        self.assertEqual(p.valid_until_ms, 1_660_000)

    def test_partial_legs_are_built_from_final_tick_aligned_risk(self):
        cfg = ExecutionConfig(tick_size=0.25, partial_at_1r=True, risk_pct=0.5, partial_account_pct=0.2)
        p = build_execution_plan(self.signal(), 5, config=cfg, signal_key="k")
        self.assertEqual([x.quantity for x in p.legs], [2, 3])
        self.assertEqual(p.legs[0].label, "banker_1R")
        self.assertAlmostEqual(p.legs[0].take_profit, p.entry + p.risk_points)

    def test_payload_and_attached_legacy_fields_are_identical(self):
        signal = self.signal("SHORT")
        p = build_execution_plan(signal, 3, config=ExecutionConfig(tick_size=0.25), signal_key="guard-key")
        attach_plan(signal, p)
        payload = p.broker_payloads("hello")[0]
        self.assertEqual(payload["limitPrice"], signal["_exec_entry"])
        self.assertEqual(payload["stopLoss"]["stopPrice"], signal["_exec_sl"])
        self.assertEqual(payload["takeProfit"]["limitPrice"], signal["_exec_tp"])
        self.assertEqual(signal["_signal_key"], "guard-key")

    def test_round_trip_serialization(self):
        p = build_execution_plan(self.signal(), 2, config=ExecutionConfig(), signal_key="k")
        self.assertEqual(ExecutionPlan.from_dict(p.to_dict()), p)

    def test_invalid_directional_levels_fail_closed(self):
        sig = self.signal()
        sig["SL"] = sig["entry"] + 10
        with self.assertRaises(PlanValidationError):
            build_execution_plan(sig, 1, config=ExecutionConfig(), signal_key="k")


if __name__ == "__main__":
    unittest.main()
