import os
import tempfile
import unittest

import pandas as pd

from detcore.config import Config
from detcore.data import load


class DetcoreDataTests(unittest.TestCase):
    def test_load_accepts_seed_and_live_iso8601_timestamp_styles(self):
        rows = pd.DataFrame(
            {
                "ts_event": [
                    "2026-08-10 19:25:00+00:00",
                    "2026-08-10T19:26:00+00:00",
                    "2026-08-10T19:27:00Z",
                ],
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "volume": [10, 11, 12],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mixed_timestamps.csv")
            rows.to_csv(path, index=False)
            ctx = load(Config(data_csv=path, cutoff=""))

        self.assertEqual(ctx.n, 3)
        self.assertEqual(str(ctx.ts.dt.tz), "UTC")
        self.assertEqual(
            ctx.ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
            [
                "2026-08-10T19:25:00Z",
                "2026-08-10T19:26:00Z",
                "2026-08-10T19:27:00Z",
            ],
        )


if __name__ == "__main__":
    unittest.main()
