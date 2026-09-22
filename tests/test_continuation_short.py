"""Direction and accounting regression tests for exploratory SHORT shadow."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import continuation_shadow as shadow


def frame(rows):
    return pd.DataFrame(rows, columns=["ts_event", "instrument_id", "open", "high", "low", "close", "volume"])


class ShortShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        shadow.DB_PATH = Path(self.temp.name) / "shadow.sqlite3"
        shadow._init_db()

    def tearDown(self):
        self.temp.cleanup()

    def _pending(self, base, candidate_id="SHORT_1", order_id="ORDER_SHORT_1"):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        ms = int(base.timestamp() * 1000)
        with shadow._connect() as con:
            con.execute(
                "INSERT INTO continuation_candidates(candidate_id,direction,event_kind,decision_ms,stage,status,eligible,forward_eligible,payload_json,first_seen_at,updated_at) "
                "VALUES(?, 'SHORT', 'CANONICAL_OUTPUT', ?, 'ORDER', 'ORDER', 1, 1, '{}', ?, ?)",
                (candidate_id, ms, now, now),
            )
            con.execute(
                "INSERT INTO continuation_orders(order_id,direction,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,stop_price,target_price,risk_points,dol_id,state,payload_json,created_at,updated_at) "
                "VALUES(?, 'SHORT', ?, 7, ?, ?, 100, 102, 96, 2, 'BEAR_DOL', 'PENDING', '{}', ?, ?)",
                (order_id, candidate_id, ms, ms + 600_000, now, now),
            )

    def test_short_fill_requires_one_tick_above_entry_and_fill_bar_is_inert(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        self._pending(base)
        raw = frame([
            (base, 7, 100, 100.00, 95, 97, 1),  # touches Entry but not tick-through
            (base + pd.Timedelta(minutes=1), 7, 99, 100.25, 95, 96, 1),  # fills; TP on fill bar ignored
        ])
        shadow._reconcile(raw)
        with shadow._connect() as con:
            trade = con.execute("SELECT * FROM continuation_trades").fetchone()
            self.assertEqual(trade["direction"], "SHORT")
            self.assertEqual(trade["state"], "OPEN")
            self.assertEqual(trade["fill_ms"], int((base + pd.Timedelta(minutes=1)).timestamp() * 1000))

    def test_short_ambiguous_next_bar_stops_first_at_adverse_gap(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        self._pending(base)
        raw = frame([
            (base, 7, 100, 100.25, 99, 100, 1),
            (base + pd.Timedelta(minutes=1), 7, 103, 104, 95, 98, 1),
        ])
        shadow._reconcile(raw)
        with shadow._connect() as con:
            trade = con.execute("SELECT * FROM continuation_trades").fetchone()
            self.assertEqual(trade["exit_reason"], "STRUCTURAL_SL")
            self.assertEqual(trade["exit_price"], 103.0)
            self.assertEqual(trade["raw_r"], -1.5)
            self.assertEqual(trade["cost_r"], 3.50 / 4.0)

    def test_short_target_is_profitable_and_cost_charged_once(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        self._pending(base)
        raw = frame([
            (base, 7, 100, 100.25, 99, 100, 1),
            (base + pd.Timedelta(minutes=1), 7, 99, 100, 95, 96, 1),
        ])
        shadow._reconcile(raw)
        with shadow._connect() as con:
            trade = con.execute("SELECT * FROM continuation_trades").fetchone()
            self.assertEqual(trade["exit_reason"], "FROZEN_OPEN_DOL")
            self.assertEqual(trade["exit_price"], 96.0)
            self.assertEqual(trade["raw_r"], 2.0)
            self.assertEqual(trade["net_r"], 2.0 - 3.50 / 4.0)

    def test_existing_long_database_is_migrated_without_changing_direction(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as con:
            con.execute("CREATE TABLE continuation_candidates(candidate_id TEXT PRIMARY KEY, decision_ms INTEGER, status TEXT, rejection_reason TEXT)")
            con.execute("CREATE TABLE continuation_orders(order_id TEXT PRIMARY KEY, state TEXT)")
            con.execute("CREATE TABLE continuation_trades(trade_id TEXT PRIMARY KEY, state TEXT)")
            con.execute("INSERT INTO continuation_candidates VALUES('OLD_LONG', 1, 'REJECTED', NULL)")
        shadow.DB_PATH = legacy
        shadow._init_db()
        with shadow._connect() as con:
            self.assertEqual(con.execute("SELECT direction FROM continuation_candidates WHERE candidate_id='OLD_LONG'").fetchone()[0], "LONG")
            for table in ("continuation_candidates", "continuation_orders", "continuation_trades"):
                self.assertIn("direction", {row[1] for row in con.execute(f"PRAGMA table_info({table})")})

    def test_short_source_lock_and_original_long_freeze_verify(self):
        provenance = shadow._verify_freeze()
        self.assertEqual(provenance["short_research_identity"], shadow.SHORT_IDENTITY)
        self.assertEqual(len(provenance["short_engine_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
