import os
import unittest
from unittest.mock import patch

import continuation_scan_runtime as subject


class ContinuationThesisModeTests(unittest.TestCase):
    def test_default_is_strict_and_invalid_value_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(subject.thesis_mode(), "strict")
        with patch.dict(os.environ, {"CONTINUATION_HTF_THESIS_MODE": "anything"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "strict or allow_none"):
                subject.thesis_mode()

    def test_allow_none_is_direction_local_but_opposite_still_blocks(self):
        theses = {
            "neutral": {"thesis": "NONE", "reason": "no_unique"},
            "long": {"thesis": "LONG", "reason": "fixed"},
            "short": {"thesis": "SHORT", "reason": "fixed"},
        }
        long = subject.directional_theses(theses, "LONG", "allow_none")
        short = subject.directional_theses(theses, "SHORT", "allow_none")
        self.assertEqual(long["neutral"]["thesis"], "LONG")
        self.assertEqual(short["neutral"]["thesis"], "SHORT")
        self.assertEqual(long["short"]["thesis"], "SHORT")
        self.assertEqual(short["long"]["thesis"], "LONG")
        self.assertFalse(long["short"]["override_applied"])
        self.assertFalse(short["long"]["override_applied"])

    def test_annotation_keeps_original_none_visible(self):
        theses = {"day": {"thesis": "NONE", "reason": "no_unique"}}
        rows = [{"candidate_id": "C1", "trading_day": "day", "jade_thesis": "SHORT"}]
        orders = [{"candidate_id": "C1"}]
        subject.annotate_policy(rows, orders, theses, "SHORT", "allow_none")
        self.assertEqual(rows[0]["jade_thesis"], "NONE")
        self.assertEqual(rows[0]["jade_thesis_effective"], "SHORT")
        self.assertEqual(rows[0]["eligibility_override_reason"], "THESIS_NONE_ALLOWED")
        self.assertTrue(orders[0]["jade_thesis_override_applied"])


if __name__ == "__main__":
    unittest.main()
