"""Guard latches unconfirmed entries without inventing a position or closure."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import v3_position_manager as manager


class GuardEodPositionsTests(unittest.TestCase):
    def test_unconfirmed_entries_stay_unresolved_and_cannot_be_resurrected(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        c.execute("CREATE TABLE orders(order_id TEXT,state TEXT,position_json TEXT,updated_ms INTEGER)")
        rows = [
            ("prepared", "PREPARED", None),
            ("waiting", "WAITING_BROKER_FILL", None),
            ("assumed", "ASSUMED_OPEN", json.dumps({"assumed": True, "quantity": 1})),
            ("confirmed", "BROKER_CONFIRMED_OPEN", json.dumps({"quantity": 1})),
            ("unknown", "EXIT_UNKNOWN", json.dumps({"quantity": 1})),
            ("empty", "ASSUMED_OPEN", None),
            ("no_fill", "NO_FILL", None),
            ("already_pending", "GUARD_EXIT_PENDING", json.dumps({"quantity": 1})),
        ]
        c.executemany("INSERT INTO orders VALUES(?,?,?,1)", rows)
        c.commit()
        @contextmanager
        def connect():
            with c:
                yield c
        transitions = []
        api = SimpleNamespace(connect=connect, now_ms=lambda: 2,
                              _exit_transition=lambda *args: transitions.append(args[1:]))
        with patch.object(manager, "live", return_value=api):
            manager.guard_exit_requested("eod_15:55")
            manager.guard_exit_requested("eod_15:55")
        actual = dict(c.execute("SELECT order_id,state FROM orders"))
        expected_transfers = {"prepared", "waiting", "assumed", "confirmed", "unknown", "empty"}
        self.assertEqual({row[0] for row in transitions}, expected_transfers)
        self.assertEqual(len(transitions), 6)
        for oid, state, _ in rows:
            self.assertEqual(actual[oid], "GUARD_EXIT_PENDING" if oid in expected_transfers else state)
        self.assertTrue(all(row[1:] == ("EXIT_REQUESTED", 2, "guard:eod_15:55") for row in transitions))
        for oid in ('prepared', 'waiting', 'empty'):
            self.assertIsNone(c.execute('SELECT position_json FROM orders WHERE order_id=?',(oid,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
