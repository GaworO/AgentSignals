import json
import os
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import guardrails


CSV_HEADER = "qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp,symbol\n"


class GuardReconcilePineV77Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        guardrails.DATA_DIR = self.tmp.name
        guardrails.GLOG = os.path.join(self.tmp.name, "guard_log.json")
        guardrails.GSTATE = os.path.join(self.tmp.name, "guard_state.json")
        self.env = mock.patch.dict(os.environ, {
            "RECONCILE_CSV_TZ": "Europe/Copenhagen",
            "RECONCILE_MATCH_MIN": "90",
            "RECONCILE_ENTRY_PTS": "2",
            "RECONCILE_PARTIAL_SEC": "120",
            "RECONCILE_PARTIAL_ENTRY_PTS": "4",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def et_ms(year, month, day, hour, minute, second=0):
        value = __import__("datetime").datetime(
            year, month, day, hour, minute, second, tzinfo=ZoneInfo("America/New_York")
        )
        return int(value.timestamp() * 1000)

    def test_partial_fills_are_aggregated_and_matched_by_et_time(self):
        weighted_entry = (1 * 29469.5 + 7 * 29466.75) / 8
        glog = [{
            "key": "ab-1", "decision": "sent", "date": "2026-08-21",
            "ts": self.et_ms(2026, 8, 21, 5, 41), "et": "2026-08-21 05:41",
            "dir": "LONG", "entry": weighted_entry, "sl": 29450.0,
            "qty": 8, "strat": "A/B",
        }]
        raw = CSV_HEADER + (
            "1,29469.5,29480.0,91,08/21/2026 11:41:03,08/21/2026 12:00:00,MNQU2026\n"
            "7,29466.75,29480.0,140,08/21/2026 11:41:18,08/21/2026 12:00:00,MNQU2026\n"
            "3,29320.0,29339.0,291,08/21/2026 16:10:00,08/21/2026 15:50:02,MNQU2026\n"
        )

        matched, unmatched, summary = guardrails._reconcile_match(raw, glog, {})

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["qty"], 8)
        self.assertEqual(matched[0]["fill_count"], 2)
        self.assertEqual(matched[0]["broker_t"], "2026-08-21 05:41:03 ET")
        self.assertEqual(matched[0]["broker_pnl"], 231)
        self.assertEqual(glog[0]["ext_outcome"], "win")
        self.assertEqual(glog[0]["ext_net"], 231)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["pnl"], 291)
        self.assertEqual(summary["matched_total"], 231)
        self.assertEqual(summary["unmatched_total"], 291)
        self.assertEqual(summary["broker_total"], 522)
        self.assertEqual(summary["matched_fill_rows"], 2)
        self.assertEqual(summary["unmatched_fill_rows"], 1)

    def test_same_price_on_wrong_trading_date_does_not_match(self):
        glog = [{
            "key": "old", "decision": "sent", "date": "2026-08-20",
            "ts": self.et_ms(2026, 8, 20, 5, 41), "et": "2026-08-20 05:41",
            "dir": "LONG", "entry": 29469.5, "sl": 29450.0, "qty": 1,
        }]
        raw = CSV_HEADER + "1,29469.5,29480.0,91,08/21/2026 11:41:03,08/21/2026 12:00:00,MNQU2026\n"

        matched, unmatched, summary = guardrails._reconcile_match(raw, glog, {})

        self.assertEqual(matched, [])
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(summary["broker_total"], 91)
        self.assertNotIn("reconciled", glog[0])

    def test_pine_can_filter_blocked_and_trading_date(self):
        rows = [
            {"decision": "sent", "date": "2026-08-20", "ts": self.et_ms(2026, 8, 20, 9, 30),
             "strat": "A/B", "sess": "NYAM", "dir": "LONG", "entry": 100, "sl": 90, "tp": 120, "qty": 2},
            {"decision": "blocked", "reason": "session:PREM", "date": "2026-08-21",
             "ts": self.et_ms(2026, 8, 21, 5, 41), "strat": "A/B-shallow", "sess": "PREM",
             "dir": "SHORT", "entry": 110, "sl": 120, "tp": 90},
        ]
        with open(guardrails.GLOG, "w") as handle:
            json.dump(rows, handle)

        pine = guardrails.pine_book("2026-08-21", "blocked")

        self.assertIn("BLOCKED: session:PREM A/B-shallow PREM SHORT", pine)
        self.assertIn("color.orange", pine)
        self.assertNotIn("FIRED A/B NYAM LONG", pine)
        self.assertEqual(guardrails.book_days("blocked"), ["2026-08-21"])
        self.assertEqual(guardrails.book_days("all"), ["2026-08-21", "2026-08-20"])
        self.assertIn('id="pineshow"', guardrails._HTML)
        self.assertIn('type="date" id="pineday"', guardrails._HTML)


if __name__ == "__main__":
    unittest.main()
