import datetime as dt
import unittest

import intake_guard


class AgentIntakeTests(unittest.TestCase):
    def bar(self):
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        return {"ts_event": now.isoformat(), "open": 100, "high": 102,
                "low": 99, "close": 101, "volume": 10}

    def test_token_auth_accepts_header_or_bearer(self):
        self.assertFalse(intake_guard.token_ok("secret", {}, ""))
        self.assertTrue(intake_guard.token_ok("secret", {"X-Bars-Token": "secret"}))
        self.assertTrue(intake_guard.token_ok("secret", {"Authorization": "Bearer secret"}))

    def test_validation_and_idempotent_sequence(self):
        bar, ms = intake_guard.normalize_bar(self.bar())
        self.assertEqual(intake_guard.sequence_decision(None, bar), "new")
        self.assertEqual(intake_guard.sequence_decision(bar, dict(bar)), "duplicate")
        self.assertEqual(intake_guard.sequence_decision(bar, dict(bar, close=100.5)), "conflict")
        older = dict(bar, ts_event=(dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
                                    - dt.timedelta(minutes=1)).isoformat())
        self.assertEqual(intake_guard.sequence_decision(bar, older), "out_of_order")

    def test_rejects_bad_ohlc_and_stale_bars(self):
        bad = self.bar(); bad["low"] = 103
        with self.assertRaisesRegex(ValueError, "high below low|OHLC geometry"):
            intake_guard.normalize_bar(bad)
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        self.assertIn("too old", intake_guard.freshness_error(now - 181000, now, 30, 180))


if __name__ == "__main__":
    unittest.main()
