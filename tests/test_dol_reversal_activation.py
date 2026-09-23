import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dol_delivery_reversal_shadow as strategy
import dol_reversal_control as control


def signal():
    return {
        "date": "2026-09-23", "model": "reversal", "cat": "PDL",
        "dir": "LONG", "bos": "10:22", "bos_ms": 1_795_000_000_000,
        "entry_ms": 1_795_000_060_000, "entry": 100.0, "SL": 95.0,
        "TP": 113.0, "_strat": "A/B",
    }


def dol():
    return {
        "metadata_status": "ATTACHED", "evaluated_at_ms": 1_795_000_060_000,
        "narrative_class": "COMPLETE_DOL_NARRATIVE", "dol_status": "OPEN",
        "direction_aligned_with_dol": True, "selected_dol": "PDH:1",
        "dol_price": 110.0, "dol_direction": "LONG", "source_families": ["native"],
    }


class DolActivationTests(unittest.TestCase):
    def test_frozen_hashes_and_threshold(self):
        frozen = control.frozen_artifacts()
        self.assertTrue(frozen["model_hash_ok"])
        self.assertTrue(frozen["threshold_hash_ok"])
        self.assertTrue(frozen["threshold_value_ok"])

    def test_fixed_2r_and_shared_identity(self):
        row = strategy._record(signal(), dol(), "canonical-1")
        self.assertEqual(row["theoretical_entry"], 100.0)
        self.assertEqual(row["SL"], 95.0)
        self.assertEqual(row["TP"], 110.0)
        self.assertEqual(len({x["signal_id"] for x in row["account_decisions"]}), 1)
        self.assertEqual(len({x["client_order_id"] for x in row["account_decisions"]}), 2)
        self.assertTrue(all(not x["submitted"] for x in row["account_decisions"]))

    def test_unavailable_state_is_explicit(self):
        gate = strategy.eligibility(signal(), {"metadata_status": "UNAVAILABLE"})
        self.assertFalse(gate["accepted"])
        self.assertEqual(gate["reason"], "DOL_STATE_UNAVAILABLE")

    def test_live_request_is_fail_closed(self):
        with patch.dict(os.environ, {"DOL_REVERSAL_MODE": "LIVE", "DOL_MANAGER_MODE": "LIVE"}, clear=False):
            ready = control.readiness()
        self.assertFalse(ready["live_activation_allowed"])
        self.assertEqual(ready["effective_modes"], {"reversal": "SHADOW", "manager": "SHADOW"})
        self.assertTrue(any(x.startswith("TRADERSPOST_LIVE_CAPABILITY:") for x in ready["activation_blockers"]))

    def test_kill_switch_disables_both_components(self):
        with patch.dict(os.environ, {"DOL_KILL_SWITCH": "1"}, clear=False):
            ready = control.readiness()
            self.assertEqual(ready["effective_modes"], {"reversal": "OFF", "manager": "OFF"})

    def test_observer_is_idempotent_and_does_not_mutate_ab(self):
        original_log = strategy.LOG
        original = signal()
        candidate = copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as tmp, patch.object(strategy, "LOG", str(Path(tmp) / "rows.json")):
            with patch("dol_reversal_manager_shadow_v1.observe_candidate", return_value=True):
                self.assertTrue(strategy.observe(candidate, dol(), candidate_id="same"))
                self.assertFalse(strategy.observe(candidate, dol(), candidate_id="same"))
            rows = json.loads(Path(strategy.LOG).read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(candidate, original)
        self.assertEqual(original_log, strategy.LOG)


if __name__ == "__main__":
    unittest.main()
