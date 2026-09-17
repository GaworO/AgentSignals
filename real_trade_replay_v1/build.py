"""Build compact, forward-only real-trade replay catalog; no model fitting."""
from __future__ import annotations

import csv
import bisect
import datetime as dt
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

import ab_dol_live
import downside_manager_shadow_v1 as shadow
from rl_trade_manager.env import ROUND_TRIP_COST
from rl_trade_manager.types import Bar

ROOT=Path(__file__).resolve().parents[1]
HERE=Path(__file__).resolve().parent
SOURCE=ROOT/"audit_results/handover_verification/source"
TRADES=SOURCE/"trades.md"
PRICES=SOURCE/"prices.csv"
SIGNALS=ROOT/"audit_results/m15_context_20260912/causal_signals.jsonl"
OUT=HERE/"catalog.json"
ET=ZoneInfo("America/New_York")
UTC=dt.timezone.utc


def _sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_rows(path=TRADES):
    lines=[line for line in Path(path).read_text().splitlines() if line.startswith("|")]
    headers=[x.strip() for x in lines[0].strip("|").split("|")]
    rows=[]
    for line in lines[2:]:
        parts=[x.strip().replace("**","").replace("\\","") for x in line.strip("|").split("|")]
        if len(parts)!=len(headers):raise ValueError("Malformed real trade row")
        record=dict(zip(headers,parts))
        if not record["Decision"].startswith("SENT"):
            continue  # blocked records are not physical executions
        for field in ("Entry","SL","TP"):
            record[field]=float(record[field])
        record["Qty"]=int(record["Qty"])
        raw=record["Real$ (broker)"].replace("✓","").strip()
        record["actual_usd"]=float(raw) if raw else None
        rows.append(record)
    return rows


def _ms(time_et):
    return int(dt.datetime.strptime(time_et,"%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()*1000)


def _signals():
    return [json.loads(line) for line in SIGNALS.read_text().splitlines() if line]


def _match(record,signals):
    signal_ms=_ms(record["Time ET"])-60_000
    candidates=[]
    for item in signals:
        if item.get("dir")!=record["Dir"] or int(item.get("bos_ms",-1))!=signal_ms:
            continue
        if abs(float(item["entry"])-record["Entry"])>0.011 or abs(float(item["SL"])-record["SL"])>0.11:
            continue
        candidates.append(item)
    if not candidates:return None
    first=candidates[0]
    if any((x.get("fvg_lo"),x.get("fvg_hi"),x.get("ce"),x.get("emitted"))!=
           (first.get("fvg_lo"),first.get("fvg_hi"),first.get("ce"),first.get("emitted")) for x in candidates):
        raise ValueError("Conflicting causal signal metadata")
    return first


def _prices():
    bars={}
    with PRICES.open(newline="") as stream:
        for row in csv.DictReader(stream):
            bar=Bar(shadow._parse_ms(row["ts_event"]),*(float(row[k]) for k in ("open","high","low","close")))
            if bar.ms in bars and bars[bar.ms]!=bar:raise ValueError("Conflicting M1 duplicate")
            bars[bar.ms]=bar
    return bars


def _tick(value):return round(value*4)/4


def _source_path(record,market):
    anchor=_ms(record["Time ET"])
    direction=1 if record["Dir"]=="LONG" else -1
    entry=_tick(record["Entry"]);stop=_tick(record["SL"])
    if direction*(entry-stop)<5:return None,"RISK_BELOW_5_POINTS"
    fill_ms=None
    for minute in range(10):
        ms=anchor+minute*60_000
        bar=market.get(ms)
        if bar is None:return None,"MISSING_M1_IN_FILL_WINDOW"
        if (bar.low<=entry-.25 if direction==1 else bar.high>=entry+.25):
            fill_ms=ms;break
    if fill_ms is None:return None,"NO_THROUGH_FILL_WITHIN_10_MINUTES"
    keys=sorted(market)
    position=bisect.bisect_left(keys,fill_ms)
    pre=[market[ms] for ms in keys[max(0,position-120):position]]
    if len(pre)<120:return None,"MISSING_120_CAUSAL_PREBARS"
    risk=direction*(entry-stop)
    target=shadow._fixed_2r_price(entry,direction,risk)
    local=dt.datetime.fromtimestamp(fill_ms/1000,UTC).astimezone(ET)
    end=local.replace(hour=15,minute=55,second=0,microsecond=0)
    end_ms=int(end.timestamp()*1000)
    if end_ms<fill_ms:return None,"FILL_AFTER_SESSION_FLATTEN"
    bars=[]
    for ms in range(fill_ms,end_ms+60_000,60_000):
        bar=market.get(ms)
        if bar is None:return None,"MISSING_M1_AFTER_FILL"
        bars.append(bar)
        stop_hit=bar.low<=stop if direction==1 else bar.high>=stop
        target_hit=(ms>fill_ms) and (bar.high>=target if direction==1 else bar.low<=target)
        if stop_hit or target_hit or ms==end_ms:break
    return {"fill_ms":fill_ms,"entry":entry,"initial_sl":stop,"fixed_tp":target,
            "risk":risk,"pre":pre,"bars":bars},None


def _metrics(values):
    if not values:return {"trades":0,"wr":None,"pf":None,"net_r":None,"avg_r":None,"max_dd_r":None,"avg_winner_r":None,"avg_loser_r":None}
    wins=[x for x in values if x>0];losses=[x for x in values if x<0]
    gains=sum(wins);loss=-sum(losses)
    curve=np.r_[0,np.cumsum(values)]
    return {"trades":len(values),"wr":100*len(wins)/len(values),"pf":gains/loss if loss else None,
            "net_r":sum(values),"avg_r":sum(values)/len(values),
            "max_dd_r":float(np.max(np.maximum.accumulate(curve)-curve)),
            "avg_winner_r":sum(wins)/len(wins) if wins else None,
            "avg_loser_r":sum(losses)/len(losses) if losses else None}


def _summary(rows):
    valid=sorted((x for x in rows if x["status"]=="REPLAYED"),key=lambda x:(x["fill_ms"],x["id"]))
    base=[x["control_r"] for x in valid]
    manager=[x["manager_r"] for x in valid]
    losers=[x for x in valid if x["control_r"]<0]
    winners=[x for x in valid if x["control_r"]>0]
    return {"replayable":len(valid),"unreplayable":len(rows)-len(valid),
            "control":_metrics(base),"manager":_metrics(manager),
            "losers_improved":sum(x["manager_r"]>x["control_r"]+1e-10 for x in losers),
            "winners_damaged":sum(x["manager_r"]<x["control_r"]-1e-10 for x in winners),
            "winners_preserved":sum(x["manager_r"]>0 for x in winners),
            "winners_to_losses":sum(x["manager_r"]<0 for x in winners),
            "losers_to_winners":sum(x["manager_r"]>0 for x in losers),
            "mean_delta_r":sum(x["delta_r"] for x in valid)/len(valid) if valid else None,
            "total_delta_r":sum(x["delta_r"] for x in valid),
            "total_r_saved_on_losers":sum(max(0,x["delta_r"]) for x in losers),
            "total_winner_r_sacrificed":-sum(min(0,x["delta_r"]) for x in winners),
            "unreplayable_reasons":dict(Counter(x.get("reason") for x in rows if x["status"]!="REPLAYED")),
            "sample_label":"SMALL FORWARD/RECENT SAMPLE — DESCRIPTIVE ONLY" if len(valid)<20 else "RECENT FORWARD SAMPLE — DESCRIPTIVE ONLY"}


def build():
    rows=parse_rows();signals=_signals();market=_prices()
    engine=ab_dol_live._engine_for(str(PRICES))
    model=shadow._model()
    cached={}
    original=shadow._raw_state
    def raw(ms):
        if ms not in cached:cached[ms]=original(engine,ms)
        return dict(cached[ms])
    output=[]
    for index,source in enumerate(rows,1):
        ident=f"{source['Strat']}|{source['Time ET']}|{source['Dir']}|{source['Entry']}"
        result={"id":hashlib.sha256(ident.encode()).hexdigest()[:20],"source_key":ident,
                "time_et":source["Time ET"],"strategy":source["Strat"],"session":source["Sess"],
                "side":source["Dir"],"listed_entry":source["Entry"],"listed_sl":source["SL"],
                "listed_tp":source["TP"],"quantity":source["Qty"],
                "listed_outcome":source["Outcome"],"actual_broker_usd":source["actual_usd"],
                "status":"UNREPLAYABLE"}
        if source["Outcome"] in ("no_fill","canceled"):
            result["reason"]="SOURCE_OUTCOME_"+source["Outcome"].upper();output.append(result);continue
        signal=_match(source,signals)
        if signal is None:
            result["reason"]="MISSING_MATCHED_CAUSAL_SIGNAL";output.append(result);continue
        path,reason=_source_path(source,market)
        if reason:
            result["reason"]=reason;output.append(result);continue
        try:
            # The full Engine contains later bars, but every lookup uses the
            # frozen decision cutoff. States are never computed from outcome.
            states=[raw(path["fill_ms"])]
            with patch.object(shadow,"_raw_state",side_effect=lambda _,ms:raw(ms)):
                for bar in path["bars"]:
                    states.append(shadow._next_state(engine,states[-1],bar))
            safe={"fvg_lo":signal.get("fvg_lo"),"fvg_hi":signal.get("fvg_hi"),
                  "ce":signal.get("ce"),"emitted":signal.get("emitted")}
            row={"trade_id":result["id"],"strategy_id":source["Strat"],
                 "direction":1 if source["Dir"]=="LONG" else -1,
                 "entry":path["entry"],"initial_sl":path["initial_sl"],
                 "fixed_tp":path["fixed_tp"],"quantity":source["Qty"],
                 "initial_risk":path["risk"],"fill_ms":path["fill_ms"],
                 "signal_json":json.dumps(safe)}
            detail=shadow.detailed_replay(row,path["bars"],path["pre"],states,model)
            if detail["control"]["status"]!="DONE" or detail["manager"]["status"]!="DONE":
                raise AssertionError("Unresolved path despite session cutoff")
            result.update(status="REPLAYED",fill_ms=path["fill_ms"],entry=path["entry"],
                          initial_sl=path["initial_sl"],fixed_tp=path["fixed_tp"],
                          initial_risk=path["risk"],
                          control_r=detail["control"]["final_r"],
                          manager_r=detail["manager"]["final_r"],delta_r=detail["delta_r"],
                          control_usd=detail["control"]["final_r"]*path["risk"]*2*source["Qty"],
                          manager_usd=detail["manager"]["final_r"]*path["risk"]*2*source["Qty"],
                          last_decision=next((x["recommendation"] for x in detail["manager"]["decisions"]
                                              if not x["recommendation"].startswith("HOLD")),"HOLD"),
                          detail=detail)
        except Exception as exc:
            result["reason"]="CAUSAL_REPLAY_ERROR: "+str(exc)[:180]
        output.append(result)
        print(f"real replay {index}/{len(rows)} {source['Time ET']} {source['Strat']} {result['status']}",flush=True)
    report={"schema":"REAL_TRADE_REPLAY_V1","source_hashes":{
                "trades_md":_sha(TRADES),"prices_csv":_sha(PRICES),
                "causal_signals_jsonl":_sha(SIGNALS),"model_json":shadow._sha(shadow.MODEL)},
            "threshold":shadow.THRESHOLD,"scope":"recent sent real A/B legs only; no historical holdout",
            "source_execution":"through-tick modeled limit fill, first 10 bars; rounded MNQ tick entry/SL; conservative stop-before-target, 2.24 USD round trip per contract; not broker-fill reconciliation",
            "summary":_summary(output),"trades":output}
    HERE.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report,separators=(",",":")))
    return report


if __name__=="__main__":
    report=build()
    print(json.dumps(report["summary"],indent=2))
