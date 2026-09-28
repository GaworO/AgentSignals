import copy
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
import tv_seconds_feed as feed

START = 1790607600000  # aligned UTC minute, fixed test clock
CURRENT = START + 61000


def packet(count=60):
    rows = [dict(ts_ms=START+i*1000, open=100., high=101., low=99., close=100., volume=1.)
            for i in range(count)]
    return dict(schema=feed.SCHEMA, source="TradingView", tf="1S", symbol="TEST:MNQZ26",
                feed_token="test-only-" + "a"*40,
                minute=dict(ts_ms=START, open=100., high=101., low=99., close=100., volume=60.), bars=rows)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, TV_1S_ENABLED="1", TV_1S_TOKEN=packet()["feed_token"],
            TV_1S_SYMBOL="TEST:MNQZ26", TV_1S_DB=str(Path(self.tmp.name)/"seconds.sqlite3"),
            TV_1S_MAX_DELAY_SEC="180")
        self.env.start()
        self.clock = patch.object(feed, "now_ms", return_value=CURRENT)
        self.clock.start()
        self.app = Flask(__name__)
        self.legacy_calls = []
        self.app.add_url_rule("/bars", "legacy", lambda: self.legacy_calls.append(True) or "ok", methods=["POST"])
        feed.register(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_complete_roundtrip(self):
        result = feed.ingest(packet())
        self.assertEqual((result["accepted"],result["quality"]), (60,"COMPLETE_MATCH"))
        self.assertFalse(result["manager_connected"])
        with feed.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM seconds").fetchone()[0],60)
        self.assertEqual(feed.status()["blockers"], [])

    def test_replay_is_idempotent(self):
        feed.ingest(packet())
        result = feed.ingest(packet())
        self.assertEqual((result["accepted"],result["duplicates"]), (0,60))
        self.assertEqual(feed.status()["total_stored_seconds"], 60)

    def test_partial_and_later_completion_without_interpolation(self):
        self.assertEqual(feed.ingest(packet(40))["quality"], "INCOMPLETE")
        self.assertEqual(feed.status()["total_stored_seconds"],40)
        result = feed.ingest(packet())
        self.assertEqual((result["accepted"],result["quality"]), (20,"COMPLETE_MATCH"))

    def test_empty_batch_records_missing(self):
        result = feed.ingest(packet(0))
        self.assertEqual((result["quality"],result["missing_seconds"]),("EMPTY",60))

    def test_mismatch_is_visible_not_repaired(self):
        body = packet()
        body["minute"]["volume"] = 61
        self.assertEqual(feed.ingest(body)["quality"],"M1_MISMATCH")
        self.assertIn("M1_MISMATCH",feed.status()["blockers"])

    def test_conflicting_second_is_atomic(self):
        feed.ingest(packet(1))
        body = packet()
        body["bars"][-1]["volume"] = 2
        body["bars"][0]["volume"] = 2
        with self.assertRaises(ValueError): feed.ingest(body)
        self.assertEqual(feed.status()["total_stored_seconds"],1)
        with feed.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM seconds").fetchone()[0],1)

    def test_conflict_after_new_inserts_rolls_back(self):
        body = packet()
        body["bars"] = body["bars"][-1:]
        feed.ingest(body)
        body = packet()
        body["bars"][-1]["volume"] = 2
        with self.assertRaises(ValueError): feed.ingest(body)
        self.assertEqual(feed.status()["total_stored_seconds"],1)

    def test_changed_m1_reference_rejected(self):
        feed.ingest(packet())
        body = packet()
        body["minute"]["high"] = 102
        with self.assertRaises(ValueError): feed.ingest(body)

    def test_invalid_inputs(self):
        changes = [lambda b:b.update(symbol="OTHER"), lambda b:b.update(tf="M1"),
                   lambda b:b.update(schema="other"), lambda b:b.update(source="Other"),
                   lambda b:b["bars"][0].update(high=98), lambda b:b["bars"][0].update(volume=-1),
                   lambda b:b["bars"][0].update(close=float("nan")),
                   lambda b:b["bars"][0].update(close=True), lambda b:b["bars"][0].update(ts_ms=START+1),
                   lambda b:b["bars"][0].update(ts_ms=START-1000),
                   lambda b:b["bars"].append(copy.deepcopy(b["bars"][0])),
                   lambda b:b["bars"].reverse(),lambda b:b.update(bars={}),lambda b:b.update(minute=None)]
        for change in changes:
            with self.subTest(change=change):
                b = packet(); change(b)
                with self.assertRaises(ValueError): feed.ingest(b)

    def test_unclosed_and_stale_minute_rejected(self):
        with self.assertRaises(ValueError): feed.ingest(packet(),START+59999)
        with self.assertRaises(ValueError): feed.ingest(packet(),START+240001)

    def test_status_uses_market_time_not_retry_time(self):
        feed.ingest(packet(),START+60001)
        feed.ingest(packet(),START+160000)
        self.assertEqual(feed.status(START+160000)["state"],"STALE")

    def test_disabled_and_missing_configuration(self):
        with patch.dict(os.environ,TV_1S_ENABLED="0"):
            self.assertEqual(self.client.post("/bars/1s",json=packet()).status_code,503)
        with patch.dict(os.environ,TV_1S_TOKEN=""):
            self.assertEqual(self.client.post("/bars/1s",json=packet()).status_code,503)

    def test_auth_body_and_header(self):
        body = packet(); body.pop("feed_token")
        self.assertEqual(self.client.post("/bars/1s",json=body).status_code,401)
        self.assertEqual(self.client.post("/bars/1s",json=body,headers={"X-TV-1S-Token":packet()["feed_token"]}).status_code,200)
        self.assertEqual(self.client.post("/bars/1s",json=packet()).status_code,200)

    def test_valid_json_object_only_and_size_limit(self):
        self.assertEqual(self.client.post("/bars/1s",json=[]).status_code,400)
        self.assertEqual(self.client.post("/bars/1s",data="bad",content_type="application/json").status_code,400)
        self.assertEqual(self.client.post("/bars/1s",data="x"*40000).status_code,413)

    def test_invalid_http_batch_returns_400_without_secrets(self):
        body = packet();body["bars"][0]["volume"] = None
        r = self.client.post("/bars/1s",json=body)
        self.assertEqual(r.status_code,400)
        self.assertNotIn(packet()["feed_token"],r.get_data(as_text=True))

    def test_seconds_do_not_enter_legacy_m1(self):
        self.assertEqual(self.client.post("/bars/1s",json=packet()).status_code,200)
        self.assertEqual(self.legacy_calls,[])
        self.assertEqual(self.client.post("/bars",json={"close":100}).status_code,200)
        self.assertEqual(self.legacy_calls,[True])

    def test_status_and_page_do_not_expose_token(self):
        feed.ingest(packet())
        for path in ("/feed/1s","/feed/1s/data"):
            r = self.client.get(path)
            self.assertEqual(r.status_code,200)
            self.assertNotIn(packet()["feed_token"],r.get_data(as_text=True))
        self.assertFalse(self.client.get("/feed/1s/data").json["manager_connected"])

    def test_export_auth_and_ordered_utc_csv(self):
        feed.ingest(packet())
        body = dict(start_ms=START,end_ms=START+60000)
        self.assertEqual(self.client.post("/feed/1s/export",json=body).status_code,401)
        r = self.client.post("/feed/1s/export",json=body,headers={"X-TV-1S-Token":packet()["feed_token"]})
        rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(len(rows),60)
        self.assertTrue(rows[0]["ts_event"].endswith("+00:00"))
        self.assertEqual(rows[0]["symbol"],"TEST:MNQZ26")

    def test_export_range_limit(self):
        with self.assertRaises(ValueError):feed.export_csv(dict(start_ms=START,end_ms=START+86401000))

    def test_concurrent_duplicate_batch(self):
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _:feed.ingest(packet()),range(4)))
        self.assertEqual(sum(r["accepted"] for r in results),60)
        self.assertEqual(feed.status()["total_stored_seconds"],60)

    def test_pine_envelope_matches_schema_and_batch_design(self):
        src = (Path(__file__).resolve().parents[1]/"tradingview/tv_1s_minute_batches.pine").read_text()
        for marker in (feed.SCHEMA,'"1S"',"request.security_lower_tf", "barstate.isconfirmed",
                       "barstate.isrealtime", "alert.freq_once_per_bar_close"):
            self.assertIn(marker,src)
        self.assertNotIn("strategy.entry",src)


if __name__ == "__main__": unittest.main()
