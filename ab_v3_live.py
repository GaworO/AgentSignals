"""Durable account-local V3 monitor and fail-closed broker exit adapter.

SENT is not FILLED. Only a trusted, authenticated broker bridge can confirm
position ownership. No model fills, catch-up closes, exit retries or SL widening.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

import pandas as pd

import ab_v3_policy as policy
import v3_frozen_exit as frozen_exit

HERE = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_M1_CALLBACK = None
_ROUTE_CALLBACK = None


def now_ms():
    return int(time.time()*1000)


def db_path():
    return Path(os.environ.get("AB_V3_DB", str(Path(os.environ.get("DATA_DIR", HERE))/"ab_v3.sqlite3")))


def dump(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), allow_nan=False)


def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS bars(tf TEXT,contract TEXT,ts_ms INTEGER,bar_json TEXT NOT NULL,
        received_ms INTEGER NOT NULL,PRIMARY KEY(tf,contract,ts_ms));
      CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY,state TEXT NOT NULL,
        order_json TEXT NOT NULL,position_json TEXT,updated_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS broker_events(event_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,
        order_id TEXT,event_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS decisions(order_id TEXT,decision_ms INTEGER,payload_json TEXT NOT NULL,
        PRIMARY KEY(order_id,decision_ms));
      CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,
        decision_ms INTEGER,state TEXT NOT NULL,payload_json TEXT NOT NULL,updated_ms INTEGER NOT NULL,
        UNIQUE(order_id));
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS exit_trades(
        trade_id TEXT PRIMARY KEY, exit_mode TEXT NOT NULL, status TEXT NOT NULL,
        primary_manager TEXT, primary_reason TEXT, triggered_managers TEXT,
        triggered_reasons TEXT, signal_time INTEGER, requested_time INTEGER,
        broker_ack_time INTEGER, fill_time INTEGER, requested_price REAL,
        fill_price REAL, exit_r REAL, exit_pnl REAL, broker_order_id TEXT,
        broker_request_id TEXT,
        error_reason TEXT, research_json TEXT NOT NULL DEFAULT '{}',
        broker_response_json TEXT, updated_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS exit_transitions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,trade_id TEXT NOT NULL,
        at_ms INTEGER NOT NULL,status TEXT NOT NULL,detail TEXT);
      CREATE INDEX IF NOT EXISTS idx_v3_bars ON bars(contract,tf,ts_ms);
    """)
    if "broker_request_id" not in {r[1] for r in c.execute("PRAGMA table_info(exit_trades)")}:
        c.execute("ALTER TABLE exit_trades ADD COLUMN broker_request_id TEXT")
    return c


def mode():
    value = os.environ.get("AB_V3_MODE", "SHADOW").upper().strip()
    return value if value in {"OFF", "SHADOW", "LIVE"} else "INVALID"


def exit_mode():
    value = os.environ.get("V3_EXIT_MODE", "shadow").strip().lower()
    return value if value in {"shadow", "real"} else "invalid"


def exit_enabled():
    return {m: os.environ.get(f"V3_EXIT_{m}_ENABLED", "true").strip().lower() == "true"
            for m in ("M1", "M2", "M3")}


def exit_blockers():
    blockers = []
    if exit_mode() != "real": blockers.append("exit_mode:"+exit_mode())
    if os.environ.get("V3_EXIT_REAL_EXECUTION", "false").lower() != "true":
        blockers.append("real_execution_not_enabled")
    if os.environ.get("V3_EXIT_LIVE_PARITY_VERIFIED", "false").lower() != "true":
        blockers.append("live_event_parity_not_attested")
    if os.environ.get("V3_EXIT_SHADOW_VERIFIED", "false").lower() != "true":
        blockers.append("shadow_stage_not_verified")
    if os.environ.get("V3_EXIT_STAGING_CLOSE_VERIFIED", "false").lower() != "true":
        blockers.append("broker_staging_close_not_verified")
    blockers.extend(setting_blockers())
    if not any(exit_enabled().values()): blockers.append("no_exit_manager_enabled")
    return blockers


def _exit_transition(c, oid, status, at, detail=None):
    old = c.execute("SELECT status FROM exit_trades WHERE trade_id=?", (oid,)).fetchone()
    if old and old[0] == status: return
    c.execute("UPDATE exit_trades SET status=?,error_reason=?,updated_ms=? WHERE trade_id=?",
              (status, detail, at, oid))
    c.execute("INSERT INTO exit_transitions(trade_id,at_ms,status,detail) VALUES(?,?,?,?)",
              (oid,at,status,detail))


def contract():
    return os.environ.get("AB_V3_CONTRACT", "").strip()


def route():
    return str(_ROUTE_CALLBACK() if _ROUTE_CALLBACK else "")


def setting_blockers():
    reasons = []
    if mode() != "LIVE": reasons.append("manager_mode:"+mode())
    if not contract() or "!" in contract(): reasons.append("explicit_contract_required")
    if contract() != os.environ.get("EXEC_TICKER", "").strip(): reasons.append("feed_broker_contract_mismatch")
    if not os.environ.get("AB_V3_FEED_TOKEN"): reasons.append("feed_token_missing")
    if not os.environ.get("AB_V3_BROKER_TOKEN"): reasons.append("broker_token_missing")
    if os.environ.get("AB_V3_EXCLUSIVE_ROUTE", "0") != "1": reasons.append("exclusive_route_not_confirmed")
    if os.environ.get("AB_V3_DETECTOR_CONTRACT_VERIFIED", "0") != "1": reasons.append("detector_archive_contract_unverified")
    try:
        offset = float(os.environ.get("PRICE_OFFSET", "0") or 0)
    except ValueError:
        offset = float('nan')
    if offset != 0: reasons.append("price_offset_not_supported")
    if not route(): reasons.append("route_unknown")
    if not os.environ.get("EXEC_WEBHOOK"): reasons.append("exec_webhook_missing")
    return reasons


def _bar(raw, tf, current):
    if tf not in {"1s", "M1"}: raise ValueError("tf must be 1s or M1")
    stamp = pd.Timestamp(raw["ts_event"])
    if stamp.tzinfo is None: raise ValueError("UTC timezone required")
    at = int(stamp.timestamp()*1000)
    duration = 1000 if tf == "1s" else 60000
    if at % duration or at+duration > current: raise ValueError("unaligned or unclosed bar")
    vals = {k: float(raw[k]) for k in ("open", "high", "low", "close", "volume")}
    if not all(math.isfinite(v) for v in vals.values()) or vals["volume"] < 0:
        raise ValueError("invalid numbers")
    if not 0 < vals["low"] <= min(vals["open"], vals["close"]) <= max(vals["open"], vals["close"]) <= vals["high"]:
        raise ValueError("invalid OHLC geometry")
    return at, dict(ts_event=stamp.tz_convert("UTC").isoformat(), **vals)


def ingest(body, current=None):
    current = now_ms() if current is None else int(current)
    if body.get("contract") != contract() or not contract(): raise ValueError("contract mismatch")
    tf = body.get("tf")
    rows = body.get("bars")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1800: raise ValueError("expected 1..1800 bars")
    parsed = [_bar(b, tf, current) for b in rows]
    if len({at for at,_ in parsed}) != len(parsed): raise ValueError("duplicate timestamps in batch")
    changed = []
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for at, bar in parsed:
            old = c.execute("SELECT bar_json FROM bars WHERE tf=? AND contract=? AND ts_ms=?", (tf,contract(),at)).fetchone()
            encoded = dump(bar)
            if old and old[0] != encoded: raise ValueError("conflicting immutable bar")
            if not old:
                c.execute("INSERT INTO bars VALUES(?,?,?,?,?)", (tf,contract(),at,encoded,current))
                changed.append((at,bar))
    # Forward one M1 stream into the existing detector, never 1s. Historical
    # preload intentionally does not send old bars into the live worker.
    if tf == "M1" and _M1_CALLBACK:
        for at,bar in sorted(changed):
            if 0 <= current-(at+60000) <= 10000:
                _M1_CALLBACK(bar)
    return dict(accepted=len(changed), duplicate=len(parsed)-len(changed))


def frame(c, tf, symbol, start=0, end=None):
    rows = c.execute("SELECT ts_ms,bar_json FROM bars WHERE tf=? AND contract=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                     (tf,symbol,int(start),int(end or now_ms()))).fetchall()
    if not rows:
        return pd.DataFrame(columns=["open","high","low","close","volume"], index=pd.DatetimeIndex([],tz="UTC"))
    d = pd.DataFrame([json.loads(r["bar_json"]) for r in rows])
    d.index = pd.to_datetime([r["ts_ms"] for r in rows], unit="ms", utc=True)
    return d[["open","high","low","close","volume"]]


def _payload(order):
    raw = order.get("payload_json")
    if raw:
        return json.loads(raw)
    return order


def prepare(order, claim=False):
    """Persist BEFORE external entry. A crash cannot leave an unowned fill."""
    if order.get("strategy") != "AB_DIRECTIONAL" or mode() == "OFF": return
    source = _payload(order)
    snap = source.get("v3_snapshot")
    value = dict(order_id=order["order_id"], direction=order["direction"],
                 activation_ms=int(order["activation_ms"]), expiry_ms=int(order["expiry_ms"]),
                 entry_price=float(order["entry_price"]), stop_price=float(order["stop_price"]),
                 target_price=float(order["target_price"]), risk_points=float(order["risk_points"]),
                 contract=contract(), route_id=route(), account_label=os.environ.get("ACCOUNT_LABEL", "account"),
                 snapshot=snap, policy=policy.POLICY)
    with connect() as c:
        if claim and mode() == 'LIVE':
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT COUNT(*) FROM orders WHERE order_id<>? AND state IN ('PREPARED','WAITING_BROKER_FILL','SUBMISSION_UNKNOWN','OPEN','EXIT_PENDING','EXIT_UNKNOWN','EXIT_REJECTED','OWNERSHIP_MISMATCH')",(order['order_id'],)).fetchone()[0]:
                raise ValueError('exclusive route already owned')
        c.execute("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?)",
                  (order["order_id"],"PREPARED",dump(value),None,now_ms()))


def record_quantity(order_id, quantity):
    with connect() as c:
        row = c.execute('SELECT order_json FROM orders WHERE order_id=?',(order_id,)).fetchone()
        if row:
            value = json.loads(row[0])
            value['quantity'] = int(quantity)
            c.execute('UPDATE orders SET order_json=? WHERE order_id=?',(dump(value),order_id))


def record_dispatch(order, result):
    if order.get("strategy") != "AB_DIRECTIONAL" or mode() == "OFF": return
    prepare(order)
    state = "WAITING_BROKER_FILL" if result.get("state") == "SENT" else str(result.get("state", "ERROR"))
    with connect() as c:
        old = c.execute("SELECT order_json FROM orders WHERE order_id=?", (order["order_id"],)).fetchone()
        value = json.loads(old[0])
        value["quantity"] = result.get("quantity")
        value["dispatch_reason"] = result.get("reason")
        # Never regress a faster broker-confirmed state back to SENT.
        c.execute("UPDATE orders SET state=CASE WHEN position_json IS NULL THEN ? ELSE state END,order_json=?,updated_ms=? WHERE order_id=?",
                  (state,dump(value),now_ms(),order["order_id"]))


def broker_event(body, current=None):
    """Trusted bridge protocol: full position snapshots, not a TP receipt.

    Snapshots assert sole ownership of this route/contract and order attribution.
    Partial fills/geometry drift are visible but disable active V3 exits.
    """
    current = now_ms() if current is None else int(current)
    kind = body.get("event")
    if kind not in {"POSITION", "CLOSED", "CANCELLED", "FLAT"}: raise ValueError("unknown broker event")
    eid = str(body.get("event_id") or "")
    at = int(body["event_ms"])
    if not eid or len(eid)>200 or at>current+1000: raise ValueError("invalid event identity/time")
    if body.get("route_id") != route() or body.get("contract") != contract(): raise ValueError("broker route/contract mismatch")
    if body.get("account_label") != os.environ.get("ACCOUNT_LABEL", "account"): raise ValueError("account mismatch")
    fingerprint = hashlib.sha256(dump(body).encode()).hexdigest()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT payload_hash FROM broker_events WHERE event_id=?", (eid,)).fetchone()
        if old:
            if old[0] != fingerprint: raise ValueError("conflicting broker event id")
            return dict(duplicate=True)
        if kind == "FLAT":
            if int(body.get("quantity", -1)) != 0: raise ValueError("FLAT must have zero quantity")
            unresolved = c.execute("SELECT COUNT(*) FROM orders WHERE state IN ('PREPARED','WAITING_BROKER_FILL','SUBMISSION_UNKNOWN','OPEN','EXIT_PENDING','EXIT_UNKNOWN','EXIT_REJECTED','OWNERSHIP_MISMATCH')").fetchone()[0]
            if unresolved: raise ValueError("resolve known orders before FLAT")
            previous = c.execute("SELECT value FROM meta WHERE key='flat'").fetchone()
            if previous and json.loads(previous[0])["event_ms"] >= at: raise ValueError("stale FLAT event")
            c.execute("INSERT OR REPLACE INTO meta VALUES('flat',?)", (dump(body),))
        else:
            oid = str(body.get("order_id") or "")
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
            if not row: raise ValueError("unknown V3 order: no adoption of foreign positions")
            order = json.loads(row["order_json"])
            if order['route_id'] != body['route_id'] or order['contract'] != body['contract'] or order['account_label'] != body['account_label']:
                raise ValueError('order belongs to a different route/account/contract')
            if at < order['activation_ms']: raise ValueError('event before order activation')
            previous = json.loads(row["position_json"]) if row["position_json"] else {}
            if at <= previous.get("event_ms", -1): raise ValueError("out-of-order broker event")
            if row["state"] in {"CLOSED", "CANCELLED"}: raise ValueError("terminal order")
            if kind == "POSITION":
                qty = int(body["quantity"])
                if qty != body["quantity"] or qty <= 0: raise ValueError("invalid quantity")
                fill = int(body["fill_ms"])
                avg = float(body["entry_price"])
                if not math.isfinite(avg) or not order["activation_ms"] <= fill <= at or fill >= order["expiry_ms"]:
                    raise ValueError("invalid fill timestamp/price")
                if not body.get("position_id") or body.get("direction") != order["direction"]:
                    raise ValueError("position identity/direction mismatch")
                if previous.get("position_id") and previous["position_id"] != body["position_id"]:
                    raise ValueError("position changed")
                if previous.get("fill_ms") and (previous["fill_ms"] != fill or previous["entry_price"] != avg):
                    raise ValueError("fill changed")
                other = c.execute("SELECT COUNT(*) FROM orders WHERE order_id<>? AND state IN ('OPEN','EXIT_PENDING','EXIT_UNKNOWN','OWNERSHIP_MISMATCH')", (oid,)).fetchone()[0]
                compatible = (body.get("exclusive") is True and not other and qty == order.get("quantity")
                    and avg == order["entry_price"] and body.get("stop_price") == order["stop_price"]
                    and body.get("target_price") == order["target_price"])
                state = row["state"] if row["state"] in {"EXIT_PENDING", "EXIT_UNKNOWN", "EXIT_REJECTED"} else "OPEN" if compatible else "OWNERSHIP_MISMATCH"
            else:
                if int(body.get("quantity", -1)) != 0: raise ValueError("terminal event needs quantity zero")
                if kind == "CANCELLED" and previous.get("position_id"): raise ValueError("filled order cannot be cancelled")
                if kind == "CLOSED" and (not previous.get("position_id") or body.get("position_id") != previous["position_id"]):
                    raise ValueError("closed position mismatch")
                state = kind
                if body.get("realized_net_usd") is not None and not math.isfinite(float(body["realized_net_usd"])):
                    raise ValueError("invalid realized PNL")
            c.execute("UPDATE orders SET state=?,position_json=?,updated_ms=? WHERE order_id=?", (state,dump(body),current,oid))
            if kind in ("POSITION","CLOSED"):
                c.execute("INSERT OR IGNORE INTO exit_trades(trade_id,exit_mode,status,updated_ms) VALUES(?,?,?,?)",
                          (oid,exit_mode(),"OPEN",current))
            ex = c.execute("SELECT * FROM exit_trades WHERE trade_id=?", (oid,)).fetchone()
            if ex:
                if kind == "POSITION":
                    if state == "OWNERSHIP_MISMATCH":
                        _exit_transition(c,oid,"BROKER_MISMATCH",current,"broker_position_does_not_match_order")
                    elif ex["requested_time"] is not None and int(body["quantity"]) != int(order["quantity"]):
                        _exit_transition(c,oid,"EXIT_BLOCKED",current,"partial_exit_fill_requires_manual_reconciliation")
                elif kind == "CLOSED":
                    price = body.get("exit_price", body.get("fill_price"))
                    if price is not None and (not math.isfinite(float(price)) or float(price) <= 0):
                        raise ValueError("invalid exit fill price")
                    exit_r = ((1 if order["direction"] == "LONG" else -1)*
                              (float(price)-float(previous["entry_price"]))/float(order["risk_points"])) if price is not None else None
                    pnl = float(body["realized_net_usd"]) if body.get("realized_net_usd") is not None else None
                    fill_at = int(body.get("exit_fill_ms",at))
                    if fill_at > at or fill_at < int(previous["fill_ms"]):
                        raise ValueError("invalid exit fill timestamp")
                    linked = bool(
                        (ex["broker_order_id"] and body.get("broker_order_id") == ex["broker_order_id"])
                        or (ex["broker_request_id"] and body.get("broker_request_id") == ex["broker_request_id"]))
                    manager_fill = bool(ex["requested_time"] and fill_at >= ex["requested_time"]
                                        and ex["status"] in ("EXIT_REQUESTED","EXIT_ACKNOWLEDGED","EXIT_BLOCKED") and linked)
                    if not manager_fill:
                        # A bracket/manual close after a request is not evidence
                        # that the manager's own exit order filled.
                        c.execute("UPDATE exit_trades SET primary_manager=NULL,primary_reason=NULL,triggered_managers='[]',triggered_reasons='[]' WHERE trade_id=?",(oid,))
                    c.execute("UPDATE exit_trades SET fill_time=?,fill_price=?,exit_r=?,exit_pnl=?,updated_ms=? WHERE trade_id=?",
                              (fill_at,float(price) if price is not None else None,exit_r,pnl,current,oid))
                    _exit_transition(c,oid,"EXIT_FILLED",current)
                    _exit_transition(c,oid,"CLOSED",current)
                elif kind == "CANCELLED":
                    _exit_transition(c,oid,"CANCELLED",current,"entry_cancelled")
        c.execute("INSERT INTO broker_events VALUES(?,?,?,?,?)", (eid,fingerprint,body.get("order_id"),at,current))
    return dict(accepted=True)


def entry_blocker(order):
    """LIVE V3 requires an exclusive, fresh broker-flat route before ENTRY."""
    if mode() != "LIVE": return None
    if order.get("strategy") != "AB_DIRECTIONAL": return "v3_exclusive_route_other_strategy"
    blockers = setting_blockers()
    if blockers: return "v3:"+blockers[0]
    source = _payload(order)
    snap = source.get("v3_snapshot") or {}
    if not all(policy.finite(snap.get(k)) for k in ("source_level", "fvg_edge")): return "v3:snapshot_missing"
    current = now_ms()
    if not int(order['activation_ms']) <= current < int(order['expiry_ms']): return 'v3:order_outside_activation_expiry'
    with connect() as c:
        if c.execute("SELECT COUNT(*) FROM orders WHERE state IN ('PREPARED','WAITING_BROKER_FILL','SUBMISSION_UNKNOWN','OPEN','EXIT_PENDING','EXIT_UNKNOWN','EXIT_REJECTED','OWNERSHIP_MISMATCH')").fetchone()[0]:
            return "v3:broker_order_unresolved"
        flat = c.execute("SELECT value FROM meta WHERE key='flat'").fetchone()
        if not flat or not 0 <= current-json.loads(flat[0])["event_ms"] <= 2000: return "v3:broker_flat_not_fresh"
        s = c.execute("SELECT MAX(ts_ms) FROM bars WHERE tf='1s' AND contract=?", (contract(),)).fetchone()[0]
        if s is None or not 0 <= current-(s+1000) <= 5000: return "v3:1s_feed_not_fresh"
    return None


def _verified_seconds(seconds, minutes):
    """Require dense seconds and matching full M1 OHLCV for each full minute."""
    if seconds.empty: return False
    groups = seconds.resample("min")
    aggregate = groups.agg(dict(open="first",high="max",low="min",close="last",volume="sum"))
    full = aggregate.loc[groups.close.count().eq(60)]
    expected = pd.date_range(seconds.index[0].ceil("min"), seconds.index[-1].floor("min"),freq="min")
    if not expected.isin(full.index).all() or not full.index.isin(minutes.index).all(): return False
    for column in ("open","high","low","close","volume"):
        if not (full[column]-minutes.reindex(full.index)[column]).abs().le(1e-7).all(): return False
    return True


def monitor():
    if mode() in {"OFF", "INVALID"}: return
    current = now_ms()
    decision_ms = current//60000*60000
    decision = pd.Timestamp(decision_ms,unit="ms",tz="UTC")
    with connect() as c:
        rows = c.execute("SELECT * FROM orders WHERE state='OPEN'").fetchall()
        for row in rows:
            oid = row["order_id"]
            saved = c.execute("SELECT payload_json FROM decisions WHERE order_id=? AND decision_ms=?", (oid,decision_ms)).fetchone()
            if saved and json.loads(saved[0]).get('cause') != 'HOLD_DATA_GAP': continue
            o = json.loads(row["order_json"])
            if o['route_id'] != route() or o['contract'] != contract() or o['account_label'] != os.environ.get('ACCOUNT_LABEL','account'): continue
            p = json.loads(row["position_json"])
            if decision_ms <= p["fill_ms"]: continue
            o["fill_ms"] = p["fill_ms"]
            s = frame(c,"1s",o["contract"],p["fill_ms"]//1000*1000,decision_ms)
            m1 = frame(c,"M1",o["contract"],min(o["activation_ms"]-48*3600000,p["fill_ms"]-48*3600000),decision_ms)
            verdict = dict(exit=False,cause="HOLD_DATA_GAP",decision_ms=decision_ms,policy=policy.POLICY)
            if _verified_seconds(s,m1) and o.get("snapshot") and all(policy.finite(o["snapshot"].get(k)) for k in ("source_level","fvg_edge")):
                verdict = policy.evaluate(o,s,m1,decision)
            # Late feed/broker data never sends a catch-up exit. Re-evaluate next
            # completed minute instead. No guessed broker prices or real PNL.
            verdict["mode"] = mode()
            verdict["action_state"] = "WOULD_EXIT" if verdict["exit"] else "HOLD"
            if verdict["exit"] and mode() == "LIVE": verdict["action_state"] = "AWAIT_FRESH_BROKER_SNAPSHOT"
            c.execute("INSERT INTO decisions VALUES(?,?,?) ON CONFLICT(order_id,decision_ms) DO UPDATE SET payload_json=excluded.payload_json WHERE decisions.payload_json LIKE '%HOLD_DATA_GAP%'", (oid,decision_ms,dump(verdict)))
    return decision_ms


def frozen_parity_ok():
    """Fail closed if the audited live module or compact parity proof drifts."""
    try:
        proof = json.loads((HERE/"v3_exit_parity.json").read_text())
        if (proof.get("id") != "V3_LIVE_EXIT_PARITY_V1" or proof.get("status") != "PASS"
                or proof.get("checked") != 939 or proof.get("signal_mismatches") != 0):
            return False
        if hashlib.sha256((HERE/"v3_frozen_exit.py").read_bytes()).hexdigest() != proof["production_sha256"]:
            return False
        if proof["signal_ledger_sha256"] != "096f1ea48d9e753c0b9e939fbf6f130ff413b45e39753c4cf6ef1953b002dc66":
            return False
        if proof["frozen_spec_sha256"] != {
                "exit_library":"41025a9175a31a85dcfafc886191c39c4b802bbaa57eb3eed3dfb4243f6cb9ef",
                "manager_configs":"0693de00a47302b299a66aad54ed446b87bd8cbc956335dccaae94183b436769"}:
            return False
        expected = {"M1":(1.0605899225066255,7224.5),
                    "M2":(1.055670703928224,6782.0),
                    "M3":(1.0655553175493198,7804.0)}
        for manager,(pf,net) in expected.items():
            row=proof["benchmark"][manager]
            if abs(float(row["pf"])-pf)>1e-12 or float(row["net_usd"])!=net:
                return False
        row=proof["model0_m3"]
        return abs(float(row["pf"])-1.1143256905578625)<1e-12 and float(row["net_usd"])==13931.5
    except (OSError,ValueError,KeyError,IndexError,TypeError):
        return False


def monitor_frozen():
    if mode() in {"OFF","INVALID"} or exit_mode() == "invalid": return
    current = now_ms()
    decision_ms = current//60000*60000
    with connect() as c:
        rows = c.execute("SELECT o.*,e.status AS exit_status FROM orders o LEFT JOIN exit_trades e ON e.trade_id=o.order_id WHERE o.state='OPEN'").fetchall()
        for row in rows:
            oid = row["order_id"]
            if row["exit_status"] is None:
                c.execute("INSERT OR IGNORE INTO exit_trades(trade_id,exit_mode,status,updated_ms) VALUES(?,?,?,?)",
                          (oid,exit_mode(),"OPEN",current))
            if row["exit_status"] not in (None,"OPEN","MONITORING","STALE_DATA","EXIT_BLOCKED","BROKER_MISMATCH"):
                continue
            if row["exit_status"] in ("MONITORING","EXIT_BLOCKED","BROKER_MISMATCH"):
                saved=c.execute("SELECT research_json FROM exit_trades WHERE trade_id=?",(oid,)).fetchone()
                if saved and json.loads(saved[0] or "{}").get("known_ms") == decision_ms:
                    continue
            order = json.loads(row["order_json"])
            position = json.loads(row["position_json"])
            if order["contract"] != contract() or order["route_id"] != route() or order["account_label"] != os.environ.get("ACCOUNT_LABEL","account"):
                _exit_transition(c,oid,"BROKER_MISMATCH",current,"local_order_identity_mismatch")
                continue
            if current-decision_ms > 15000:
                _exit_transition(c,oid,"STALE_DATA",current,"completed_m1_decision_too_old")
                continue
            minutes = frame(c,"M1",order["contract"],min(order["activation_ms"],position["fill_ms"])-48*3600000,decision_ms)
            try:
                state = frozen_exit.evaluate(order,position,minutes,decision_ms,exit_enabled())
            except ValueError as exc:
                _exit_transition(c,oid,"STALE_DATA",current,str(exc))
                continue
            c.execute("UPDATE exit_trades SET research_json=?,updated_ms=? WHERE trade_id=?",
                      (dump(state),current,oid))
            signals = state["signals"]
            if not signals["primary_reason"]:
                prior = [state["first_events"].get(k) for k in frozen_exit.REASONS
                         if state["first_events"].get(k) and any(
                             exit_enabled()[m] and k in codes for m,codes in frozen_exit.MANAGER_REASONS.items())]
                if prior and min(prior) < decision_ms:
                    _exit_transition(c,oid,"EXIT_BLOCKED",current,"missed_first_signal_no_catchup")
                else:
                    _exit_transition(c,oid,"MONITORING",current)
                continue
            if int(state["known_ms"]) != decision_ms:
                _exit_transition(c,oid,"EXIT_BLOCKED",current,"signal_not_from_latest_completed_m1")
                continue
            c.execute("UPDATE exit_trades SET exit_mode=?,primary_manager=?,primary_reason=?,triggered_managers=?,triggered_reasons=?,signal_time=?,requested_price=?,error_reason=NULL,updated_ms=? WHERE trade_id=?",
                      (exit_mode(),signals["primary_manager"],signals["primary_reason"],
                       dump(signals["triggered_managers"]),dump(signals["triggered_reasons"]),
                       decision_ms,float(minutes.iloc[-1]["close"]),current,oid))
            _exit_transition(c,oid,"EXIT_SIGNALLED",current)


def _send_broker_exit(payload):
    import requests
    return requests.post(os.environ["EXEC_WEBHOOK"],json=payload,timeout=3)


def dispatch_frozen_exits():
    if exit_mode() != "real": return
    current = now_ms()
    tasks = []
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("SELECT o.*,e.* FROM exit_trades e JOIN orders o ON o.order_id=e.trade_id WHERE e.status='EXIT_SIGNALLED'").fetchall()
        for row in rows:
            oid=row["order_id"]
            order=json.loads(row["order_json"])
            position=json.loads(row["position_json"]) if row["position_json"] else {}
            reason=None
            age=current-int(row["signal_time"] or 0)
            if not frozen_parity_ok(): reason="historical_parity_failed"
            elif exit_blockers(): reason=";".join(exit_blockers())
            elif row["state"] != "OPEN": reason="trade_not_open"
            elif row["signal_time"] is None or age > 10000 or age < 0: reason="stale_signal_or_no_next_open"
            elif age < 1000: continue
            elif order["contract"] != contract() or order["route_id"] != route() or order["account_label"] != os.environ.get("ACCOUNT_LABEL","account"):
                reason="order_identity_mismatch"
            elif not position.get("exclusive") or position.get("contract") != order["contract"] or position.get("route_id") != order["route_id"] or position.get("account_label") != order["account_label"] or position.get("direction") != order["direction"]:
                reason="broker_position_identity_mismatch"
            elif int(position.get("quantity",0)) <= 0 or position.get("quantity") != order.get("quantity"):
                reason="broker_position_quantity_mismatch"
            elif not row["signal_time"]+1000 <= int(position.get("event_ms",0)) <= current or current-int(position.get("event_ms",0)) > 2000:
                continue  # Wait inside the 10-second window for a fresh broker snapshot.
            else:
                s=c.execute("SELECT MAX(ts_ms) FROM bars WHERE tf='1s' AND contract=?",(order["contract"],)).fetchone()[0]
                if s is None or not 0 <= current-(s+1000) <= 5000:
                    continue  # The feed can arrive after the M1 decision.
            if reason:
                _exit_transition(c,oid,"EXIT_BLOCKED",current,reason)
                continue
            aid=hashlib.sha256((oid+"|frozen|"+str(row["signal_time"])).encode()).hexdigest()[:24]
            payload=dict(ticker=order["contract"],action="exit",orderType="market",
                         quantity=position["quantity"],
                         time=pd.Timestamp(current,unit="ms",tz="UTC").isoformat(),rejectAfter=3,
                         extras=dict(v3ActionId=aid,v3OrderId=oid,v3PositionId=position["position_id"],
                                     v3Policy="V3_FAILURE_EXIT_LIBRARY_V1",v3PrimaryReason=row["primary_reason"]))
            inserted=c.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?)",
                               (aid,oid,row["signal_time"],"EXIT_UNKNOWN",dump(payload),current)).rowcount
            if not inserted:
                _exit_transition(c,oid,"EXIT_BLOCKED",current,"existing_exit_action_no_retry")
                continue
            c.execute("UPDATE exit_trades SET requested_time=?,broker_request_id=?,updated_ms=? WHERE trade_id=?",(current,aid,current,oid))
            _exit_transition(c,oid,"EXIT_REQUESTED",current)
            c.execute("UPDATE orders SET state='EXIT_UNKNOWN',updated_ms=? WHERE order_id=? AND state='OPEN'",(current,oid))
            tasks.append((aid,oid,payload))
    for aid,oid,payload in tasks:
        status="EXIT_REQUESTED"; order_state="EXIT_UNKNOWN"; response=None; error="broker_response_unknown_no_retry"
        try:
            r=_send_broker_exit(payload)
            body=r.json()
            response={"http_status":int(r.status_code),"success":body.get("success"),
                      "broker_order_id":body.get("orderId") or body.get("order_id") or body.get("id"),
                      "body":body}
            if 200 <= r.status_code < 300 and body.get("success") is True:
                status="EXIT_ACKNOWLEDGED";order_state="EXIT_PENDING";error=None
            elif 400 <= r.status_code < 500:
                status="EXIT_REJECTED";order_state="EXIT_REJECTED";error="broker_rejected_close"
        except Exception:
            pass
        with connect() as c:
            c.execute("UPDATE actions SET state=?,updated_ms=? WHERE action_id=?",(order_state,now_ms(),aid))
            c.execute("UPDATE orders SET state=?,updated_ms=? WHERE order_id=? AND state='EXIT_UNKNOWN'",(order_state,now_ms(),oid))
            c.execute("UPDATE exit_trades SET broker_order_id=?,broker_response_json=?,broker_ack_time=?,error_reason=CASE WHEN status='EXIT_REQUESTED' THEN ? ELSE error_reason END,updated_ms=? WHERE trade_id=?",
                      (response.get("broker_order_id") if response else None,dump(response) if response else None,
                       now_ms() if status=="EXIT_ACKNOWLEDGED" else None,error,now_ms(),oid))
            still=c.execute("SELECT status FROM exit_trades WHERE trade_id=?",(oid,)).fetchone()
            if still and still[0] == "EXIT_REQUESTED":
                _exit_transition(c,oid,status,now_ms(),error)


def dispatch_exits():
    """Only after >=1s and a newer matching exclusive broker snapshot.

    One durable action per order. A crash/timeout is UNKNOWN, never retried.
    TradersPost action exit cannot open a reverse position. SENT != CLOSED.
    """
    # The previous MTF manager is retained for audit only.  A deployment must
    # opt in explicitly to its old executor; the frozen engine has its own gate.
    if "V3_EXIT_MODE" in os.environ or os.environ.get("AB_V3_LEGACY_EXIT_ENABLED", "false").lower() != "true": return
    if setting_blockers(): return
    current = now_ms()
    tasks = []
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("SELECT o.*,d.decision_ms,d.payload_json FROM orders o JOIN decisions d USING(order_id) WHERE o.state='OPEN' AND d.decision_ms=?", (current//60000*60000,)).fetchall()
        for row in rows:
            v = json.loads(row["payload_json"])
            p = json.loads(row["position_json"])
            o = json.loads(row["order_json"])
            if o['route_id'] != route() or o['contract'] != contract() or o['account_label'] != os.environ.get('ACCOUNT_LABEL','account'): continue
            if not v.get("exit") or not 1000 <= current-row["decision_ms"] <= 10000: continue
            if not row["decision_ms"]+1000 <= p["event_ms"] <= current or current-p["event_ms"] > 2000: continue
            if not p.get("exclusive") or p["quantity"] != o.get("quantity"): continue
            s = c.execute("SELECT MAX(ts_ms) FROM bars WHERE tf='1s' AND contract=?", (o["contract"],)).fetchone()[0]
            if s is None or current-s > 6000: continue
            aid = hashlib.sha256((row["order_id"]+"|"+str(row["decision_ms"])).encode()).hexdigest()[:24]
            payload = dict(ticker=o["contract"],action="exit",orderType="market",quantity=p["quantity"],
                time=pd.Timestamp(current,unit="ms",tz="UTC").isoformat(),rejectAfter=3,
                extras=dict(v3ActionId=aid,v3OrderId=row["order_id"],v3Policy=policy.POLICY))
            inserted = c.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?)", (aid,row["order_id"],row["decision_ms"],"EXIT_UNKNOWN",dump(payload),current)).rowcount
            if inserted:
                c.execute("UPDATE orders SET state='EXIT_UNKNOWN',updated_ms=? WHERE order_id=?", (current,row["order_id"]))
                tasks.append((aid,row["order_id"],payload))
    for aid,oid,payload in tasks:
        state = "EXIT_UNKNOWN"
        try:
            r = _send_broker_exit(payload)
            body = r.json()
            if 200 <= r.status_code < 300 and body.get("success") is True: state = "EXIT_PENDING"
            elif 400 <= r.status_code < 500: state = "EXIT_REJECTED"
        except Exception:
            pass  # No URL, token or external response body in public diagnostics.
        with connect() as c:
            c.execute("UPDATE actions SET state=?,updated_ms=? WHERE action_id=?", (state,now_ms(),aid))
            c.execute("UPDATE orders SET state=? WHERE order_id=? AND state='EXIT_UNKNOWN'", (state,oid))


def status():
    current = now_ms()
    with connect() as c:
        feeds = []
        for tf,duration in (("1s",1000),("M1",60000)):
            r = c.execute("SELECT MAX(ts_ms),COUNT(*) FROM bars WHERE tf=? AND contract=?", (tf,contract())).fetchone()
            age = None if r[0] is None else (current-r[0]-duration)/1000
            feeds.append(dict(tf=tf,rows=r[1],last_start_ms=r[0],age_seconds=age,state="MISSING" if age is None else "FRESH" if age <= (5 if tf=="1s" else 15) else "STALE"))
        orders = []
        for r in c.execute("SELECT * FROM orders ORDER BY updated_ms DESC LIMIT 200"):
            o = json.loads(r["order_json"])
            p = json.loads(r["position_json"]) if r["position_json"] else None
            latest = c.execute("SELECT payload_json FROM decisions WHERE order_id=? ORDER BY decision_ms DESC LIMIT 1",(r["order_id"],)).fetchone()
            orders.append(dict(o,state=r["state"],broker_position=p,last_decision=json.loads(latest[0]) if latest else None))
        actions = [dict(r) for r in c.execute("SELECT action_id,order_id,decision_ms,state,updated_ms FROM actions ORDER BY updated_ms DESC LIMIT 100")]
        logs = [dict(order_id=r["order_id"],**json.loads(r["payload_json"])) for r in c.execute("SELECT * FROM decisions ORDER BY decision_ms DESC LIMIT 200")]
    blockers = setting_blockers()
    if not orders: blockers.append("no_broker_confirmed_V3_positions")
    blockers += [f["tf"]+":"+f["state"] for f in feeds if f["state"] != "FRESH"]
    return dict(policy=policy.POLICY,mode=mode(),contract=contract() or None,route_id=route(),
                account_label=os.environ.get("ACCOUNT_LABEL","account"),blockers=blockers,
                feeds=feeds,orders=orders,actions=actions,decisions=logs,
                exits=exit_status(),
                execution_note="HTTP acceptance is not broker fill; CLOSED only from authenticated broker bridge.")


def _session(ms):
    t=pd.Timestamp(int(ms),unit="ms",tz="UTC").tz_convert("America/New_York")
    m=t.hour*60+t.minute
    if m>=1080 or m<120:return "ASIA"
    if m<300:return "LO"
    if m<570:return "PREM"
    if m<660:return "NYAM"
    if m<810:return "NYL"
    if m<960:return "NYPM"
    return "PM_AH"


def _metrics(rows):
    pnl=[float(x["exit_pnl"]) for x in rows if x["exit_pnl"] is not None]
    wins=sum(x>0 for x in pnl)
    loss=-sum(x for x in pnl if x<0)
    gain=sum(x for x in pnl if x>0)
    equity=high=dd=0.0
    for x in pnl:
        equity+=x;high=max(high,equity);dd=max(dd,high-equity)
    exits=[x for x in rows if x["primary_reason"]]
    rr=[x["exit_r"] for x in exits if x["exit_r"] is not None]
    return dict(trades=len(rows),confirmed_pnl_trades=len(pnl),early_exits=len(exits),
                exit_rate=len(exits)/len(rows) if rows else None,
                win_rate=wins/len(pnl) if pnl else None,
                pf=gain/loss if loss else None,net_pnl=sum(pnl) if pnl else None,
                expectancy=sum(pnl)/len(pnl) if pnl else None,max_dd=dd if pnl else None,
                avg_exit_r=sum(rr)/len(rr) if rr else None)


def exit_status():
    with connect() as c:
        records=[]
        for row in c.execute("SELECT e.*,o.order_json,o.position_json,o.state AS broker_state FROM exit_trades e JOIN orders o ON o.order_id=e.trade_id ORDER BY e.updated_ms DESC"):
            r=dict(row)
            order=json.loads(r.pop("order_json"));position=json.loads(r.pop("position_json")) if r["position_json"] else {}
            r.pop("position_json",None)
            research=json.loads(r.pop("research_json") or "{}")
            r["research"]=research
            r.pop("broker_response_json",None)
            r["triggered_managers"]=json.loads(r["triggered_managers"] or "[]")
            r["triggered_reasons"]=json.loads(r["triggered_reasons"] or "[]")
            r.update(direction=order["direction"],entry=position.get("entry_price",order["entry_price"]),
                     original_sl=order["stop_price"],original_tp=order["target_price"],
                     quantity=order.get("quantity"),activation_ms=order["activation_ms"],
                     session=_session(order["activation_ms"]),
                     baseline_outcome="UNKNOWN",delta_vs_baseline=None)
            records.append(r)
    real_closed=sorted((x for x in records if x["status"]=="CLOSED" and x["exit_mode"]=="real"),
                       key=lambda x:(x["fill_time"] or 0,x["trade_id"]))
    reason_stats=[]
    for reason in frozen_exit.REASONS:
        subset=[x for x in real_closed if x["primary_reason"]==reason]
        pnl=[float(x["exit_pnl"]) for x in subset if x["exit_pnl"] is not None]
        rr=[float(x["exit_r"]) for x in subset if x["exit_r"] is not None]
        reason_stats.append(dict(reason=reason,exits=len(subset),winners=sum(x>0 for x in pnl),
                                 losers=sum(x<0 for x in pnl),avg_exit_r=sum(rr)/len(rr) if rr else None,
                                 avg_pnl=sum(pnl)/len(pnl) if pnl else None,
                                 pf_contribution=sum(x for x in pnl if x>0)/(-sum(x for x in pnl if x<0))
                                 if any(x<0 for x in pnl) else None,
                                 explanation=frozen_exit.EXPLANATIONS[reason]))
    contributions={m:sum(m in x["triggered_managers"] for x in real_closed) for m in ("M1","M2","M3")}
    overlaps={}
    for x in real_closed:
        label="+".join(x["triggered_managers"]) or "NONE"
        overlaps[label]=overlaps.get(label,0)+1
    cuts={}
    for field,fn in (("month",lambda x:pd.Timestamp(x["fill_time"],unit="ms",tz="UTC").strftime("%Y-%m")),
                     ("year",lambda x:pd.Timestamp(x["fill_time"],unit="ms",tz="UTC").strftime("%Y")),
                     ("side",lambda x:x["direction"]),("session",lambda x:x["session"])):
        groups={}
        for x in real_closed:
            if x["fill_time"] is not None: groups.setdefault(fn(x),[]).append(x)
        cuts[field]=[dict(bucket=k,**_metrics(v)) for k,v in sorted(groups.items())]
    return dict(mode=exit_mode(),real_execution=os.environ.get("V3_EXIT_REAL_EXECUTION","false").lower()=="true",
                enabled=exit_enabled(),blockers=exit_blockers(),historical_parity=frozen_parity_ok(),
                title="PRODUCTION V3 + LIVE EXIT ENGINE · Model 0 research benchmark",
                runner_status="RESEARCH_ONLY",
                open=[x for x in records if x["broker_state"] not in ("CLOSED","CANCELLED")],
                completed=[x for x in records if x["status"]=="CLOSED"],
                metrics=_metrics(real_closed),reason_stats=reason_stats,
                manager_contribution=contributions,overlap=overlaps,cuts=cuts)


def _tick():
    if not _LOCK.acquire(blocking=False): return
    try:
        monitor()
        monitor_frozen()
        dispatch_frozen_exits()
        dispatch_exits()
    except Exception as exc:
        with connect() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES('monitor_error',?)", (type(exc).__name__,))
    finally:
        _LOCK.release()


def register(app, m1_callback=None, route_callback=None):
    from flask import Response, jsonify, request
    global _M1_CALLBACK, _ROUTE_CALLBACK
    _M1_CALLBACK, _ROUTE_CALLBACK = m1_callback, route_callback

    def authenticated(variable, header, body, allow_body=False):
        wanted = os.environ.get(variable, "")
        supplied = request.headers.get(header, "") or (str(body.get("feed_token", "")) if allow_body else "")
        return bool(wanted and supplied and hmac.compare_digest(wanted,supplied))

    def feed():
        body = request.get_json(silent=True) or {}
        if not authenticated("AB_V3_FEED_TOKEN","X-V3-Feed-Token",body,True): return jsonify(error="unauthorized"),401
        try: result = ingest(body)
        except (ValueError,KeyError,TypeError,OverflowError): return jsonify(error="invalid feed payload or conflicting bar"),400
        threading.Thread(target=_tick,daemon=True).start()
        return jsonify(result)

    def broker():
        body = request.get_json(silent=True) or {}
        if not authenticated("AB_V3_BROKER_TOKEN","X-V3-Broker-Token",body): return jsonify(error="unauthorized"),401
        try: result = broker_event(body)
        except (ValueError,KeyError,TypeError,OverflowError): return jsonify(error="invalid broker event or ownership mismatch"),400
        threading.Thread(target=_tick,daemon=True).start()
        return jsonify(result)

    def data():
        value = status()
        try:
            import continuation_shadow as shadow
            shadow._init_db()
            with shadow._connect() as c:
                row = c.execute("SELECT value FROM continuation_meta WHERE key='v3_market'").fetchone()
                value["market"] = json.loads(row[0]) if row else None
                value["candidates"] = [json.loads(r[0]) for r in c.execute("SELECT payload_json FROM continuation_candidates WHERE strategy='AB_DIRECTIONAL' OR event_kind='CLOSE_THROUGH' ORDER BY decision_ms DESC LIMIT 120")]
        except Exception:
            value.update(market=None,candidates=[])
        response = jsonify(value)
        response.headers["Cache-Control"] = "no-store"
        return response

    def page():
        return Response((HERE/"templates/ab_v3.html").read_text(encoding="utf-8"),mimetype="text/html")

    app.add_url_rule("/ab/v3","ab_v3_page",page)
    app.add_url_rule("/ab/v3/data","ab_v3_data",data)
    app.add_url_rule("/ab/v3/feed","ab_v3_feed",feed,methods=["POST"])
    app.add_url_rule("/ab/v3/broker","ab_v3_broker",broker,methods=["POST"])
