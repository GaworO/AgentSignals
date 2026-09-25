import os
import tempfile
import unittest
import sys
import types
from pathlib import Path
from unittest import mock

try:
    import flask  # noqa: F401
except ImportError:
    stub = types.ModuleType("flask")
    stub.Response = object
    stub.jsonify = lambda *args, **kwargs: None
    stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = stub

import continuation_live as live
import continuation_shadow as shadow


class ContinuationLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_shadow_db = shadow.DB_PATH
        self.old_live_db = live.DB_PATH
        self.old_dispatcher = live._DISPATCHER
        shadow.DB_PATH = root / "shadow.sqlite3"
        live.DB_PATH = root / "live.sqlite3"
        live._DISPATCHER = None
        shadow._init_db(); live._init_db()

    def tearDown(self):
        shadow.DB_PATH = self.old_shadow_db
        live.DB_PATH = self.old_live_db
        live._DISPATCHER = self.old_dispatcher
        self.tmp.cleanup()

    def _bar(self, ms):
        with shadow._connect() as con:
            shadow._set_meta(con, "last_bar_ms", ms)

    def _order(self, activation, direction="LONG", suffix="1", expiry=None):
        expiry = expiry or activation + 600_000
        candidate = "C" + suffix; order = "O" + suffix
        entry, stop, target = ((20000.0, 19990.0, 20030.0) if direction == "LONG"
                               else (20000.0, 20010.0, 19970.0))
        with shadow._connect() as con:
            con.execute(
                """INSERT INTO continuation_candidates
                   (candidate_id,direction,event_kind,decision_ms,trading_day,stage,status,eligible,
                    forward_eligible,payload_json,first_seen_at,updated_at)
                   VALUES(?,?,'CANONICAL_OUTPUT',?,?,'ORDER','FILLED_OR_PENDING_REPLAY',1,1,'{}','x','x')""",
                (candidate, direction, activation, "2026-09-23"),
            )
            con.execute(
                """INSERT INTO continuation_orders
                   (order_id,direction,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,
                    stop_price,target_price,risk_points,dol_id,state,payload_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING','{}','x','x')""",
                (order, direction, candidate, 1, activation, expiry, entry, stop, target, 10.0, "DOL" + suffix),
            )
        return order

    def test_first_drain_arms_without_catchup(self):
        self._bar(1_000_000); self._order(900_000)
        called = []
        live.configure(lambda row: called.append(row) or {"state": "SENT"})
        with mock.patch.dict(os.environ, {"CONTINUATION_LIVE_LONG": "1"}, clear=False):
            result = live.drain()
        self.assertEqual("armed", result["status"])
        self.assertEqual([], called)
        self.assertEqual([], live.rows())

    def test_new_enabled_order_dispatches_once(self):
        self._bar(1_000_000)
        live.configure(lambda row: {"state": "SENT", "quantity": 3, "route_id": "route"})
        live.drain()
        order_id = self._order(1_060_000, expiry=1_600_000); self._bar(1_060_000)
        with mock.patch.dict(os.environ, {"CONTINUATION_LIVE_LONG": "1"}, clear=False):
            first = live.drain(); second = live.drain()
        self.assertEqual(1, first["processed"])
        self.assertEqual(0, second["processed"])
        row = live.rows()[0]
        self.assertEqual(order_id, row["order_id"])
        self.assertEqual("SENT", row["state"])
        self.assertEqual(3, row["quantity"])
        self.assertEqual("CONT-L", row["strategy_class"])
        self.assertEqual("FROZEN OPEN DOL", row["setup_class"])
        self.assertEqual("N/A", row["quality_tier"])
        self.assertEqual("DOL1", row["dol_id"])

    def test_disabled_direction_is_terminal(self):
        self._bar(2_000_000); live.configure(lambda row: {"state": "SENT"}); live.drain()
        self._order(2_060_000, direction="SHORT", suffix="s", expiry=2_600_000); self._bar(2_060_000)
        with mock.patch.dict(os.environ, {"CONTINUATION_LIVE_SHORT": "0"}, clear=False):
            result = live.drain()
        self.assertEqual(1, result["processed"])
        self.assertEqual("DISABLED", live.rows()[0]["state"])

    def test_expired_order_is_never_dispatched(self):
        self._bar(3_000_000); live.configure(lambda row: {"state": "SENT"}); live.drain()
        self._order(3_010_000, suffix="e", expiry=3_020_000); self._bar(3_030_000)
        with mock.patch.dict(os.environ, {"CONTINUATION_LIVE_LONG": "1"}, clear=False):
            result = live.drain()
        self.assertEqual(0, result["processed"])
        self.assertEqual([], live.rows())


if __name__ == "__main__":
    unittest.main()
