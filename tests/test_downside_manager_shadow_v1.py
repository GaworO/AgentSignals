"""Focused parity and safety checks for the broker-inert shadow."""
import ast
import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import downside_manager_shadow_v1 as shadow
from rl_trade_manager.downside_manager import prepare_split


class DownsideShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prepared, _ = prepare_split("train")
        cls.saved = {row["trade_id"]: row for row in json.loads(
            (shadow.HERE / "rl_trade_manager/downside_manager_v1/train_report.json").read_text())["rows"]}

    def test_frozen_control_and_manager_replay(self):
        model = shadow._model()
        for trade, _, _, _ in self.prepared[:15]:
            signal_ms = (dt.datetime.fromtimestamp(trade.fvg_signal_ms / 1000, dt.timezone.utc).isoformat()
                         if trade.fvg_signal_ms else None)
            row = {"trade_id": trade.key, "strategy_id": trade.strategy_id,
                   "direction": trade.direction, "entry": trade.entry,
                   "initial_sl": trade.initial_sl, "fixed_tp": trade.target,
                   "quantity": trade.qty, "initial_risk": trade.risk,
                   "fill_ms": trade.fill_ms,
                   "signal_json": json.dumps({"fvg_lo": trade.fvg_low,
                                              "fvg_hi": trade.fvg_high,
                                              "ce": trade.fvg_ce,
                                              "emitted": signal_ms})}
            args = (row, list(trade.bars), list(trade.pre_bars), list(trade.dol_states), model, "COMPLETE")
            first = shadow._replay(*args)
            second = shadow._replay(*args)
            expected = self.saved[trade.key]
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "DONE")
            self.assertAlmostEqual(first["control_final_r"], expected["base_r"], places=9)
            self.assertAlmostEqual(first["manager_final_r"], expected["policy_r"], places=9)
            self.assertEqual(first["manager_exit_reason"], expected["exit_reason"])
            self.assertEqual(trade.target, shadow._fixed_2r_price(trade.entry, trade.direction, trade.risk))
            self.assertGreaterEqual(trade.direction * (first["manager_virtual_sl"] - trade.initial_sl), -1e-10)
            # The frozen outcome is reached without an entry intervention or
            # any action after the first +2R target fill.
            self.assertEqual(expected["entry_action"], 0)
            self.assertLessEqual(first["_last_processed_i"], len(trade.bars))

    def test_restart_idempotency_and_no_broker_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal = {"_strat": "A/B", "cls": "B", "model": "Cont", "dir": "LONG",
                      "entry": 100.0, "SL": 90.0, "bos_ms": 1_750_000_000_000,
                      "entry_ms": 1_750_000_060_000, "fvg_lo": 97.0, "fvg_hi": 101.0,
                      "ce": 99.0, "emitted": "2025-06-15T10:00:00+00:00"}
            with patch.object(shadow, "DATA_DIR", root), patch.object(shadow, "DB", root / "shadow.sqlite3"), patch.object(shadow, "ENABLED", True):
                shadow.migrate()
                self.assertEqual(shadow.observe_signal(signal, "source-1", 3), 1)
                self.assertEqual(shadow.observe_signal(signal, "source-1", 3), 0)
                with shadow._connect() as connection:
                    count = connection.execute("SELECT COUNT(*) FROM downside_shadow_trades").fetchone()[0]
                    row = connection.execute("SELECT fixed_tp, quantity, status FROM downside_shadow_trades").fetchone()
                self.assertEqual(count, 1)
                self.assertEqual((row["fixed_tp"], row["quantity"], row["status"]), (120.0, 3, "PENDING"))

        tree = ast.parse(Path(shadow.__file__).read_text())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])}
        self.assertTrue(imports.isdisjoint({"live_emit", "guardrails", "requests", "traderspost"}))

    def test_buffer_catchup_and_terminal_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = 1_750_000_000_000 // 60_000 * 60_000
            feed = root / "buffer.csv"
            with feed.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("ts_event", "open", "high", "low", "close"))
                for i in range(120):
                    timestamp = dt.datetime.fromtimestamp((start + (i - 120) * 60_000) / 1000,
                                                          dt.timezone.utc).isoformat()
                    writer.writerow((timestamp, 105, 106, 104, 105))
                for i, values in enumerate(((100, 101, 99.75, 100), (101, 121, 100, 120))):
                    timestamp = dt.datetime.fromtimestamp((start + (i + 1) * 60_000) / 1000,
                                                          dt.timezone.utc).isoformat()
                    writer.writerow((timestamp, *values))
            signal = {"_strat": "A/B", "cls": "B", "model": "Cont", "dir": "LONG",
                      "entry": 100.0, "SL": 90.0, "bos_ms": start,
                      "entry_ms": start, "fvg_lo": 97.0, "fvg_hi": 101.0,
                      "ce": 99.0, "emitted": dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).isoformat()}
            with (patch.object(shadow, "DATA_DIR", root), patch.object(shadow, "DB", root / "shadow.sqlite3"),
                  patch.object(shadow, "BUFFER", feed), patch.object(shadow, "ENABLED", True),
                  patch("ab_dol_live._engine_for", return_value=object()),
                  patch.object(shadow, "_raw_state", side_effect=lambda engine, ms: shadow._placeholder(ms)),
                  patch.object(shadow, "_next_state", side_effect=lambda engine, state, bar: shadow._placeholder(bar.ms + 60_000))):
                shadow.migrate()
                self.assertEqual(shadow.observe_signal(signal, "source-2", 2), 1)
                self.assertEqual(shadow.refresh(), 1)
                self.assertEqual(shadow.refresh(), 0)
                with shadow._connect() as connection:
                    row = connection.execute("SELECT * FROM downside_shadow_trades").fetchone()
                    decisions = connection.execute("SELECT COUNT(*) FROM downside_shadow_decisions").fetchone()[0]
                self.assertEqual(row["status"], "DONE")
                self.assertAlmostEqual(row["control_final_r"], 2 - 2.24 / 20, places=9)
                self.assertEqual(len(json.loads(row["bars_json"])), 2)
                self.assertEqual(decisions, 1)


if __name__ == "__main__":
    unittest.main()
