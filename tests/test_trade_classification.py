import unittest

import trade_classification as subject


class TradeClassificationTests(unittest.TestCase):
    def test_ab_quality_is_visible_but_shadow_only(self):
        row = subject.candidate({"_strat": "A/B", "dir": "LONG", "_ab_quality": {
            "tier": "Q2", "score": 2, "mode": "SHADOW_ONLY"}})
        self.assertEqual("AB · Q2 SHADOW", row["label"])
        self.assertEqual("SHADOW_ONLY", row["quality_mode"])

    def test_dol_keeps_underlying_ab_quality(self):
        row = subject.candidate({"_strat": "DOL_DELIVERY_REVERSAL", "dir": "SHORT", "_ab_quality": {
            "tier": "Q3", "score": 4, "mode": "SHADOW_ONLY"}})
        self.assertEqual("DOL-REVERSAL", row["family"])
        self.assertEqual("DOL REVERSAL · MANAGER LIVE · Q3", row["label"])

    def test_continuation_is_not_given_an_ab_quality_grade(self):
        row = subject.continuation_order({"direction": "SHORT", "dol_id": "D1"})
        self.assertEqual("CONT-S · FROZEN OPEN DOL", row["label"])
        self.assertEqual("N/A_AB_ONLY", row["quality_mode"])

    def test_legacy_continuation_guard_row_is_inferred(self):
        row = subject.guard_row({"strat": "Continuation LONG", "dir": "LONG"})
        self.assertEqual("CONT-L · FROZEN OPEN DOL", row["label"])

    def test_old_ab_without_features_is_not_given_a_fake_grade(self):
        row = subject.guard_row({"strat": "A/B", "dir": "LONG"})
        self.assertEqual("AB · QUALITY NOT RECORDED", row["label"])


if __name__ == "__main__":
    unittest.main()
