import unittest

import ab_candidates_view
import dashboard


class ABCandidatesViewTests(unittest.TestCase):
    def env(self, enabled="1"):
        return {
            "AB_SHALLOW_ENABLED": enabled,
            "AB_SHALLOW_FRACTION": "0.25",
            "AB_SHALLOW_RR": "2",
            "SETUP_GROUP_RISK_USD": "900",
            "SETUP_GROUP_RT_COST_USD": "2.24",
            "ACCOUNT": "100000",
            "POINT_VALUE": "2",
            "EXEC_TICK": "0.25",
            "AB_SHALLOW_MIN_SL_PTS": "5",
            "AB_SHALLOW_MAX_SL_PTS": "0",
        }

    def confirmed_trace(self):
        base = {
            "trig_ms": 1000,
            "trig": "2026-08-27 09:30",
            "model": "Reversal",
            "cat": "PDH",
            "dir": "SHORT",
            "fvg": [100.0, 105.0],
        }
        return [
            dict(base, stage="displacement OK"),
            dict(base, stage="setup OK (BOS)"),
            dict(base, stage="POTWIERDZONY", date="2026-08-27", bos="09:42",
                 bos_ms=2000, entry=100.0, SL=120.0, TP=60.0, risk=20.0,
                 signal_close=80.0),
        ]

    def test_confirmed_ab_builds_ready_shallow_leg(self):
        rows = ab_candidates_view.build_candidates(self.confirmed_trace(), self.env())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ab_state"], "confirmed")
        self.assertEqual(rows[0]["shallow"]["state"], "ready")
        self.assertEqual(rows[0]["shallow"]["entry"], 85.0)
        self.assertEqual(rows[0]["shallow"]["SL"], 120.0)
        self.assertEqual(rows[0]["shallow"]["TP"], 15.0)
        self.assertEqual(rows[0]["shallow"]["budget"], 450.0)

    def test_failed_ab_blocks_shallow(self):
        trace = [{
            "trig_ms": 1000, "trig": "2026-08-27 09:30", "model": "Cont",
            "cat": "VI", "dir": "LONG", "stage": "brak setupu (odbicie/BOS)",
        }]
        row = ab_candidates_view.build_candidates(trace, self.env())[0]
        self.assertEqual(row["ab_state"], "failed")
        self.assertEqual(row["shallow"]["state"], "blocked")

    def test_disabled_shallow_is_explicit(self):
        row = ab_candidates_view.build_candidates(self.confirmed_trace(), self.env("0"))[0]
        self.assertEqual(row["shallow"]["state"], "disabled")
        self.assertIn("AB_SHALLOW_ENABLED", row["shallow"]["reason"])

    def test_page_contains_two_pipelines_and_legend(self):
        page = ab_candidates_view.render_page(
            self.confirmed_trace(), 12, self.env(),
            live_status={"updated_at": "2026-08-27T12:00:00Z", "age_sec": 4,
                         "last_bar": "2026-08-27T11:59:00Z", "refresh_seconds": 15,
                         "version": "v31.13-live-ab-candidates-no-orb-amd"},
        )
        self.assertIn("A/B Strategy", page)
        self.assertIn("A/B Shallow", page)
        self.assertIn("What each step means", page)
        self.assertIn("Shallow is not an independent signal", page)
        self.assertIn('http-equiv="refresh" content="15"', page)
        self.assertIn('liveflag ok">LIVE', page)
        self.assertIn("v31.13-live-ab-candidates-no-orb-amd", page)

    def test_dashboard_uses_combined_page_and_hides_orb_amd(self):
        page = dashboard.render_home()
        nav = page.split("var NAV=", 1)[1].split("var frame=", 1)[0]
        self.assertIn("A/B + Shallow", nav)
        self.assertIn("/ab/candidates", page)
        self.assertNotIn("['orb','ORB'", nav)
        self.assertNotIn("['amd','AMD'", nav)


if __name__ == "__main__":
    unittest.main()
