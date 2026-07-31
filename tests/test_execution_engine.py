import unittest

from execution_engine import ExecutionState, resolve_arrays, step
from execution_plan import ExecutionConfig, build_execution_plan


class ExecutionEngineTests(unittest.TestCase):
    def plan(self):
        signal = {
            "dir": "LONG", "entry": 100.0, "SL": 95.0, "TP": 110.0,
            "bos_ms": 0, "entry_ms": 60_000, "date": "x", "model": "x", "cat": "x", "bos": "x",
        }
        cfg = ExecutionConfig(tick_size=0.25, fill_window_min=10, fill_through_ticks=1)
        return build_execution_plan(signal, 1, config=cfg, signal_key="k", now_ms=1)

    def test_first_active_bar_can_fill(self):
        p = self.plan(); s = ExecutionState.for_plan(p)
        step(p, s, bar_ms=60_000, high=101.0, low=99.75)
        self.assertEqual(s.status, "open")
        self.assertEqual(s.filled_ms, 60_000)

    def test_touch_without_trade_through_does_not_fill(self):
        p = self.plan(); s = ExecutionState.for_plan(p)
        step(p, s, bar_ms=60_000, high=101.0, low=100.0)
        self.assertEqual(s.status, "pending")

    def test_fill_bar_stop_is_loss_and_fill_bar_tp_is_ignored(self):
        p = self.plan(); s = ExecutionState.for_plan(p)
        step(p, s, bar_ms=60_000, high=111.0, low=94.0)
        self.assertEqual(s.status, "closed")
        self.assertEqual(s.outcome, "loss")
        self.assertEqual(s.realized_gross_r, -1.0)

    def test_no_fill_at_valid_until(self):
        p = self.plan(); s = ExecutionState.for_plan(p)
        step(p, s, bar_ms=p.valid_until_ms, high=101.0, low=99.0)
        self.assertEqual(s.outcome, "no_fill")

    def test_array_resolution_uses_same_rules(self):
        p = self.plan()
        result = resolve_arrays(
            p,
            [0, 60_000, 120_000, 180_000],
            [100, 101, 106, 111],
            [100, 99.75, 100, 105],
        )
        self.assertEqual(result["outcome"], "win")
        self.assertEqual(result["gross_R"], 2.0)


if __name__ == "__main__":
    unittest.main()
