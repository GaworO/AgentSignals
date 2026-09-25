import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ab_quality
import allview


def signal(**overrides):
    value = {
        "s": 10, "u": 15, "bos_bar": 19,
        "entry": 100.0, "SL": 90.0, "signal_close": 103.0,
        "fvg_lo": 101.0, "fvg_hi": 106.0, "atr5": 10.0,
    }
    value.update(overrides)
    return value


class AbQualityTests(unittest.TestCase):
    def test_quality_is_q3_when_three_or_four_checks_pass(self):
        q = ab_quality.classify(signal())
        self.assertEqual(q["tier"], "Q3")
        self.assertEqual(q["score"], 4)
        self.assertEqual(q["suggested_risk_mult"], 1.0)
        self.assertEqual(q["mode"], "SHADOW_ONLY")

    def test_quality_q1_is_reduced_suggestion_only(self):
        q = ab_quality.classify(signal(u=12, bos_bar=13, signal_close=108.0))
        self.assertEqual(q["tier"], "Q1")
        self.assertEqual(q["suggested_risk_mult"], 0.25)
        self.assertTrue(q["suggestion_only"])

    def test_allview_reads_persisted_quality(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); outcomes = root / "outcomes.json"; db = root / "journal.db"
            key = "AB|one"
            outcomes.write_text(json.dumps([{
                "key": key, "bos_ms": 1_700_000_000_000, "dir": "LONG", "cat": "x",
                "entry": 100, "sl": 90, "r": 2, "reason": "tp",
            }]))
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE signals(key TEXT, quality_json TEXT)")
            con.execute("INSERT INTO signals VALUES(?,?)", (
                key, json.dumps({"tier": "Q2", "suggested_risk_mult": 1.0})))
            con.commit(); con.close()
            with patch.object(allview, "_OUTCOMES", str(outcomes)), \
                 patch.object(allview, "_SIGNALS_DB", str(db)), \
                 patch.object(allview, "_CONT_DB", str(root / "missing.sqlite")), \
                 patch.object(allview, "_ANNOT_DB", str(root / "annotations.sqlite")):
                rows = allview._ab_trades()
                self.assertEqual(rows[0]["quality"]["tier"], "Q2")
                self.assertIn("Quality", allview.render_trades())


if __name__ == "__main__":
    unittest.main()
