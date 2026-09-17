"""Read-only trading-terminal UI and replay APIs for the frozen shadow."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from flask import abort, jsonify, Response, render_template_string

import downside_manager_shadow_v1 as shadow
from rl_trade_manager.types import Bar

CATALOG=Path(__file__).resolve().parent/"real_trade_replay_v1/catalog.json"


@lru_cache(maxsize=1)
def _catalog():
    if not CATALOG.exists():
        return {"summary":{},"trades":[],"error":"Replay catalog has not been built"}
    return json.loads(CATALOG.read_text())


def _public(row):
    return {k:v for k,v in row.items() if k!="detail"}


def _live_rows():
    if not shadow.ENABLED:return []
    with shadow._connect() as c:
        return [dict(r) for r in c.execute("""SELECT id,trade_id,source_key,strategy_id,status,
             direction,entry,initial_sl,fixed_tp,quantity,initial_risk,signal_ms,fill_ms,
             current_r,mfe_r,mae_r,manager_probability,recommendation,control_final_r,
             manager_final_r,delta_r,control_exit_reason,manager_exit_reason,state_quality
             FROM downside_shadow_trades ORDER BY id DESC LIMIT 300""")]


def _live_detail(identifier):
    if not shadow.ENABLED:abort(404)
    with shadow._connect() as c:
        stored=c.execute("SELECT * FROM downside_shadow_trades WHERE id=?",(identifier,)).fetchone()
    if stored is None:abort(404)
    row=dict(stored)
    signal=json.loads(row["signal_json"])
    bars=[Bar(*x) for x in json.loads(row["bars_json"])]
    pre=[Bar(*x) for x in json.loads(row["pre_bars_json"])]
    states=json.loads(row["dol_states_json"])
    detail=(shadow.detailed_replay(row,bars,pre,states,quality=row["state_quality"])
            if bars and row["fill_ms"] is not None else None)
    return {"id":row["id"],"trade_id":row["trade_id"],"source_key":row["source_key"],
            "strategy":row["strategy_id"],"session":signal.get("session"),
            "status":row["status"],"side":"LONG" if row["direction"]==1 else "SHORT",
            "entry":row["entry"],"initial_sl":row["initial_sl"],"fixed_tp":row["fixed_tp"],
            "quantity":row["quantity"],"fill_ms":row["fill_ms"],"state_quality":row["state_quality"],
            "actual_broker_usd":None,"control_r":row["control_final_r"],
            "manager_r":row["manager_final_r"],"delta_r":row["delta_r"],"detail":detail}


def register(app):
    app.add_url_rule("/downside-shadow/api/trades","dm_replay_list",
                     lambda:jsonify({"summary":_catalog().get("summary",{}),
                                     "trades":[_public(x) for x in _catalog().get("trades",[])]}))
    def trade(identifier):
        value=next((x for x in _catalog().get("trades",[]) if x["id"]==identifier),None)
        if value is None:abort(404)
        return jsonify(value)
    app.add_url_rule("/downside-shadow/api/trade/<identifier>","dm_replay_one",trade)
    app.add_url_rule("/downside-shadow/api/live","dm_live_list",
                     lambda:jsonify({"status":shadow.status(),"trades":_live_rows()}))
    app.add_url_rule("/downside-shadow/api/live/<int:identifier>","dm_live_one",
                     lambda identifier:jsonify(_live_detail(identifier)))
    for path,endpoint in (("/downside-shadow/trades","dm_history"),
                          ("/downside-shadow/real-replays","dm_real_replays"),
                          ("/downside-shadow/metrics","dm_metrics"),
                          ("/downside-shadow/trade/<identifier>","dm_trade_page")):
        app.add_url_rule(path,endpoint,lambda **_:Response(page(),mimetype="text/html"))


def page():
    return render_template_string((Path(__file__).resolve().parent / "templates" /
                                   "downside_manager_shadow.html").read_text())
