"""Forward-only, broker-inert fixed-2R versus frozen downside-manager shadow.

The only inputs are already emitted canonical signals and closed M1 bars. This
module has no import of broker/guard execution modules and no order API.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import queue
import sqlite3
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from rl_trade_manager.env import (CLOSE_FULL, HOLD, MOVE_SL_TO_BREAKEVEN,
    MOVE_SL_TO_PROTECTED_STRUCTURE, TradeManagementEnv)
from rl_trade_manager.features import NAMES
from rl_trade_manager.types import Bar, Trade

HERE=Path(__file__).resolve().parent
DATA_DIR=Path(os.environ.get("DATA_DIR",str(HERE)))
DB=DATA_DIR/"downside_manager_shadow_v1.sqlite3"
SCHEMA=HERE/"downside_manager_shadow_v1_schema.sql"
MODEL=HERE/"rl_trade_manager/downside_manager_v1/model.json"
THRESHOLD=0.9219345929635789
VERSION="DOWNSIDE_MANAGER_SHADOW_V1"
FEATURE_SCHEMA="RL_TRADE_MANAGER_58_V1"
ENABLED=os.environ.get("DOWNSIDE_MANAGER_SHADOW_ENABLED","false").lower() in ("1","true","yes")
BUFFER=DATA_DIR/"buffer.csv"
NY=ZoneInfo("America/New_York")
_WORKER=None
_QUEUE=queue.Queue(maxsize=2)


def _fixed_2r_price(entry,direction,risk):
    """The frozen benchmark rounds the +2R target outward to an MNQ tick."""
    units=(entry+direction*2*risk)/0.25
    return (math.ceil(units-1e-10) if direction==1 else math.floor(units+1e-10))*0.25


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dol_hash():
    files=(HERE/"audit_results/ab_dol_state_audit_20260915/run.py",
           HERE/"audit_results/ab_dol_qualitative_random10_20260915/build_two_horizon_preoutcome.py")
    h=hashlib.sha256()
    for path in files:h.update(path.read_bytes())
    return h.hexdigest()


def _feature_hash():
    return hashlib.sha256(json.dumps(NAMES,separators=(",",":")).encode()).hexdigest()


def _connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


def migrate():
    with _connect() as c:
        c.executescript(SCHEMA.read_text())


def _model():
    m=json.loads(MODEL.read_text())
    return {"mean":np.asarray(m["mean"],float),"scale":np.asarray(m["scale"],float),
            "weight":np.asarray(m["weight"],float),"intercept":float(m["intercept"])}


def _probability(observation,model):
    x=np.asarray(observation,dtype=float)
    if x.shape!=(58,) or not np.isfinite(x).all():
        raise ValueError("Invalid 58-element causal observation")
    z=float(((x-model["mean"])/model["scale"])@model["weight"]+model["intercept"])
    return 1/(1+math.exp(-max(-700,min(700,z))))


def _parse_ms(value):
    text=str(value).strip().replace("Z","+00:00")
    if "+" not in text and text.count("-")<=2:text+="+00:00"
    return int(dt.datetime.fromisoformat(text).timestamp()*1000)


def _rows_from_buffer():
    if not BUFFER.exists():return []
    rows={}
    with BUFFER.open(newline="") as stream:
        for row in csv.DictReader(stream):
            try:
                bar=Bar(_parse_ms(row["ts_event"]),*(float(row[k]) for k in ("open","high","low","close")))
                if all(math.isfinite(v) for v in (bar.open,bar.high,bar.low,bar.close)):
                    rows[bar.ms]=bar
            except (KeyError,ValueError,OverflowError):
                continue
    return [rows[k] for k in sorted(rows)]


def _candidate(signal,source_key,quantity):
    if str(signal.get("_strat","A/B"))!="A/B":return None
    family=str(signal.get("cls","")).upper()
    if family not in ("A","B") or signal.get("kind")=="A/B shallow":return None
    direction={"LONG":1,"SHORT":-1}.get(str(signal.get("dir","")).upper())
    if direction is None:return None
    entry=float(signal["entry"]);sl=float(signal["SL"])
    risk=abs(entry-sl)
    if risk<=0 or direction*(entry-sl)<=0:return None
    qty=int(quantity)
    if qty<1:return None
    bos=int(signal["bos_ms"])
    anchor=max(bos,int(signal.get("entry_ms") or bos))
    safe={k:signal.get(k) for k in ("model","cls","cat","fvg_lo","fvg_hi","ce","emitted","session","bos_ms","entry_ms")}
    safe["source_key"]=str(source_key)
    return (direction,entry,sl,_fixed_2r_price(entry,direction,risk),
            qty,risk,bos,anchor,safe)


def observe_signal(signal,source_key,quantity):
    """Called strictly after canonical decision/persistence; never mutates it."""
    if not ENABLED:return 0
    value=_candidate(signal,source_key,quantity)
    if value is None:return 0
    direction,entry,sl,tp,qty,risk,bos,anchor,safe=value
    strategies=["A/B"]
    try:
        import a_cont_both_aligned_shadow as aligned
        if aligned.eligibility(signal,signal.get("_dol")).get("accepted"):
            strategies.append("A_CONT_BOTH_ALIGNED")
    except Exception as exc:
        print("[downside-shadow] aligned eligibility unavailable:",exc,flush=True)
    inserted=0
    with _connect() as c:
        for strategy in strategies:
            trade_id=f"{strategy}|{source_key}"
            cur=c.execute("""INSERT OR IGNORE INTO downside_shadow_trades
                (source_key,trade_id,strategy_id,signal_json,direction,entry,initial_sl,fixed_tp,
                 quantity,initial_risk,signal_ms,entry_anchor_ms,control_virtual_sl,manager_virtual_sl,
                 model_hash,threshold,policy_version,feature_schema_version,dol_runtime_hash)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (str(source_key),trade_id,strategy,json.dumps(safe,separators=(",",":")),direction,
                  entry,sl,tp,qty,risk,bos,anchor,sl,sl,_sha(MODEL),THRESHOLD,VERSION,
                  FEATURE_SCHEMA+":"+_feature_hash(),_dol_hash()))
            inserted+=cur.rowcount
    return inserted


def _raw_state(engine,decision_ms):
    from audit_results.ab_dol_state_audit_20260915.run import direction_tag, resolve_delivery
    from audit_results.ab_dol_qualitative_random10_20260915.build_two_horizon_preoutcome import execution_dol
    long=direction_tag(engine,{"fill_ms":decision_ms},"LONG")
    short=direction_tag(engine,{"fill_ms":decision_ms},"SHORT")
    direction,selected,_=resolve_delivery(long,short)
    htf=selected.get("current_dol") if selected else None
    cutoff=int(np.searchsorted(engine.ms,decision_ms-60_000,side="right")-1)
    execution=execution_dol(engine,cutoff,decision_ms)
    state={"decision_ms":decision_ms,"htf_direction":direction,
           "htf_price":float(htf["pool_price"]) if htf else None,
           "htf_status":selected["dol_status"] if selected and selected["dol_status"] in ("OPEN","DELIVERED") else "UNCLEAR",
           "htf_id":htf["pool_id"] if htf else None,
           "execution_direction":execution["direction"],"execution_price":execution["price"],
           "execution_type":execution["type"]}
    state["execution_status"]="OPEN" if state["execution_price"] is not None else "UNCLEAR"
    return state


def _next_state(engine,previous,bar):
    state=_raw_state(engine,bar.ms+60_000)
    selected=((previous["execution_direction"],previous["execution_price"],previous.get("execution_type"))
              if previous.get("execution_price") is not None else None)
    delivered=previous.get("execution_status")=="DELIVERED"
    if selected:
        direction,price,_=selected
        delivered|=bar.high>=price if direction=="LONG" else bar.low<=price
    raw_direction=state["execution_direction"]
    if state["execution_price"] is not None:
        new=(raw_direction,state["execution_price"],state.get("execution_type"))
        if new!=selected:delivered=False
        state["execution_status"]="OPEN"
    elif delivered and selected and raw_direction in (None,selected[0]):
        state["execution_direction"],state["execution_price"],state["execution_type"]=selected
        state["execution_status"]="DELIVERED"
    else:
        state["execution_status"]="UNCLEAR"
    return state


def _placeholder(decision_ms):
    return {"decision_ms":decision_ms,"htf_direction":None,"htf_price":None,
            "htf_status":"UNCLEAR","execution_direction":None,"execution_price":None,
            "execution_type":None,"execution_status":"UNCLEAR"}


def _fill_eligible(row,bar):
    if bar.ms<=row["entry_anchor_ms"]:return False
    direction=row["direction"];entry=row["entry"]
    return bar.low<=entry-0.25 if direction==1 else bar.high>=entry+0.25


def _trade(row,bars,pre,states):
    signal=json.loads(row["signal_json"])
    final=bars[-1] if bars else Bar(row["fill_ms"],row["entry"],row["entry"],row["entry"],row["entry"])
    local=dt.datetime.fromtimestamp(final.ms/1000,NY)
    eod=(local.hour,local.minute)>=(15,55) and (local.hour,local.minute)<(18,0)
    if eod:
        stream=tuple(bars)
        reason="EOD"
    else:
        dummy=Bar(final.ms+60_000,final.close,final.close,final.close,final.close)
        stream=tuple(bars)+(dummy,)
        reason="LIVE"
    try:fvg_signal=_parse_ms(signal["emitted"]) if signal.get("emitted") else None
    except Exception:fvg_signal=None
    initial=states[0] if states else _placeholder(row["fill_ms"])
    return Trade(row["trade_id"],0,row["direction"],row["entry"],row["initial_sl"],
                 row["fixed_tp"],row["quantity"],row["initial_risk"],row["fill_ms"],0,
                 reason,0.0,"LIVE",row["strategy_id"],
                 1 if initial["htf_direction"]=="LONG" else -1 if initial["htf_direction"]=="SHORT" else 0,
                 initial["htf_price"],initial["htf_status"],
                 1 if initial["execution_direction"]=="LONG" else -1 if initial["execution_direction"]=="SHORT" else 0,
                 initial["execution_price"],stream,tuple(pre),
                 float(signal["fvg_lo"]) if signal.get("fvg_lo") is not None else None,
                 float(signal["fvg_hi"]) if signal.get("fvg_hi") is not None else None,
                 tuple(states),fvg_signal,
                 float(signal["ce"]) if signal.get("ce") is not None else None)


def _decision(env,observation,model,state_quality):
    if env.i==0:return HOLD,None,"HOLD_ENTRY"
    if state_quality!="COMPLETE":return HOLD,None,"HOLD_STATE_UNAVAILABLE"
    p=_probability(observation,model)
    if p<THRESHOLD:return HOLD,p,"HOLD"
    masks=env.action_masks()
    if masks[MOVE_SL_TO_PROTECTED_STRUCTURE]:
        return MOVE_SL_TO_PROTECTED_STRUCTURE,p,"PROTECTED_STOP"
    if masks[MOVE_SL_TO_BREAKEVEN]:return MOVE_SL_TO_BREAKEVEN,p,"BE"
    return CLOSE_FULL,p,"CLOSE_EARLY"


def _replay(row,bars,pre,states,model,quality):
    trade=_trade(row,bars,pre,states)
    controls=[]
    for managed in (False,True):
        env=TradeManagementEnv(trade)
        observation,_=env.reset()
        last_probability=None;recommendation="HOLD_ENTRY"
        for _ in range(len(bars)):
            if env.terminated:break
            action,last_probability,recommendation=(
                _decision(env,observation,model,quality) if managed else (HOLD,None,"HOLD_CONTROL"))
            observation,_,done,truncated,info=env.step(action)
            if truncated or info["invalid_action"]:raise AssertionError("Invalid shadow action")
            if env.sl!=trade.initial_sl and trade.direction*(env.sl-trade.initial_sl)<-1e-10:
                raise AssertionError("Shadow stop widened")
            if done:break
        if not env.terminated and managed:
            _,last_probability,recommendation=_decision(env,observation,model,quality)
        controls.append((env,last_probability,recommendation))
    control,manager=controls
    cenv=control[0];menv=manager[0]
    complete=cenv.terminated and menv.terminated
    m1=menv._m1() if not menv.terminated else None
    active_state=(states[menv.i] if menv.i<len(states) else states[-1]) if states else None
    return {"_last_processed_i":max(cenv.i,menv.i),
            "status":"DONE" if complete else "OPEN",
            "control_status":"DONE" if cenv.terminated else "OPEN",
            "manager_status":"DONE" if menv.terminated else "OPEN",
            "control_final_r":float(cenv._equity()) if cenv.terminated else None,
            "manager_final_r":float(menv._equity()) if menv.terminated else None,
            "delta_r":float(menv._equity()-cenv._equity()) if complete else None,
            "control_exit_reason":cenv.reason,"manager_exit_reason":menv.reason,
            "control_virtual_sl":float(cenv.sl),"manager_virtual_sl":float(menv.sl),
            "current_r":float(menv._equity()),"mfe_r":float(menv.mfe_r),"mae_r":float(menv.mae_r),
            "dol_state_json":json.dumps(active_state,separators=(",",":")) if active_state else None,
            "m1_state_json":json.dumps(m1,separators=(",",":")) if m1 else None,
            "manager_probability":manager[1],"recommendation":manager[2] if not menv.terminated else "CLOSED"}


def refresh():
    """Catch up every open record from closed buffer bars; idempotent across restarts."""
    if not ENABLED:return 0
    all_bars=_rows_from_buffer()
    if not all_bars:return 0
    model=_model()
    with _connect() as c:
        active=c.execute("SELECT * FROM downside_shadow_trades WHERE status IN ('PENDING','OPEN') ORDER BY id").fetchall()
    if not active:return 0
    engine=None
    changed=0
    for source in active:
        row=dict(source)
        candidate=[bar for bar in all_bars if bar.ms>=row["entry_anchor_ms"] and
                   (row["last_bar_ms"] is None or bar.ms>row["last_bar_ms"])]
        if not candidate:continue
        bars=[Bar(*b) for b in json.loads(row["bars_json"])]
        pre=[Bar(*b) for b in json.loads(row["pre_bars_json"])]
        states=json.loads(row["dol_states_json"])
        quality=row["state_quality"]
        for bar in candidate:
            if row["status"]=="PENDING":
                if _fill_eligible(row,bar):
                    prior=[x for x in all_bars if x.ms<bar.ms]
                    pre=prior[-120:]
                    if len(pre)<120:
                        quality="UNAVAILABLE_PREBARS"
                    row["fill_ms"]=bar.ms
                    row["status"]="OPEN"
                    bars=[];states=[]
                    if engine is None:
                        try:
                            import ab_dol_live
                            engine=ab_dol_live._engine_for(str(BUFFER))
                        except Exception as exc:
                            print("[downside-shadow] DOL engine unavailable:",exc,flush=True)
                    try:
                        states=[_raw_state(engine,bar.ms)] if engine is not None else [_placeholder(bar.ms)]
                    except Exception as exc:
                        print("[downside-shadow] entry DOL unavailable:",exc,flush=True)
                        states=[_placeholder(bar.ms)]
                        quality="UNAVAILABLE_DOL"
                    if quality=="PENDING":quality="COMPLETE"
                else:
                    age=(bar.ms-row["entry_anchor_ms"])//60_000
                    if age>=10:row["status"]="NO_FILL"
                    row["last_bar_ms"]=bar.ms
                    if row["status"]=="NO_FILL":break
                    continue
            if row["status"]!="OPEN":break
            if bars and bar.ms!=bars[-1].ms+60_000:
                quality="UNAVAILABLE_M1_GAP"
            bars.append(bar)
            if engine is None:
                try:
                    import ab_dol_live
                    engine=ab_dol_live._engine_for(str(BUFFER))
                except Exception:pass
            try:
                states.append(_next_state(engine,states[-1],bar) if engine is not None else _placeholder(bar.ms+60_000))
            except Exception as exc:
                print("[downside-shadow] DOL update unavailable:",exc,flush=True)
                states.append(_placeholder(bar.ms+60_000))
                quality="UNAVAILABLE_DOL"
            row["last_bar_ms"]=bar.ms
        replay=_replay(row,bars,pre,states,model,quality) if row["status"]=="OPEN" else None
        if replay and replay["status"]=="DONE":
            bars=bars[:replay["_last_processed_i"]]
            states=states[:len(bars)+1]
            row["last_bar_ms"]=bars[-1].ms
        update={"status":row["status"],"fill_ms":row["fill_ms"],"last_bar_ms":row["last_bar_ms"],
                "bars_json":json.dumps([[b.ms,b.open,b.high,b.low,b.close] for b in bars],separators=(",",":")),
                "pre_bars_json":json.dumps([[b.ms,b.open,b.high,b.low,b.close] for b in pre],separators=(",",":")),
                "dol_states_json":json.dumps(states,separators=(",",":")),"state_quality":quality}
        if replay:
            replay.pop("_last_processed_i")
            update.update(replay)
        columns=", ".join(f"{key}=?" for key in update)
        with _connect() as c:
            c.execute(f"UPDATE downside_shadow_trades SET {columns}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (*update.values(),row["id"]))
            if row["status"]=="OPEN":
                c.execute("""INSERT OR REPLACE INTO downside_shadow_decisions
                    (trade_fk,decision_ms,control_status,manager_status,current_r,mfe_r,mae_r,
                     control_virtual_sl,manager_virtual_sl,manager_probability,recommendation,
                     dol_state_json,m1_state_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (row["id"],row["last_bar_ms"]+60_000,update["control_status"],update["manager_status"],
                      update["current_r"],update["mfe_r"],update["mae_r"],update["control_virtual_sl"],
                      update["manager_virtual_sl"],update["manager_probability"],update["recommendation"],
                      update["dol_state_json"],update["m1_state_json"]))
        changed+=1
    return changed


def _worker():
    while True:
        try:
            _QUEUE.get(timeout=300)
        except queue.Empty:
            pass
        try:refresh()
        except Exception as exc:print("[downside-shadow] refresh error:",exc,flush=True)


def notify_bar():
    if not ENABLED:return
    try:_QUEUE.put_nowait(True)
    except queue.Full:pass  # next wake catches up from persisted buffer


def status():
    if not ENABLED:return {"enabled":False,"shadow_only":True,"broker_execution":False,
                           "policy_version":VERSION,"strategies":{"A/B":"READY","A_CONT_BOTH_ALIGNED":"READY","C":"NOT CONNECTED"}}
    with _connect() as c:
        records=[dict(r) for r in c.execute("SELECT strategy_id,status,source_key,control_final_r,manager_final_r,delta_r,recommendation,state_quality FROM downside_shadow_trades ORDER BY id")]
    def metric(values):
        gains=sum(x for x in values if x>0);losses=-sum(x for x in values if x<0)
        curve=np.r_[0.0,np.cumsum(values)]
        return {"trades":len(values),"wr":100*sum(x>0 for x in values)/len(values) if values else None,
                "pf":gains/losses if losses else None,"net_r":sum(values),
                "max_dd_r":float(np.max(np.maximum.accumulate(curve)-curve))}
    grouped={}
    for strategy in ("A/B","A_CONT_BOTH_ALIGNED"):
        done=[r for r in records if r["strategy_id"]==strategy and r["control_final_r"] is not None and r["manager_final_r"] is not None]
        control=metric([r["control_final_r"] for r in done]);manager=metric([r["manager_final_r"] for r in done])
        losers=[r for r in done if r["control_final_r"]<0]
        grouped[strategy]={"control":control,"manager":manager,
                           "delta_r":sum(r["delta_r"] for r in done),
                           "losers_improved":sum(r["manager_final_r"]>r["control_final_r"]+1e-10 for r in losers),
                           "winners_damaged":sum(r["control_final_r"]>0 and r["manager_final_r"]<r["control_final_r"]-1e-10 for r in done),
                           "avg_loser_before":sum(r["control_final_r"] for r in losers)/len(losers) if losers else None,
                           "avg_loser_after":sum(r["manager_final_r"] for r in losers)/len(losers) if losers else None,
                           "active_recommendations":[{"trade_id":r["source_key"],"recommendation":r["recommendation"],"state_quality":r["state_quality"]}
                                                     for r in records if r["strategy_id"]==strategy and r["status"]=="OPEN"]}
    return {"enabled":True,"shadow_only":True,"broker_execution":False,"policy_version":VERSION,
            "model_hash":_sha(MODEL),"threshold":THRESHOLD,
            "total":len(records),"pending":sum(r["status"]=="PENDING" for r in records),
            "open":sum(r["status"]=="OPEN" for r in records),
            "done":sum(r["status"]=="DONE" for r in records),"strategies":grouped,
            "strategy_c":"NOT CONNECTED: separate Railway service lacks the frozen 58-feature observer"}


def _page():
    data=status()
    lines=["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<title>Downside Manager Shadow</title><style>body{font:14px system-ui;margin:24px;background:#0b1220;color:#e5e7eb}table{border-collapse:collapse;width:100%}th,td{padding:8px;border:1px solid #334155;text-align:right}th:first-child,td:first-child{text-align:left}.tag{color:#fbbf24;font-weight:700}</style></head><body>",
           "<h2>DOWNSIDE MANAGER SHADOW</h2><p class='tag'>SHADOW ONLY — NO BROKER EXECUTION</p>",
           f"<p>Status: {html.escape('ENABLED' if data['enabled'] else 'DISABLED')} · Pending: {data.get('pending',0)} · Open: {data.get('open',0)} · Done: {data.get('done',0)}</p>",
           "<table><tr><th>Strategy</th><th>Trades</th><th>Control WR</th><th>Manager WR</th><th>Control PF</th><th>Manager PF</th><th>Control Net R</th><th>Manager Net R</th><th>Delta R</th><th>Losers improved</th><th>Winners damaged</th><th>Avg loser before / after</th><th>Max DD control / manager</th><th>Active recommendation</th></tr>"]
    for strategy,row in data["strategies"].items():
        if not isinstance(row,dict) or "control" not in row:continue
        c=row["control"];m=row["manager"]
        fmt=lambda x:"—" if x is None else f"{x:.3f}"
        active=", ".join(f"{x['trade_id']}: {x['recommendation']}" for x in row["active_recommendations"]) or "—"
        cells=[strategy,str(c["trades"]),fmt(c["wr"]),fmt(m["wr"]),fmt(c["pf"]),fmt(m["pf"]),
               fmt(c["net_r"]),fmt(m["net_r"]),fmt(row["delta_r"]),str(row["losers_improved"]),
               str(row["winners_damaged"]),fmt(row["avg_loser_before"])+" / "+fmt(row["avg_loser_after"]),
               fmt(c["max_dd_r"])+" / "+fmt(m["max_dd_r"]),active]
        lines.append("<tr>"+"".join("<td>"+html.escape(x)+"</td>" for x in cells)+"</tr>")
    lines.append("</table><p>Strategy C: NOT CONNECTED. <a href='/downside-shadow/status'>JSON status</a></p></body></html>")
    return "".join(lines)


def register(app):
    from flask import jsonify,Response
    app.add_url_rule("/downside-shadow/status","downside_shadow_status",lambda:jsonify(status()),methods=["GET"])
    app.add_url_rule("/downside-shadow","downside_shadow_page",lambda:Response(_page(),mimetype="text/html"),methods=["GET"])
    if ENABLED:
        migrate()
        global _WORKER
        if _WORKER is None:
            _WORKER=threading.Thread(target=_worker,name="downside-shadow",daemon=True)
            _WORKER.start()
            notify_bar()
        print(f"[downside-shadow] ENABLED SHADOW ONLY model={_sha(MODEL)[:12]} threshold={THRESHOLD:.9f} db={DB}",flush=True)
    else:
        print("[downside-shadow] DISABLED (set DOWNSIDE_MANAGER_SHADOW_ENABLED=true to start)",flush=True)
    return app


if __name__=="__main__":
    import sys
    if len(sys.argv)==2 and sys.argv[1]=="migrate":
        migrate();print(f"[downside-shadow] migrated {DB}")
    elif len(sys.argv)==2 and sys.argv[1]=="status":
        print(json.dumps(status(),indent=2))
    else:raise SystemExit("usage: python -m downside_manager_shadow_v1 migrate|status")
