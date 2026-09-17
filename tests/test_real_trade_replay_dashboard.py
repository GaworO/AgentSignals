"""Focused forward-replay and read-only dashboard checks."""
import datetime as dt
import csv
import json
import unittest

from flask import Flask

import downside_manager_dashboard as dashboard
import downside_manager_shadow_v1 as shadow
from real_trade_replay_v1.build import _metrics, parse_rows
from rl_trade_manager.downside_manager import prepare_split


class RealReplayDashboardTests(unittest.TestCase):
    def test_parse_and_catalog_metrics(self):
        rows=parse_rows()
        self.assertEqual(len(rows),28)
        self.assertEqual(sum(x["Strat"]=="A/B" for x in rows),14)
        self.assertEqual(sum(x["Strat"]=="A/B-shallow" for x in rows),14)
        self.assertEqual(_metrics([2,-1,-1])["pf"],1.0)
        catalog=json.loads(dashboard.CATALOG.read_text())
        valid=[x for x in catalog["trades"] if x["status"]=="REPLAYED"]
        self.assertEqual(catalog["summary"]["replayable"],len(valid))
        self.assertEqual(len({x["id"] for x in catalog["trades"]}),len(catalog["trades"]))
        self.assertAlmostEqual(sum(x["delta_r"] for x in valid),catalog["summary"]["total_delta_r"])
        for row in valid:
            self.assertAlmostEqual(row["detail"]["control"]["final_r"],row["control_r"])
            self.assertAlmostEqual(row["detail"]["manager"]["final_r"],row["manager_r"])
            self.assertEqual(row["detail"]["manager"]["decisions"][0]["recommendation"],"HOLD_ENTRY")
            self.assertTrue(all(x["closed_candle_ms"] is None or x["closed_candle_ms"]<x["decision_ms"]
                                for x in row["detail"]["manager"]["decisions"]))
        # Independent verified nine-trade source checks the fill/stop/target
        # accounting without using the broker result as a training signal.
        lookup={(x["time_et"],x["strategy"]):x for x in valid}
        source=shadow.HERE/"audit_results/handover_verification/verified_nine_trades.csv"
        with source.open() as stream:
            for reference in csv.DictReader(stream):
                trade=lookup[(reference["time_et"],"A/B-shallow")]
                self.assertAlmostEqual(trade["control_usd"],float(reference["old_net"]),places=6)

    def test_causal_trace_prefix_and_frozen_outcome(self):
        prepared,_=prepare_split("train")
        trade,base,_,_=next(x for x in prepared if len(x[0].bars)>5)
        emitted=(dt.datetime.fromtimestamp(trade.fvg_signal_ms/1000,dt.timezone.utc).isoformat()
                 if trade.fvg_signal_ms else None)
        row={"trade_id":trade.key,"strategy_id":trade.strategy_id,"direction":trade.direction,
             "entry":trade.entry,"initial_sl":trade.initial_sl,"fixed_tp":trade.target,
             "quantity":trade.qty,"initial_risk":trade.risk,"fill_ms":trade.fill_ms,
             "signal_json":json.dumps({"fvg_lo":trade.fvg_low,"fvg_hi":trade.fvg_high,
                                       "ce":trade.fvg_ce,"emitted":emitted})}
        model=shadow._model()
        full=shadow.detailed_replay(row,list(trade.bars),list(trade.pre_bars),list(trade.dol_states),model)
        prefix=shadow.detailed_replay(row,list(trade.bars[:4]),list(trade.pre_bars),list(trade.dol_states[:5]),model)
        a=full["manager"]["decisions"][:len(prefix["manager"]["decisions"])]
        b=prefix["manager"]["decisions"]
        self.assertEqual(a,b)
        self.assertAlmostEqual(full["control"]["final_r"],base,places=9)
        self.assertEqual(full["manager"]["decisions"][0]["action"],0)

    def test_list_detail_and_terminal_render(self):
        app=Flask(__name__)
        shadow.register(app)
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith("/downside-shadow"):
                self.assertTrue(rule.methods.issubset({"GET","HEAD","OPTIONS"}))
        client=app.test_client()
        listing=client.get("/downside-shadow/api/trades")
        self.assertEqual(listing.status_code,200)
        rows=listing.get_json()["trades"]
        self.assertEqual(len(rows),28)
        self.assertNotIn("detail",rows[0])
        item=next(x for x in rows if x["status"]=="REPLAYED")
        one=client.get("/downside-shadow/api/trade/"+item["id"])
        self.assertEqual(one.status_code,200)
        self.assertIn("candles",one.get_json()["detail"])
        self.assertEqual(client.get("/downside-shadow/api/trade/not-a-trade").status_code,404)
        for path in ("/downside-shadow","/downside-shadow/trades","/downside-shadow/real-replays",
                     "/downside-shadow/metrics","/downside-shadow/trade/"+item["id"]):
            page=client.get(path)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"SHADOW ONLY",page.data)
            self.assertIn(b"M1 Candles",page.data)
        self.assertFalse(client.get("/downside-shadow/api/live").get_json()["status"]["broker_execution"])


if __name__=="__main__":unittest.main()
