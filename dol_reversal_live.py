"""DOL Delivery Reversal live lifecycle through the account's TradersPost webhook.

The broker-side bracket remains the emergency protection.  TradersPost webhook
acceptance is never called a fill: fills are detected causally from closed M1
bars and are labelled LOCAL_FILL_DETECTED.  Protected-structure stops are
virtual and trigger one idempotent exit webhook when breached.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:
    requests = None

import ab_dol_live
import dol_delivery_reversal_shadow as strategy
import dol_reversal_control

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(HERE)))
DB = Path(os.environ.get("DOL_REVERSAL_LIVE_DB", str(DATA_DIR / "dol_reversal_live.sqlite3")))
AUDIT = Path(os.environ.get("DOL_REVERSAL_AUDIT_CSV", str(DATA_DIR / "audit.csv")))
_LOCK = threading.RLock()

AUDIT_FIELDS = [
    "account_profile", "strategy", "signal_id", "client_order_id", "ticker",
    "contract_symbol", "side", "quantity", "entry_price", "initial_sl", "current_sl",
    "virtual_protected_stop", "take_profit", "risk_points", "submitted_at",
    "traderspost_http_status", "traderspost_success", "traderspost_signal_id",
    "traderspost_log_id", "traderspost_message", "local_fill_status", "local_fill_time",
    "manager_active", "manager_score", "manager_action", "manager_action_id",
    "manager_action_status", "last_processed_bar", "closed_at", "close_reason",
    "realized_r", "error_state",
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _account() -> str:
    return str(os.environ.get("ACCOUNT_LABEL") or os.environ.get("ACCOUNT_PLAN") or "account")


def _ticker() -> str:
    return str(os.environ.get("EXEC_TICKER", os.environ.get("CONTRACT", "MNQ1!")))


def _mode(name: str) -> str:
    return str(os.environ.get(name, "SHADOW") or "SHADOW").strip().upper()


def _live_entry() -> bool:
    ready = dol_reversal_control.readiness()
    return (_mode("DOL_REVERSAL_MODE") == "LIVE" and ready["live_activation_allowed"]
            and not dol_reversal_control.killed())


def _live_manager() -> bool:
    return (_mode("DOL_MANAGER_MODE") == "LIVE" and dol_reversal_control.readiness()["live_activation_allowed"]
            and os.environ.get("DOL_MANAGER_EXECUTION", "").upper() == "TRADERSPOST_WEBHOOK"
            and not dol_reversal_control.killed())


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _init() -> None:
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS trades(
          client_order_id TEXT PRIMARY KEY, account_profile TEXT NOT NULL,
          strategy TEXT NOT NULL, signal_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
          ticker TEXT NOT NULL, contract_symbol TEXT, side TEXT NOT NULL, quantity INTEGER,
          entry_price REAL NOT NULL, initial_sl REAL NOT NULL, current_sl REAL NOT NULL,
          virtual_protected_stop REAL, take_profit REAL NOT NULL, risk_points REAL NOT NULL,
          signal_bar_ms INTEGER NOT NULL, submitted_at TEXT, traderspost_http_status INTEGER,
          traderspost_success INTEGER NOT NULL DEFAULT 0, traderspost_signal_id TEXT,
          traderspost_log_id TEXT, traderspost_message TEXT, local_fill_status TEXT NOT NULL,
          local_fill_time TEXT, manager_active INTEGER NOT NULL DEFAULT 0,
          manager_score REAL, manager_action TEXT, manager_action_id TEXT,
          manager_action_status TEXT, last_processed_bar INTEGER, closed_at TEXT,
          close_reason TEXT, realized_r REAL, error_state TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, UNIQUE(account_profile,signal_id)
        );
        CREATE TABLE IF NOT EXISTS manager_actions(
          action_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, decision_bar_ms INTEGER NOT NULL,
          action TEXT NOT NULL, score REAL, threshold REAL NOT NULL, feature_json TEXT,
          previous_sl REAL, new_sl REAL, status TEXT NOT NULL, http_status INTEGER,
          traderspost_signal_id TEXT, traderspost_log_id TEXT, response_text TEXT,
          exit_signal_price REAL, execution_price REAL, execution_difference_points REAL,
          webhook_dispatched_at TEXT, bar_to_webhook_ms REAL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        # Forward-compatible migration for ledgers created by an earlier build.
        existing = {r[1] for r in con.execute("PRAGMA table_info(manager_actions)")}
        migrations = {
            "exit_signal_price": "REAL", "execution_price": "REAL",
            "execution_difference_points": "REAL", "webhook_dispatched_at": "TEXT",
            "bar_to_webhook_ms": "REAL",
        }
        for name, sql_type in migrations.items():
            if name not in existing:
                con.execute(f"ALTER TABLE manager_actions ADD COLUMN {name} {sql_type}")


def _json_response(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        try:
            value = json.loads(response.text or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _write_audit() -> None:
    _init()
    with _connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY created_at,client_order_id")]
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUDIT.with_suffix(AUDIT.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in AUDIT_FIELDS})
    os.replace(tmp, AUDIT)


def _fixed_2r(signal: dict[str, Any]) -> float:
    entry, sl = float(signal["entry"]), float(signal["SL"])
    risk = abs(entry - sl)
    return round(entry + (2.0 * risk if signal["dir"] == "LONG" else -2.0 * risk), 2)


def classify(signal: dict[str, Any], buffer_path: str) -> dict[str, Any]:
    """Attach causal DOL before Guard; in LIVE re-label the one physical A/B order."""
    if signal.get("_strat", "A/B") != "A/B" or str(signal.get("model", "")).lower() != "reversal":
        return {"accepted": False, "reason": "NOT_CANONICAL_AB"}
    if "_dol" not in signal:
        ab_dol_live.attach_metadata(signal, buffer_path)
    gate = strategy.eligibility(signal, signal.get("_dol"))
    signal["_dol_eligibility"] = gate
    if gate["reason"] == "DOL_STATE_UNAVAILABLE":
        signal["_dol_state"] = "DOL_STATE_UNAVAILABLE"
    if not gate["accepted"]:
        return gate
    candidate_id = str(signal.get("_setup_group_id") or signal.get("candidate_id") or
                       "%s|%s|%s|%s" % (signal.get("date"), signal.get("bos_ms"), signal.get("dir"), signal.get("entry")))
    sid = dol_reversal_control.signal_id(candidate_id)
    cid = dol_reversal_control.client_order_id(candidate_id, _account())
    # Feed the frozen manager's existing causal replay before relabelling; the
    # observer copies its inputs and cannot mutate this order.
    try:
        strategy.observe(signal, signal.get("_dol"), candidate_id=candidate_id)
    except Exception:
        pass
    signal.update({
        "_base_strat": "A/B", "_signal_id": sid, "_client_order_id": cid,
        "_dol_candidate_id": candidate_id, "_disable_partial": True,
        "_dol_live_eligible": True,
    })
    if _live_entry():
        signal["_strat"] = "DOL_DELIVERY_REVERSAL"
        signal["TP"] = _fixed_2r(signal)
        signal["tp_src"] = "dol_fixed_2r"
    return {**gate, "signal_id": sid, "client_order_id": cid, "live": _live_entry()}


def _ensure_trade(signal: dict[str, Any], quantity: int | None = None,
                  payload: dict[str, Any] | None = None) -> None:
    _init(); now = _now()
    payload = payload or {}
    entry = float(payload.get("limitPrice", signal["entry"]))
    sl = float((payload.get("stopLoss") or {}).get("stopPrice", signal["SL"]))
    tp = float((payload.get("takeProfit") or {}).get("limitPrice", signal["TP"]))
    with _connect() as con:
        con.execute("""INSERT OR IGNORE INTO trades(
          client_order_id,account_profile,strategy,signal_id,candidate_id,ticker,contract_symbol,
          side,quantity,entry_price,initial_sl,current_sl,take_profit,risk_points,signal_bar_ms,
          local_fill_status,manager_active,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
          signal["_client_order_id"], _account(), "DOL_DELIVERY_REVERSAL", signal["_signal_id"],
          signal["_dol_candidate_id"], _ticker(), _ticker(), signal["dir"], quantity,
          entry, sl, sl, tp, abs(entry-sl), int(signal.get("entry_ms") or signal["bos_ms"]),
          "NOT_FILLED", 0, now, now))
        if quantity is not None:
            con.execute("UPDATE trades SET quantity=?,updated_at=? WHERE client_order_id=?",
                        (int(quantity), now, signal["_client_order_id"]))


def claim_entry(signal: dict[str, Any], quantity: int, payload: dict[str, Any]) -> tuple[bool, str]:
    if signal.get("_strat") != "DOL_DELIVERY_REVERSAL":
        return True, "not_dol"
    if not _live_entry():
        return False, "dol_live_disabled"
    with _LOCK:
        # Persist exactly the price levels sent to TradersPost (including the
        # existing executor offset/tick alignment), not merely detector prices.
        _ensure_trade(signal, quantity, payload)
        now = _now()
        with _connect() as con:
            row = con.execute("SELECT submitted_at,traderspost_success,error_state FROM trades WHERE client_order_id=?",
                              (signal["_client_order_id"],)).fetchone()
            if row and (row["submitted_at"] or row["traderspost_success"] or row["error_state"] == "UNKNOWN_REQUIRES_REVIEW"):
                return False, "duplicate_or_unresolved_entry"
            con.execute("UPDATE trades SET submitted_at=?,traderspost_message=?,updated_at=? WHERE client_order_id=?",
                        (now, "PENDING", now, signal["_client_order_id"]))
        _write_audit()
    payload["extras"] = dict(payload.get("extras") or {}, signalId=signal["_signal_id"],
                             clientOrderId=signal["_client_order_id"], strategy="DOL_DELIVERY_REVERSAL",
                             accountProfile=_account())
    if os.environ.get("DOL_TRADERSPOST_TEST", "0") == "1":
        payload["test"] = True
    return True, "claimed"


def record_entry_response(signal: dict[str, Any], response: Any, error: str | None = None) -> None:
    if signal.get("_strat") != "DOL_DELIVERY_REVERSAL":
        return
    now = _now(); status = getattr(response, "status_code", None) if response is not None else None
    body = _json_response(response) if response is not None else {}
    success = bool(status is not None and 200 <= int(status) < 300 and body.get("success", True))
    message = str(body.get("message") or getattr(response, "text", "") or error or "")[:500]
    err = None if success else ("UNKNOWN_REQUIRES_REVIEW" if status is None else "WEBHOOK_REJECTED")
    with _LOCK, _connect() as con:
        con.execute("""UPDATE trades SET traderspost_http_status=?,traderspost_success=?,
          traderspost_signal_id=?,traderspost_log_id=?,traderspost_message=?,error_state=?,updated_at=?
          WHERE client_order_id=?""", (status, int(success), body.get("id"), body.get("logId"),
          "WEBHOOK_ACCEPTED" if success else message, err, now, signal["_client_order_id"]))
    _write_audit()


def _bar_ms(bar: dict[str, Any]) -> int:
    raw = str(bar.get("ts_event") or "")
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00") if ("Z" in raw or "+" in raw) else raw + "+00:00")
    return int(value.timestamp() * 1000)


def _post_action(row: sqlite3.Row, action_id: str, action: str, bar_ms: int,
                 score: float | None, feature: Any, new_sl: float | None = None,
                 exit_signal_price: float | None = None) -> str:
    now = _now()
    with _connect() as con:
        cur = con.execute("""INSERT OR IGNORE INTO manager_actions(
          action_id,client_order_id,decision_bar_ms,action,score,threshold,feature_json,
          previous_sl,new_sl,exit_signal_price,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)""",
          (action_id,row["client_order_id"],bar_ms,action,score,dol_reversal_control.FROZEN_THRESHOLD,
           json.dumps(feature,separators=(",",":"),default=str),row["current_sl"],new_sl,
           exit_signal_price,now,now))
        if cur.rowcount != 1:
            return "DUPLICATE_SKIPPED"
    if action == "HOLD":
        with _connect() as con:
            con.execute("UPDATE manager_actions SET status='NO_WEBHOOK',updated_at=? WHERE action_id=?", (now,action_id))
            con.execute("""UPDATE trades SET manager_score=?,manager_action='HOLD',manager_action_id=?,
              manager_action_status='NO_WEBHOOK',last_processed_bar=?,updated_at=? WHERE client_order_id=?""",
              (score,action_id,bar_ms,now,row["client_order_id"]))
        _write_audit(); return "NO_WEBHOOK"
    if action == "VIRTUAL_PROTECTED_STOP":
        with _connect() as con:
            con.execute("UPDATE manager_actions SET status='VIRTUAL_ARMED',updated_at=? WHERE action_id=?", (now,action_id))
            con.execute("""UPDATE trades SET manager_score=?,manager_action=?,manager_action_id=?,
              manager_action_status='VIRTUAL_PROTECTED_STOP',virtual_protected_stop=?,last_processed_bar=?,updated_at=?
              WHERE client_order_id=?""", (score,action,action_id,new_sl,bar_ms,now,row["client_order_id"]))
        _write_audit(); return "VIRTUAL_ARMED"
    payload = ({"ticker":row["contract_symbol"],"action":"breakeven","orderType":"stop"}
               if action == "BREAKEVEN" else
               {"ticker":row["contract_symbol"],"action":"exit","cancel":True,"ignoreTradingWindows":True})
    payload["extras"] = {"signalId":row["signal_id"],"managerActionId":action_id,
                         "strategy":"DOL_DELIVERY_REVERSAL","accountProfile":row["account_profile"]}
    if os.environ.get("DOL_TRADERSPOST_TEST", "0") == "1":
        payload["test"] = True
    url = os.environ.get("EXEC_WEBHOOK", "")
    response = None; error = None
    dispatched_at = _now()
    latency_ms = max(0.0, (time.time() * 1000.0) - float(bar_ms))
    with _connect() as con:
        con.execute("UPDATE manager_actions SET webhook_dispatched_at=?,bar_to_webhook_ms=?,updated_at=? WHERE action_id=?",
                    (dispatched_at, latency_ms, dispatched_at, action_id))
    try:
        if not url or requests is None: raise RuntimeError("EXEC_WEBHOOK unavailable")
        response = requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        error = str(exc)
    status = getattr(response,"status_code",None) if response is not None else None
    body = _json_response(response) if response is not None else {}
    accepted = bool(status is not None and 200 <= int(status) < 300 and body.get("success",True))
    result = (("EXIT_REQUEST_ACCEPTED" if action == "FULL_CLOSE" else "WEBHOOK_ACCEPTED") if accepted else
              "UNKNOWN_REQUIRES_REVIEW" if status is None else "WEBHOOK_REJECTED")
    text = str(body.get("message") or getattr(response,"text","") or error or "")[:500]
    if "no open position" in text.lower(): result = "NO_OPEN_POSITION_AT_TRADERSPOST"
    with _connect() as con:
        con.execute("""UPDATE manager_actions SET status=?,http_status=?,traderspost_signal_id=?,
          traderspost_log_id=?,response_text=?,updated_at=? WHERE action_id=?""",
          (result,status,body.get("id"),body.get("logId"),text,now,action_id))
        manager_active = (0 if accepted and action == "FULL_CLOSE" else int(row["manager_active"]))
        if result == "NO_OPEN_POSITION_AT_TRADERSPOST":
            manager_active = 0
        con.execute("""UPDATE trades SET manager_score=?,manager_action=?,manager_action_id=?,
          manager_action_status=?,current_sl=?,last_processed_bar=?,manager_active=?,
          closed_at=CASE WHEN ?='NO_OPEN_POSITION_AT_TRADERSPOST' THEN ? ELSE closed_at END,
          close_reason=CASE WHEN ?='NO_OPEN_POSITION_AT_TRADERSPOST' THEN ? ELSE close_reason END,
          error_state=?,updated_at=? WHERE client_order_id=?""",
          (score,action,action_id,result,
           float(row["entry_price"]) if accepted and action == "BREAKEVEN" else float(row["current_sl"]),bar_ms,
           manager_active,result,now,result,"NO_OPEN_POSITION_AT_TRADERSPOST",
           result if result in ("UNKNOWN_REQUIRES_REVIEW","NO_OPEN_POSITION_AT_TRADERSPOST") else None,
           now,row["client_order_id"]))
    _write_audit(); return result


def _manager_decision(row: sqlite3.Row, bar_ms: int) -> tuple[str,float|None,Any,float|None]:
    try:
        import dol_reversal_manager_shadow_v1 as manager
        manager.refresh()
        source = next((x for x in manager.rows() if x.get("candidate_id") == row["candidate_id"]), None)
        decision = (source.get("decisions") or [])[-1] if source else None
        if not decision or int(decision.get("decision_ms") or 0) != int(bar_ms):
            return "HOLD", None, None, None
        rec = str(decision.get("recommendation") or "HOLD")
        action = ("BREAKEVEN" if rec == "BE" else "VIRTUAL_PROTECTED_STOP" if rec == "PROTECTED_STOP"
                  else "FULL_CLOSE" if rec == "CLOSE_EARLY" else "HOLD")
        return action, decision.get("probability"), decision.get("feature_snapshot") or decision.get("m1"), decision.get("virtual_sl_after_action")
    except Exception:
        return "HOLD", None, None, None


def on_closed_bar(bar: dict[str, Any]) -> dict[str, Any]:
    """Advance local fills, virtual stops and frozen manager once per closed bar."""
    _init(); ms=_bar_ms(bar); high=float(bar["high"]); low=float(bar["low"]); now=_now(); events=[]
    with _LOCK:
        with _connect() as con:
            rows = list(con.execute("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY created_at"))
        for row in rows:
            if row["last_processed_bar"] is not None and ms <= int(row["last_processed_bar"]):
                continue
            if not row["traderspost_success"]:
                continue
            direction = str(row["side"])
            newly_filled = False
            if row["local_fill_status"] == "NOT_FILLED":
                # Strict trade-through on a later closed bar; a touch alone is not called a fill.
                filled = (low <= float(row["entry_price"])-0.25 if direction=="LONG"
                          else high >= float(row["entry_price"])+0.25)
                if not filled:
                    with _connect() as con:
                        con.execute("UPDATE trades SET last_processed_bar=?,updated_at=? WHERE client_order_id=?",
                                    (ms,now,row["client_order_id"]))
                    continue
                with _connect() as con:
                    con.execute("""UPDATE trades SET local_fill_status='LOCAL_FILL_DETECTED',local_fill_time=?,
                      manager_active=1,last_processed_bar=?,updated_at=? WHERE client_order_id=?""",
                      (now,ms,now,row["client_order_id"]))
                events.append({"client_order_id":row["client_order_id"],"event":"LOCAL_FILL_DETECTED"})
                row = dict(row); row.update(local_fill_status="LOCAL_FILL_DETECTED",manager_active=1,last_processed_bar=ms)
                newly_filled = True
            if not newly_filled:
                stop = float(row["current_sl"]); target = float(row["take_profit"])
                stop_hit = low <= stop if direction == "LONG" else high >= stop
                target_hit = high >= target if direction == "LONG" else low <= target
                if stop_hit or target_hit:
                    # Conservative SL-first resolution for a bar touching both.
                    reason = "SL" if stop_hit else "TP"
                    realized = ((stop-float(row["entry_price"]))/float(row["risk_points"]) if direction=="LONG" else
                                (float(row["entry_price"])-stop)/float(row["risk_points"])) if stop_hit else 2.0
                    with _connect() as con:
                        con.execute("""UPDATE trades SET local_fill_status='CLOSED_LOCALLY',manager_active=0,
                          closed_at=?,close_reason=?,realized_r=?,last_processed_bar=?,updated_at=? WHERE client_order_id=?""",
                          (now,reason,realized,ms,now,row["client_order_id"]))
                    events.append({"client_order_id":row["client_order_id"],"event":"CLOSED_LOCALLY","reason":reason})
                    continue
            vp = row["virtual_protected_stop"]
            if (vp is not None and _live_manager() and row["manager_active"] and
                    row["manager_action_status"] not in
                    ("PENDING","UNKNOWN_REQUIRES_REVIEW","EXIT_REQUEST_ACCEPTED","NO_OPEN_POSITION_AT_TRADERSPOST")):
                breached = low <= float(vp) if direction=="LONG" else high >= float(vp)
                if breached:
                    aid = hashlib.sha256(f"{row['account_profile']}|{row['signal_id']}|FULL_CLOSE|{ms}".encode()).hexdigest()
                    status = _post_action(row,aid,"FULL_CLOSE",ms,row["manager_score"],
                                          {"virtual_stop":vp},
                                          exit_signal_price=float(bar.get("close", vp)))
                    events.append({"client_order_id":row["client_order_id"],"event":"VIRTUAL_STOP_EXIT","status":status})
                    continue
            if not _live_manager() or not row["manager_active"]:
                continue
            if row["manager_action_status"] in ("PENDING","UNKNOWN_REQUIRES_REVIEW"):
                continue
            action,score,feature,new_sl = _manager_decision(row,ms)
            aid = hashlib.sha256(f"{row['account_profile']}|{row['signal_id']}|{action}|{ms}".encode()).hexdigest()
            result = _post_action(row,aid,action,ms,score,feature,new_sl)
            events.append({"client_order_id":row["client_order_id"],"event":action,"status":result})
        _write_audit()
    return {"bar_ms":ms,"events":events}


def rows(limit: int=500) -> list[dict[str,Any]]:
    _init()
    with _connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",(limit,))]


def actions(limit: int=1000) -> list[dict[str,Any]]:
    _init()
    with _connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM manager_actions ORDER BY created_at DESC LIMIT ?",(limit,))]


def view_rows(limit: int=500) -> list[dict[str,Any]]:
    """Dashboard projection; dollar P&L is local/modelled, never broker-confirmed."""
    point_value = float(os.environ.get("POINT_VALUE", "2") or 2)
    result = []
    for row in rows(limit):
        item = dict(row)
        rr = item.get("realized_r")
        qty = item.get("quantity")
        item["local_pnl_usd"] = (round(float(rr) * float(item["risk_points"]) * point_value * int(qty), 2)
                                  if rr is not None and qty is not None else None)
        item["entry_status"] = ("LOCAL_FILL_DETECTED" if item["local_fill_status"] == "LOCAL_FILL_DETECTED"
                                else "CLOSED_LOCALLY" if item["local_fill_status"] == "CLOSED_LOCALLY"
                                else "WEBHOOK_ACCEPTED" if item["traderspost_success"] else
                                item.get("error_state") or "PENDING")
        item["broker_fill_status"] = "UNAVAILABLE"
        result.append(item)
    return result


def status() -> dict[str,Any]:
    rs=rows()
    return {"status":"LIVE VIA TRADERSPOST WEBHOOK" if _live_entry() and _live_manager() else "SHADOW",
            "entry_mode":_mode("DOL_REVERSAL_MODE"),"manager_mode":_mode("DOL_MANAGER_MODE"),
            "manager_execution":os.environ.get("DOL_MANAGER_EXECUTION"),"kill_switch":dol_reversal_control.killed(),
            "account_profile":_account(),"audit_csv":str(AUDIT),"trades":len(rs),
            "webhook_accepted":sum(bool(r["traderspost_success"]) for r in rs),
            "local_fills":sum(r["local_fill_status"]=="LOCAL_FILL_DETECTED" for r in rs),
            "broker_fill_confirmation_available":False}


def register(app):
    from flask import jsonify, Response
    def data(): return jsonify(status=status(),rows=view_rows(),actions=actions())
    def page():
        return Response("""<!doctype html><meta charset=utf-8><title>DOL Reversal Live</title>
<style>body{background:#0b0e14;color:#e7ebf2;font:13px system-ui;padding:20px}.note{color:#fbbf24}.wrap{overflow:auto;border:1px solid #28344b;border-radius:10px}table{border-collapse:collapse;width:100%;font:12px ui-monospace,monospace}th,td{padding:7px 9px;border-bottom:1px solid #202b40;white-space:nowrap;text-align:left}th{color:#94a3b8;background:#111827;position:sticky;top:0}.cards{display:flex;gap:10px;margin:12px 0}.card{background:#111827;padding:10px 14px;border-radius:8px}</style>
<h2>DOL Reversal · LIVE via TradersPost webhook</h2><p class=note>WEBHOOK_ACCEPTED i LOCAL_FILL_DETECTED nie są BROKER_FILL_CONFIRMED. Virtual protected stop nie zmienia fizycznego initial SL.</p><div id=s class=cards></div><div class=wrap><table><thead><tr id=h></tr></thead><tbody id=b></tbody></table></div>
<script>const cols=['account_profile','strategy','signal_id','traderspost_signal_id','traderspost_log_id','side','quantity','entry_price','initial_sl','current_sl','take_profit','virtual_protected_stop','entry_status','manager_score','manager_action','manager_action_status','close_reason','realized_r','local_pnl_usd','broker_fill_status'];const esc=x=>String(x??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));fetch('/dol-reversal-live/data',{cache:'no-store'}).then(r=>r.json()).then(x=>{s.innerHTML=['status','account_profile','trades','webhook_accepted','local_fills'].map(k=>`<div class=card><b>${esc(k)}</b><br>${esc(x.status[k])}</div>`).join('');h.innerHTML=cols.map(k=>`<th>${esc(k)}</th>`).join('');b.innerHTML=x.rows.map(r=>`<tr>${cols.map(k=>`<td>${esc(r[k])}</td>`).join('')}</tr>`).join('')||'<tr><td colspan=20>Brak transakcji DOL.</td></tr>'})</script>""",mimetype="text/html")
    app.add_url_rule("/dol-reversal-live","dol_reversal_live_page",page)
    app.add_url_rule("/dol-reversal-live/data","dol_reversal_live_data",data)
    return app
