import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import allview


class ContinuationAllViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "continuation.sqlite3")
        con = sqlite3.connect(self.db)
        con.executescript(
            """
            CREATE TABLE continuation_trades(
              trade_id TEXT, direction TEXT, fill_ms INTEGER, entry_price REAL,
              stop_price REAL, target_price REAL, net_r REAL, state TEXT,
              exit_reason TEXT
            );
            CREATE TABLE continuation_candidates(
              direction TEXT, decision_ms INTEGER, stage TEXT, status TEXT,
              rejection_reason TEXT, dol_id TEXT
            );
            """
        )
        con.executemany(
            "INSERT INTO continuation_trades VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("tl", "LONG", 1_700_000_000_000, 100.0, 90.0, 130.0, 3.0, "CLOSED", "TARGET"),
                ("ts", "SHORT", 1_700_000_060_000, 100.0, 110.0, 70.0, -1.0, "CLOSED", "STOP"),
            ],
        )
        con.executemany(
            "INSERT INTO continuation_candidates VALUES(?,?,?,?,?,?)",
            [
                ("LONG", 1_700_000_000_000, "ORDER", "ACCEPTED", None, "dol-l"),
                ("SHORT", 1_700_000_060_000, "ORDER", "ACCEPTED", None, "dol-s"),
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_and_short_appear_in_trade_and_candidate_tables(self):
        with mock.patch.object(allview, "_CONT_DB", self.db), \
             mock.patch.object(allview, "_OUTCOMES", os.path.join(self.tmp.name, "missing.json")), \
             mock.patch.object(allview, "_ANNOT_DB", os.path.join(self.tmp.name, "annotations.sqlite3")), \
             mock.patch.dict(os.environ, {"STRAT_C_URL": "", "STRAT_F_URL": ""}):
            trades = allview._continuation_trades()
            candidates = allview._continuation_candidates()
            trade_html = allview.render_trades()
            candidate_html = allview.render_candidates()

        self.assertEqual({row["strat"] for row in trades}, {"CONT-L", "CONT-S"})
        self.assertEqual({row["strat"] for row in candidates}, {"CONT-L", "CONT-S"})
        self.assertEqual({row["target"] for row in trades}, {130.0, 70.0})
        self.assertIn("CONT-L", trade_html)
        self.assertIn("CONT-S", trade_html)
        self.assertIn("CONT-L", candidate_html)
        self.assertIn("CONT-S", candidate_html)


if __name__ == "__main__":
    unittest.main()
