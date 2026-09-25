import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import guardrails


class GuardClassificationBackfillTests(unittest.TestCase):
    def test_repairs_continuation_provenance_and_exact_ab_quality(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "guard_log.json"
            rows = [
                {"strat": "Continuation LONG", "dir": "LONG", "bar_ms": 1000,
                 "entry": 100.0, "classification": {"label": "Continuation LONG · LEGACY/UNCLASSIFIED"}},
                {"strat": "A/B", "dir": "SHORT", "bar_ms": 2000, "entry": 200.0},
            ]
            path.write_text(json.dumps(rows))
            setups = [{"dir": "SHORT", "bos_ms": 2000, "entry": 200.0,
                       "_ab_quality": {"tier": "Q2", "score": 2, "mode": "SHADOW_ONLY"}}]
            with patch.object(guardrails, "GLOG", str(path)), patch.object(guardrails, "DATA_DIR", td):
                changed = guardrails.backfill_trade_classifications(setups)
            self.assertGreater(changed, 0)
            saved = json.loads(path.read_text())
            self.assertEqual("struct", saved[0]["sl_src"])
            self.assertEqual("open_dol", saved[0]["tp_src"])
            self.assertEqual("CONT-L · FROZEN OPEN DOL", saved[0]["classification"]["label"])
            self.assertEqual("AB · Q2 SHADOW", saved[1]["classification"]["label"])


if __name__ == "__main__":
    unittest.main()
