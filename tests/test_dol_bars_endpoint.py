import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent
import dol_reversal_live as live
from tests.test_dol_reversal_live import Response, signal


class DolBarsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = (live.DB, live.AUDIT)
        live.DB, live.AUDIT = root / "dol.sqlite3", root / "audit.csv"
        self.env = mock.patch.dict(os.environ, {
            "DOL_REVERSAL_MODE": "LIVE", "DOL_MANAGER_MODE": "LIVE",
            "DOL_MANAGER_EXECUTION": "TRADERSPOST_WEBHOOK", "DOL_KILL_SWITCH": "0",
            "ACCOUNT_LABEL": "100K", "EXEC_WEBHOOK": "https://example.invalid/hook",
            "EXEC_TICKER": "MNQZ6",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        live.DB, live.AUDIT = self.paths
        self.tmp.cleanup()

    @staticmethod
    def _wait_worker():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with agent._barq_lock:
                if not agent._barq_running and not agent._barq:
                    return
            time.sleep(.01)
        raise AssertionError("/bars worker did not finish")

    def test_chronological_replay_through_bars_sends_one_virtual_exit(self):
        s = signal()
        with mock.patch.object(live.strategy, "observe", return_value=True):
            live.classify(s, "unused.csv")
        payload = {"limitPrice": 100.0, "stopLoss": {"stopPrice": 95.0},
                   "takeProfit": {"limitPrice": 110.0}}
        self.assertTrue(live.claim_entry(s, 4, payload)[0])
        live.record_entry_response(s, Response())
        row = live._connect().execute("select * from trades").fetchone()
        live._post_action(row, "vp-endpoint", "VIRTUAL_PROTECTED_STOP", 1, .95, [1], 98.0)

        processed = []
        def after(bar, _now_ms):
            processed.append(bar["ts_event"])
            live.on_closed_bar(bar)

        patches = [
            mock.patch.object(agent, "_append_bar"),
            mock.patch.object(agent, "_feed_gap_min", return_value=None),
            mock.patch.object(agent, "_process_new", return_value={"nowe": 0}),
            mock.patch.object(agent, "_after_bar_processed", side_effect=after),
            mock.patch.object(agent.shadow, "refresh"),
            mock.patch.object(agent.guardrails, "sweep_orphans"),
            mock.patch.object(live, "_manager_decision", return_value=("HOLD", None, None, None)),
            mock.patch.object(live.requests, "post", return_value=Response()),
        ]
        started = [patcher.start() for patcher in patches]
        try:
            client = agent.app.test_client()
            for bar in (
                {"ts_event":"2027-01-15T08:01:00Z","open":100.5,"high":101,"low":99.5,"close":100},
                {"ts_event":"2027-01-15T08:02:00Z","open":100,"high":101,"low":97.5,"close":97.75},
                {"ts_event":"2027-01-15T08:03:00Z","open":97.75,"high":98,"low":97,"close":97.25},
            ):
                self.assertEqual(client.post("/bars", json=bar).status_code, 200)
                self._wait_worker()
            self.assertEqual(processed, ["2027-01-15T08:01:00Z", "2027-01-15T08:02:00Z",
                                         "2027-01-15T08:03:00Z"])
            self.assertEqual(started[-1].call_count, 1)
            self.assertEqual(sum(a["action"] == "FULL_CLOSE" for a in live.actions()), 1)
        finally:
            for patcher in reversed(patches):
                patcher.stop()


if __name__ == "__main__":
    unittest.main()
