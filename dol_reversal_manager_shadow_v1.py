#!/usr/bin/env python3
"""Broker-inert shadow manager for DOL Delivery Reversal.

Consumes only candidates already ACCEPTED by dol_delivery_reversal_shadow.
The strategy's entry/SL/fixed +2R target are immutable.  This module compares:
  * CONTROL: fixed +2R / initial SL
  * MANAGER: frozen 58-feature downside classifier
No function in this file can place, modify, cancel, or retry broker orders.
"""
from __future__ import annotations

import datetime as dt
import json, math, os, queue, threading
from pathlib import Path
from typing import Any

import numpy as np

import downside_manager_shadow_v1 as base
from rl_trade_manager.env import HOLD, CLOSE_FULL, MOVE_SL_TO_BREAKEVEN, MOVE_SL_TO_PROTECTED_STRUCTURE, TradeManagementEnv
from rl_trade_manager.features import NAMES
from rl_trade_manager.types import Bar, Trade

HERE=Path(__file__).resolve().parent
DATA_DIR=Path(os.environ.get("DATA_DIR",str(HERE)))
STORE=DATA_DIR/"dol_reversal_manager_shadow_v1.json"
MODEL=HERE/"dol_reversal_manager_v1/model.json"
MODEL_META=json.loads(MODEL.read_text())
THRESHOLD=float(MODEL_META.get("threshold",0.892916))
VERSION="DOL_REVERSAL_MANAGER_SHADOW_V1"
FEATURE_SCHEMA="RL_TRADE_MANAGER_58_V1"
ENABLED=os.environ.get("DOL_REVERSAL_MANAGER_SHADOW_ENABLED","true").lower() in ("1","true","yes")
_LOCK=threading.RLock(); _QUEUE=queue.Queue(maxsize=2); _WORKER=None


def _load():
    try:
        x=json.loads(STORE.read_text()); return x if isinstance(x,list) else []
    except Exception:return []

def _save(rows):
    DATA_DIR.mkdir(parents=True,exist_ok=True); tmp=STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows,separators=(",",":"),default=str)); os.replace(tmp,STORE)

def _model():
    m=MODEL_META
    return {"mean":np.asarray(m["mean"],float),"scale":np.asarray(m["scale"],float),"weight":np.asarray(m["weight"],float),"intercept":float(m["intercept"])}

def _probability(observation,model):
    x=np.asarray(observation,float)
    if x.shape!=(58,) or not np.isfinite(x).all(): raise ValueError("Invalid 58-element causal observation")
    z=float(((x-model["mean"])/model["scale"])@model["weight"]+model["intercept"])
    return 1/(1+math.exp(-max(-700,min(700,z))))

def _decision(env,obs,model,quality):
    if env.i==0:return HOLD,None,"HOLD_ENTRY"
    if quality!="COMPLETE":return HOLD,None,"HOLD_STATE_UNAVAILABLE"
    p=_probability(obs,model)
    if p<THRESHOLD:return HOLD,p,"HOLD"
    masks=env.action_masks()
    if masks[MOVE_SL_TO_PROTECTED_STRUCTURE]:return MOVE_SL_TO_PROTECTED_STRUCTURE,p,"PROTECTED_STOP"
    if masks[MOVE_SL_TO_BREAKEVEN]:return MOVE_SL_TO_BREAKEVEN,p,"BE"
    return CLOSE_FULL,p,"CLOSE_EARLY"

def observe_candidate(strategy_row:dict[str,Any],signal:dict[str,Any]):
    """Observe only an already accepted strategy row.  Never mutates canonical signal."""
    if not ENABLED or not strategy_row.get("accepted_by_DOL_DELIVERY_REVERSAL"):return False
    cid=str(strategy_row["candidate_id"])
    safe={k:signal.get(k) for k in ("date","model","cls","cat","session","fvg_lo","fvg_hi","ce","emitted","bos_ms","entry_ms")}
    row={
      "candidate_id":cid,"created_at":dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
      "status":"PENDING","direction":1 if strategy_row["direction"]=="LONG" else -1,
      "entry":float(strategy_row["theoretical_entry"]),"initial_sl":float(strategy_row["SL"]),"fixed_tp":float(strategy_row["TP"]),
      "risk":abs(float(strategy_row["theoretical_entry"])-float(strategy_row["SL"])),"quantity":1,
      "bos_ms":int(strategy_row["bos_ms"]),"entry_anchor_ms":max(int(strategy_row["bos_ms"]),int(strategy_row.get("entry_ms") or strategy_row["bos_ms"])),
      "signal":safe,"strategy_context":{k:strategy_row.get(k) for k in ("catalyst","narrative_class","selected_dol","dol_price","dol_tier_class","dol_status","source_families","gate_reason")},
      "fill_ms":None,"last_bar_ms":None,"state_quality":"PENDING","bars":[],"pre_bars":[],"dol_states":[],"decisions":[],
      "control_status":"PENDING","manager_status":"PENDING","control_final_r":None,"manager_final_r":None,"delta_r":None,
      "control_exit_reason":None,"manager_exit_reason":None,"manager_probability":None,"recommendation":"WAIT_FILL",
      "manager_virtual_sl":float(strategy_row["SL"]),"control_virtual_sl":float(strategy_row["SL"]),
      "execution_enabled":False,"model_version":MODEL_META.get("version"),"threshold":THRESHOLD,
    }
    with _LOCK:
        rows=_load()
        if any(x.get("candidate_id")==cid for x in rows):return False
        rows.append(row);_save(rows)
    notify_bar();return True

def _trade(row,bars,pre,states):
    s=row["signal"]; final=bars[-1] if bars else Bar(row["fill_ms"],row["entry"],row["entry"],row["entry"],row["entry"])
    local=dt.datetime.fromtimestamp(final.ms/1000,base.NY); eod=(local.hour,local.minute)>=(15,55) and (local.hour,local.minute)<(18,0)
    stream=tuple(bars) if eod else tuple(bars)+(Bar(final.ms+60_000,final.close,final.close,final.close,final.close),)
    reason="EOD" if eod else "LIVE"; initial=states[0] if states else base._placeholder(row["fill_ms"])
    try:fvg_signal=base._parse_ms(s["emitted"]) if s.get("emitted") else None
    except Exception:fvg_signal=None
    return Trade("DOL_REV|"+row["candidate_id"],0,row["direction"],row["entry"],row["initial_sl"],row["fixed_tp"],1,row["risk"],row["fill_ms"],0,reason,0.0,"LIVE","DOL_DELIVERY_REVERSAL",
      1 if initial["htf_direction"]=="LONG" else -1 if initial["htf_direction"]=="SHORT" else 0,initial["htf_price"],initial["htf_status"],
      1 if initial["execution_direction"]=="LONG" else -1 if initial["execution_direction"]=="SHORT" else 0,initial["execution_price"],stream,tuple(pre),
      float(s["fvg_lo"]) if s.get("fvg_lo") is not None else None,float(s["fvg_hi"]) if s.get("fvg_hi") is not None else None,tuple(states),fvg_signal,float(s["ce"]) if s.get("ce") is not None else None)

def _replay(row,bars,pre,states,quality):
    model=_model();trade=_trade(row,bars,pre,states); paths={}
    manager_decisions=[]
    for name,managed in (("control",False),("manager",True)):
        env=TradeManagementEnv(trade);obs,_=env.reset();decisions=[];exit_ms=None
        for i,bar in enumerate(bars):
            if env.terminated:break
            action,p,rec=_decision(env,obs,model,quality) if managed else (HOLD,None,"HOLD_CONTROL")
            m1=env._m1();dol=states[env.i] if env.i<len(states) else base._placeholder(trade.fill_ms+env.i*60_000)
            d={"decision_ms":trade.fill_ms+i*60_000,"price":float(env.last_price),"current_r":float(env._equity()),"mfe_r":float(env.mfe_r),"mae_r":float(env.mae_r),
               "giveback_r":float(m1["giveback_from_mfe_r"]),"probability":p,"threshold":THRESHOLD if managed else None,"recommendation":rec,"action":int(action),
               "virtual_sl":float(env.sl),"fixed_tp":float(trade.target),"m1":m1,"dol":dol,"state_quality":quality}
            obs,_,done,truncated,info=env.step(action)
            if truncated or info["invalid_action"]:raise AssertionError("Invalid shadow action")
            d["virtual_sl_after_action"]=float(env.sl);decisions.append(d)
            if done:exit_ms=bar.ms;break
        paths[name]={"status":"DONE" if env.terminated else "OPEN","final_r":float(env._equity()) if env.terminated else None,"exit_reason":env.reason,"exit_ms":exit_ms,"virtual_sl":float(env.sl),"decisions":decisions}
        if managed:manager_decisions=decisions
    c,m=paths["control"],paths["manager"]
    return {"control_status":c["status"],"manager_status":m["status"],"control_final_r":c["final_r"],"manager_final_r":m["final_r"],
      "delta_r":(m["final_r"]-c["final_r"] if m["final_r"] is not None and c["final_r"] is not None else None),
      "control_exit_reason":c["exit_reason"],"manager_exit_reason":m["exit_reason"],"control_virtual_sl":c["virtual_sl"],"manager_virtual_sl":m["virtual_sl"],
      "manager_probability":manager_decisions[-1]["probability"] if manager_decisions else None,"recommendation":manager_decisions[-1]["recommendation"] if manager_decisions else "HOLD_ENTRY",
      "decisions":manager_decisions,"detail":{"control":c,"manager":m,"candles":[[b.ms,b.open,b.high,b.low,b.close] for b in bars],"delta_r":(m["final_r"]-c["final_r"] if m["final_r"] is not None and c["final_r"] is not None else None)}}

def refresh():
    if not ENABLED:return 0
    all_bars=base._rows_from_buffer()
    if not all_bars:return 0
    try:
        import ab_dol_live; engine=ab_dol_live._engine_for(str(base.BUFFER))
    except Exception:engine=None
    changed=0
    with _LOCK:
      rows=_load()
      for row in rows:
        if row["status"] not in ("PENDING","OPEN"):continue
        new=[b for b in all_bars if b.ms>=row["entry_anchor_ms"] and (row["last_bar_ms"] is None or b.ms>row["last_bar_ms"])]
        if not new:continue
        bars=[Bar(*x) for x in row["bars"]];pre=[Bar(*x) for x in row["pre_bars"]];states=row["dol_states"];quality=row["state_quality"]
        for bar in new:
          if row["status"]=="PENDING":
            fill=(bar.ms>row["entry_anchor_ms"] and (bar.low<=row["entry"]-0.25 if row["direction"]==1 else bar.high>=row["entry"]+0.25))
            if fill:
                pre=[x for x in all_bars if x.ms<bar.ms][-120:];row["fill_ms"]=bar.ms;row["status"]="OPEN";bars=[];states=[]
                quality="COMPLETE" if len(pre)>=120 and engine is not None else ("UNAVAILABLE_PREBARS" if len(pre)<120 else "UNAVAILABLE_DOL")
                try:states=[base._raw_state(engine,bar.ms)] if engine is not None else [base._placeholder(bar.ms)]
                except Exception:states=[base._placeholder(bar.ms)];quality="UNAVAILABLE_DOL"
            else:
                if (bar.ms-row["entry_anchor_ms"])//60_000>=10:row["status"]="NO_FILL";row["recommendation"]="NO_FILL";break
                row["last_bar_ms"]=bar.ms;continue
          if row["status"]!="OPEN":break
          if bars and bar.ms!=bars[-1].ms+60_000:quality="UNAVAILABLE_M1_GAP"
          bars.append(bar)
          try:states.append(base._next_state(engine,states[-1],bar) if engine is not None else base._placeholder(bar.ms+60_000))
          except Exception:states.append(base._placeholder(bar.ms+60_000));quality="UNAVAILABLE_DOL"
          row["last_bar_ms"]=bar.ms
        row["bars"]=[[b.ms,b.open,b.high,b.low,b.close] for b in bars];row["pre_bars"]=[[b.ms,b.open,b.high,b.low,b.close] for b in pre];row["dol_states"]=states;row["state_quality"]=quality
        if row["status"]=="OPEN" and bars:
            rep=_replay(row,bars,pre,states,quality);row.update(rep)
            if rep["control_status"]=="DONE" and rep["manager_status"]=="DONE":row["status"]="DONE";row["recommendation"]="CLOSED"
        changed+=1
      if changed:_save(rows)
    return changed

def notify_bar():
    if not ENABLED:return
    try:_QUEUE.put_nowait(True)
    except queue.Full:pass

def _worker():
    while True:
      try:_QUEUE.get(timeout=300)
      except queue.Empty:pass
      try:refresh()
      except Exception as exc:print("[dol-reversal-manager] refresh error",exc,flush=True)

def rows():
    refresh();return _load()

def status():
    rs=rows() if ENABLED else []
    done=[r for r in rs if r.get("control_final_r") is not None and r.get("manager_final_r") is not None]
    def metric(key):
      v=[float(r[key]) for r in done];g=sum(x for x in v if x>0);l=-sum(x for x in v if x<0);curve=np.r_[0,np.cumsum(v)] if v else np.array([0.])
      return {"trades":len(v),"wr":100*sum(x>0 for x in v)/len(v) if v else None,"pf":g/l if l else None,"net_r":sum(v),"max_dd_r":float(np.max(np.maximum.accumulate(curve)-curve))}
    winners=[r for r in done if r["control_final_r"]>0]; losers=[r for r in done if r["control_final_r"]<0]
    return {"enabled":ENABLED,"shadow_only":True,"broker_execution":False,"version":VERSION,"threshold":THRESHOLD,
      "pending":sum(r["status"]=="PENDING" for r in rs),"open":sum(r["status"]=="OPEN" for r in rs),"done":len(done),
      "control":metric("control_final_r"),"manager":metric("manager_final_r"),"delta_r":sum(float(r.get("delta_r") or 0) for r in done),
      "losers_improved":sum(r["manager_final_r"]>r["control_final_r"]+1e-10 for r in losers),"winners_damaged":sum(r["manager_final_r"]<r["control_final_r"]-1e-10 for r in winners)}

def detail(candidate_id):
    row=next((r for r in rows() if r.get("candidate_id")==candidate_id),None)
    if not row:return None
    return {"id":candidate_id,"trade_id":candidate_id,"source_key":candidate_id,"strategy":"DOL Delivery Reversal","strategy_id":"DOL_DELIVERY_REVERSAL",
      "session":row["signal"].get("session"),"status":row["status"],"side":"LONG" if row["direction"]==1 else "SHORT","entry":row["entry"],"initial_sl":row["initial_sl"],
      "fixed_tp":row["fixed_tp"],"quantity":row["quantity"],"fill_ms":row["fill_ms"],"state_quality":row["state_quality"],"control_r":row.get("control_final_r"),
      "manager_r":row.get("manager_final_r"),"delta_r":row.get("delta_r"),"detail":row.get("detail"),"context":row.get("strategy_context"),"signal":row.get("signal")}

def register(app):
    from flask import jsonify,Response
    import dol_reversal_manager_dashboard as dash
    app.add_url_rule("/dol-reversal-manager/status","dolrev_mgr_status",lambda:jsonify(status()))
    app.add_url_rule("/dol-reversal-manager/data","dolrev_mgr_data",lambda:jsonify({"status":status(),"rows":rows()}))
    # Register the trading-terminal dashboard and replay APIs.
    app.add_url_rule("/dol-reversal-manager","dolrev_mgr_page",lambda:Response(dash.page(),mimetype="text/html"))
    dash.register(app)
    if ENABLED:
      global _WORKER
      if _WORKER is None:_WORKER=threading.Thread(target=_worker,name="dol-reversal-manager",daemon=True);_WORKER.start();notify_bar()
      print(f"[dol-reversal-manager] ENABLED SHADOW ONLY threshold={THRESHOLD:.6f}",flush=True)
    return app
