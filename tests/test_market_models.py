import unittest

import numpy as np
import pandas as pd

import market_models


def synthetic_daily(periods=760):
    rng = np.random.default_rng(20260903)
    index = pd.bdate_range("2023-01-02", periods=periods)
    regime = np.repeat([0.0008, -0.0007, 0.0, 0.0004], periods // 4 + 1)[:periods]
    volatility = np.repeat([0.006, 0.014, 0.008, 0.011], periods // 4 + 1)[:periods]
    returns = regime + rng.normal(0, volatility)
    close = 12_000 * np.exp(np.cumsum(returns))
    previous = np.r_[close[0], close[:-1]]
    open_ = previous * (1 + rng.normal(0, 0.0015, periods))
    spread = np.maximum(close, open_) * (0.003 + np.abs(rng.normal(0, 0.002, periods)))
    return pd.DataFrame({
        "open": open_,
        "high": np.maximum(open_, close) + spread,
        "low": np.minimum(open_, close) - spread,
        "close": close,
        "volume": rng.integers(50_000, 200_000, periods),
    }, index=index)


class MarketModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = synthetic_daily()
        cls.result = market_models.classify_market(cls.daily, "BULLISH")

    def test_hmm_uses_filtered_current_probability(self):
        hmm = self.result["hmm"]
        self.assertTrue(hmm["ok"])
        self.assertTrue(hmm["shadow_only"])
        self.assertTrue(hmm["filtered_not_smoothed"])
        self.assertEqual(len(hmm["probabilities"]), 4)
        self.assertAlmostEqual(sum(row["probability"] for row in hmm["probabilities"]),
                               100.0, delta=0.2)

    def test_ai_is_walk_forward_shadow_output(self):
        ai = self.result["ai"]
        validation = ai["validation"]
        self.assertTrue(ai["ok"])
        self.assertTrue(ai["shadow_only"])
        self.assertTrue(validation["ok"])
        self.assertGreaterEqual(validation["samples"], 250)
        self.assertIn(ai["prediction"], market_models.AI_CLASSES)
        self.assertAlmostEqual(sum(ai["probabilities"].values()), 100.0, delta=0.2)

    def test_features_do_not_change_when_future_rows_are_added(self):
        cutoff = 500
        full = market_models.build_features(self.daily)
        prefix = market_models.build_features(self.daily.iloc[:cutoff])
        columns = [column for column in prefix.columns if column not in ("target", "forward_atr")]
        pd.testing.assert_series_equal(full.iloc[cutoff - 1][columns],
                                       prefix.iloc[-1][columns], check_names=False)


if __name__ == "__main__":
    unittest.main()
