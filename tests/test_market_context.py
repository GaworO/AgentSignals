import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import market_context


NY = ZoneInfo("America/New_York")


def synthetic_bars(start="2026-03-02", periods=130):
    rows = []
    dates = pd.bdate_range(start, periods=periods)
    price = 20_000.0
    for i, date in enumerate(dates):
        # The future half deliberately reverses so a full-data calculation would
        # disagree with a causal prefix calculation if look-ahead leaked in.
        drift = 17.0 if i < periods // 2 else -24.0
        price += drift + (i % 5 - 2) * 2.0
        stamp = pd.Timestamp(date).tz_localize(NY) + pd.Timedelta(hours=16, minutes=59)
        rows.append({"ts_event": stamp.tz_convert("UTC").isoformat(),
                     "open": price - drift / 2, "high": price + 25,
                     "low": price - 25, "close": price, "volume": 1000 + i})
    return pd.DataFrame(rows)


class MarketContextTests(unittest.TestCase):
    def test_report_and_history_are_causal(self):
        bars = synthetic_bars()
        cutoff = 82
        with tempfile.TemporaryDirectory() as tmp:
            full_path = Path(tmp) / "full.csv"
            prefix_path = Path(tmp) / "prefix.csv"
            bars.to_csv(full_path, index=False)
            bars.iloc[:cutoff].to_csv(prefix_path, index=False)

            full = market_context.build_report([full_path], daily_limit=180, weekly_limit=104)
            prefix = market_context.build_report([prefix_path], daily_limit=180, weekly_limit=104)

            self.assertTrue(full["ok"])
            self.assertTrue(prefix["ok"])
            date = prefix["daily"]["as_of"]
            historical = next(row for row in full["history"]["daily"] if row["date"] == date)
            self.assertEqual(historical["bias"], prefix["daily"]["bias"])
            self.assertEqual(historical["score"], prefix["daily"]["score"])
            self.assertEqual(historical["regime"], prefix["daily"]["regime"]["code"])
            self.assertTrue(full["informational_only"])

    def test_scheduler_writes_once_per_due_period(self):
        now = dt.datetime(2026, 8, 31, 9, 0, tzinfo=NY)  # Monday: daily + Sunday plan due.
        bars = synthetic_bars(start="2026-03-02", periods=130)
        bars.loc[bars.index[-1], "ts_event"] = (now - dt.timedelta(minutes=1)).astimezone(
            dt.timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            bars.to_csv(path, index=False)
            first = market_context.record_if_due([path], tmp, now=now)
            second = market_context.record_if_due([path], tmp, now=now)

            self.assertEqual({row["kind"] for row in first}, {"daily", "weekly"})
            self.assertEqual(second, [])
            saved = market_context.read_recorded_history(Path(tmp) / market_context.HISTORY_FILE)
            self.assertEqual(len(saved), 2)
            self.assertTrue(all(row["informational_only"] for row in saved))

    def test_compact_database_can_serve_report_without_raw_history(self):
        bars = synthetic_bars(periods=140)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "history.csv"
            database = Path(tmp) / "market_history.db"
            bars.to_csv(source, index=False)
            meta = market_context.build_history_database(source, database)
            daily, loaded_meta = market_context.load_history_database(database)
            report = market_context.build_report([], database_path=database,
                                                  news={"ok": True, "events": []})

            self.assertEqual(int(meta["daily_bars"]), 140)
            self.assertEqual(len(daily), 140)
            self.assertEqual(loaded_meta["schema_version"], "1")
            self.assertTrue(report["ok"])
            self.assertTrue(report["data"]["history_database"])
            self.assertEqual(report["data"]["days"], 140)

    def test_globex_trade_date_is_stable_across_spring_dst(self):
        stamps = [
            pd.Timestamp("2026-03-06 16:59", tz=NY),
            pd.Timestamp("2026-03-08 18:30", tz=NY),
            pd.Timestamp("2026-03-09 16:59", tz=NY),
        ]
        rows = [{"ts_event": stamp.tz_convert("UTC").isoformat(), "open": 1,
                 "high": 2, "low": 0, "close": 1, "volume": 1}
                for stamp in stamps]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "dst.csv"
            pd.DataFrame(rows).to_csv(source, index=False)
            daily, _ = market_context._aggregate(market_context.load_bars([source]))

            self.assertEqual(list(daily.index.strftime("%Y-%m-%d")),
                             ["2026-03-06", "2026-03-09"])


if __name__ == "__main__":
    unittest.main()
