#!/usr/bin/env python3
"""Broker-inert forward shadow for MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1.

The detector and order geometry are delegated to the immutable outcome-free
freeze.  This module owns only forward observation state, simulated orders,
simulated Policy-B exits, and read-only dashboards.  It intentionally contains
no webhook, broker, Guard, or production-Reversal integration.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Response, jsonify, request


IDENTITY = "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1"
SCHEMA = "CONTINUATION_POLICY_B_SHADOW_V1"
TICK = 0.25
POINT_VALUE = 2.0
ROUND_TRIP_COST_USD = 3.50
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(HERE)))
DB_PATH = Path(os.environ.get("CONTINUATION_DB", str(DATA_DIR / "continuation_shadow.sqlite3")))
ARCHIVE_PATH = Path(os.environ.get("CONTINUATION_HISTORY_CSV", str(DATA_DIR / "archive.csv")))
INSTRUMENT_ID = int(os.environ.get("CONTINUATION_INSTRUMENT_ID", "1"))
MAX_HISTORY_DAYS = int(os.environ.get("CONTINUATION_MAX_HISTORY_DAYS", "0"))
ENABLED = os.environ.get("CONTINUATION_SHADOW_ENABLED", "1") == "1"

_LOCK = threading.RLock()
_WORKER_LOCK = threading.Lock()
_WORKER_RUNNING = False
_RESCAN_REQUESTED = False
_LAST = {
    "status": "starting" if ENABLED else "disabled",
    "last_bar": None,
    "last_scan_started": None,
    "last_scan_completed": None,
    "last_error": None,
    "rows_scanned": 0,
    "detector_outputs": 0,
    "worker_running": False,
}


def _utc(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value).tz_localize("UTC")


def _iso(value: Any) -> str | None:
    if value is None or value is pd.NaT:
        return None
    try:
        return _utc(value).isoformat()
    except Exception:
        return str(value)


def _safe(value: Any) -> Any:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _init_db() -> None:
    with _connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS continuation_meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS continuation_candidates (
              candidate_id TEXT PRIMARY KEY,
              event_kind TEXT NOT NULL,
              decision_ms INTEGER NOT NULL,
              trading_day TEXT,
              stage TEXT NOT NULL,
              status TEXT NOT NULL,
              rejection_reason TEXT,
              eligible INTEGER NOT NULL DEFAULT 0,
              forward_eligible INTEGER NOT NULL DEFAULT 0,
              bsl_name TEXT,
              entry_price REAL,
              stop_price REAL,
              target_price REAL,
              dol_id TEXT,
              payload_json TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cont_candidates_time
              ON continuation_candidates(decision_ms DESC);
            CREATE INDEX IF NOT EXISTS idx_cont_candidates_status
              ON continuation_candidates(status, rejection_reason);
            CREATE TABLE IF NOT EXISTS continuation_orders (
              order_id TEXT PRIMARY KEY,
              candidate_id TEXT NOT NULL,
              instrument_id INTEGER NOT NULL,
              activation_ms INTEGER NOT NULL,
              expiry_ms INTEGER NOT NULL,
              entry_price REAL NOT NULL,
              stop_price REAL NOT NULL,
              target_price REAL NOT NULL,
              risk_points REAL NOT NULL,
              dol_id TEXT NOT NULL,
              state TEXT NOT NULL,
              fill_ms INTEGER,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(candidate_id) REFERENCES continuation_candidates(candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cont_orders_state ON continuation_orders(state);
            CREATE TABLE IF NOT EXISTS continuation_trades (
              trade_id TEXT PRIMARY KEY,
              order_id TEXT UNIQUE NOT NULL,
              candidate_id TEXT NOT NULL,
              instrument_id INTEGER NOT NULL,
              fill_ms INTEGER NOT NULL,
              entry_price REAL NOT NULL,
              stop_price REAL NOT NULL,
              target_price REAL NOT NULL,
              risk_points REAL NOT NULL,
              state TEXT NOT NULL,
              exit_ms INTEGER,
              exit_price REAL,
              exit_reason TEXT,
              raw_r REAL,
              cost_r REAL,
              net_r REAL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(order_id) REFERENCES continuation_orders(order_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cont_trades_state ON continuation_trades(state);
            """
        )


def _meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM continuation_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO continuation_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _load_history() -> pd.DataFrame:
    source = ARCHIVE_PATH
    if not source.exists() and source.with_name("buffer.csv").exists():
        source = source.with_name("buffer.csv")
    if not source.exists():
        raise FileNotFoundError(f"history not found: {ARCHIVE_PATH}")
    use = ["ts_event", "open", "high", "low", "close", "volume"]
    head = pd.read_csv(source, nrows=0).columns.tolist()
    if "instrument_id" in head:
        use.append("instrument_id")
    raw = pd.read_csv(source, usecols=use)
    raw["ts_event"] = pd.to_datetime(raw.ts_event, utc=True, format="ISO8601", errors="coerce")
    raw = raw.dropna(subset=["ts_event", "open", "high", "low", "close"])
    if "instrument_id" not in raw:
        raw["instrument_id"] = INSTRUMENT_ID
    raw["instrument_id"] = raw.instrument_id.fillna(INSTRUMENT_ID).astype("int64")
    raw = raw.sort_values("ts_event", kind="stable").drop_duplicates("ts_event", keep="last")
    if MAX_HISTORY_DAYS > 0 and len(raw):
        raw = raw[raw.ts_event >= raw.ts_event.iloc[-1] - pd.Timedelta(days=MAX_HISTORY_DAYS)]
    return raw.reset_index(drop=True)


def _freeze_module():
    from MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE.source import freeze_baseline
    return freeze_baseline


def _verify_freeze() -> dict[str, Any]:
    root = HERE / "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE"
    manifest_path = root / "SHA256_MANIFEST.json"
    root_path = root / "FREEZE.sha256"
    if not manifest_path.exists() or not root_path.exists():
        raise RuntimeError("immutable Continuation freeze is unavailable")
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    expected = root_path.read_text(encoding="utf-8").split()[0]
    if digest != expected:
        raise RuntimeError("Continuation freeze root hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = []
    for name, wanted in manifest.items():
        path = root / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
            bad.append(name)
    if bad:
        raise RuntimeError("Continuation freeze member mismatch: " + ", ".join(bad[:5]))
    baseline = json.loads((root / "BASELINE_CONFIGURATION.json").read_text(encoding="utf-8"))
    return {
        "freeze_root_sha256": expected,
        "configuration_path": baseline["selected_generic_configuration"]["path"],
        "configuration_sha256": baseline["selected_generic_configuration"]["sha256"],
        "effective_detector_environment": baseline["effective_detector_environment"],
    }


def _ms(value: Any) -> int:
    return int(_utc(value).timestamp() * 1000)


def _candidate_state(row: dict[str, Any]) -> tuple[str, str]:
    if not row.get("eligible"):
        return "REJECTED", "REJECTED"
    if row.get("estimated_fill"):
        return "ORDER", "FILLED_OR_PENDING_REPLAY"
    return "ORDER", "UNFILLED_OR_PENDING"


def _upsert_scan(raw: pd.DataFrame, outputs: list[dict], triggers: list[dict], candidates: list[dict], orders: list[dict], funnel: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    latest_ms = int(raw.ts_event.iloc[-1].timestamp() * 1000)
    output_sources = {
        (int(x.get("source_event", {}).get("trigger_ms", -1)), str(x.get("source_event", {}).get("bsl_name", "")))
        for x in outputs
    }
    with _connect() as con:
        armed = _meta(con, "armed_after_ms")
        if armed is None:
            # Some canonical rows emitted on the final warm-up candle carry an
            # activation timestamp on the next minute. Exclude those too; no
            # record discovered by the first historical scan may later be
            # promoted into a forward order.
            warmup_decisions = [int(x.get("entry_ms") or x.get("bos_ms") or latest_ms) for x in candidates]
            armed_ms = max([latest_ms, *warmup_decisions])
            _set_meta(con, "armed_after_ms", armed_ms)
            _set_meta(con, "warmup_completed_at", now)
        else:
            armed_ms = int(armed)

        for trigger in triggers:
            tms = int(trigger["trigger_ms"])
            name = str(trigger["bsl_name"])
            cid = "TRIGGER_" + hashlib.sha256(f"{trigger['epoch']}|{tms}|{name}|{trigger['bsl_price']}".encode()).hexdigest()[:20]
            emitted = (tms, name) in output_sources
            expired = latest_ms >= tms + 120 * 60_000
            stage = "CANONICAL_OUTPUT_EMITTED" if emitted else ("EXPIRED_NO_CANONICAL_CONFIRMATION" if expired else "TRACKING_CONFIRMATION")
            status = "SUPERSEDED" if emitted else ("REJECTED" if expired else "TRACKING")
            payload = dict(trigger, candidate_id=cid, stage=stage, status=status)
            con.execute(
                """INSERT INTO continuation_candidates
                (candidate_id,event_kind,decision_ms,trading_day,stage,status,rejection_reason,eligible,
                 forward_eligible,bsl_name,payload_json,first_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET stage=excluded.stage,status=excluded.status,
                  rejection_reason=excluded.rejection_reason,payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at""",
                (cid, "CLOSE_THROUGH", tms, None, stage, status,
                 "NO_CANONICAL_CONFIRMATION" if expired and not emitted else None, 0, 0, name,
                 _json(payload), now, now),
            )

        order_by_candidate = {str(x["candidate_id"]): x for x in orders}
        for row in candidates:
            cid = str(row["candidate_id"])
            decision_ms = int(row.get("entry_ms") or row.get("bos_ms"))
            stage, status = _candidate_state(row)
            forward = bool(row.get("eligible") and decision_ms > armed_ms)
            payload = dict(row, forward_eligible=forward, shadow_schema=SCHEMA)
            con.execute(
                """INSERT INTO continuation_candidates
                (candidate_id,event_kind,decision_ms,trading_day,stage,status,rejection_reason,eligible,
                 forward_eligible,bsl_name,entry_price,stop_price,target_price,dol_id,payload_json,first_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET stage=excluded.stage,status=excluded.status,
                  rejection_reason=excluded.rejection_reason,eligible=excluded.eligible,
                  forward_eligible=MAX(continuation_candidates.forward_eligible,excluded.forward_eligible),
                  entry_price=excluded.entry_price,stop_price=excluded.stop_price,target_price=excluded.target_price,
                  dol_id=excluded.dol_id,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (cid, "CANONICAL_OUTPUT", decision_ms, _iso(row.get("trading_day")), stage, status,
                 row.get("rejection_reason"), int(bool(row.get("eligible"))), int(forward),
                 row.get("source_event", {}).get("bsl_name"), row.get("final_entry"),
                 row.get("final_structural_sl"), row.get("policy_B_target"), row.get("dol_id"),
                 _json(payload), now, now),
            )
            order = order_by_candidate.get(cid)
            if not (forward and order):
                continue
            activation = _ms(order["activation_timestamp"])
            expiry = _ms(order["expiry_timestamp"])
            con.execute(
                """INSERT OR IGNORE INTO continuation_orders
                (order_id,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,stop_price,
                 target_price,risk_points,dol_id,state,fill_ms,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
                (order["order_id"], cid, int(order["instrument_id"]), activation, expiry,
                 float(order["entry_price"]), float(order["structural_sl_price"]),
                 float(order["policy_B_target"]), float(order["initial_risk_points"]),
                 str(order["dol_id"]), "PENDING", _json(order), now, now),
            )

        _set_meta(con, "last_funnel", _json(funnel))
        _set_meta(con, "last_bar_ms", latest_ms)
        _set_meta(con, "last_scan_completed", now)


def _bar_arrays(raw: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "ms": raw.ts_event.astype("int64").to_numpy() // 1_000_000,
        "iid": raw.instrument_id.to_numpy(np.int64),
        "open": raw.open.to_numpy(float),
        "high": raw.high.to_numpy(float),
        "low": raw.low.to_numpy(float),
        "close": raw.close.to_numpy(float),
    }


def _reconcile(raw: pd.DataFrame) -> None:
    """Advance only simulated orders/trades; fill bar has no bracket evaluation."""
    a = _bar_arrays(raw)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with _connect() as con:
        orders = con.execute("SELECT * FROM continuation_orders WHERE state='PENDING' ORDER BY activation_ms").fetchall()
        for order in orders:
            left = int(np.searchsorted(a["ms"], order["activation_ms"], side="left"))
            right = int(np.searchsorted(a["ms"], order["expiry_ms"], side="left"))
            right_seen = min(right, len(a["ms"]))
            hits = np.flatnonzero(
                (a["iid"][left:right_seen] == int(order["instrument_id"]))
                & (a["low"][left:right_seen] <= float(order["entry_price"]) - TICK)
            )
            if len(hits):
                i = left + int(hits[0]); fill_ms = int(a["ms"][i])
                con.execute("UPDATE continuation_orders SET state='FILLED',fill_ms=?,updated_at=? WHERE order_id=?",
                            (fill_ms, now, order["order_id"]))
                trade_id = "TRADE_" + hashlib.sha256(str(order["order_id"]).encode()).hexdigest()[:20]
                con.execute(
                    """INSERT OR IGNORE INTO continuation_trades
                    (trade_id,order_id,candidate_id,instrument_id,fill_ms,entry_price,stop_price,target_price,
                     risk_points,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                    (trade_id, order["order_id"], order["candidate_id"], int(order["instrument_id"]), fill_ms,
                     float(order["entry_price"]), float(order["stop_price"]), float(order["target_price"]),
                     float(order["risk_points"]), now, now),
                )
            elif len(a["ms"]) and int(a["ms"][-1]) >= int(order["expiry_ms"]):
                con.execute("UPDATE continuation_orders SET state='UNFILLED_EXPIRED',updated_at=? WHERE order_id=?",
                            (now, order["order_id"]))

        trades = con.execute("SELECT * FROM continuation_trades WHERE state='OPEN' ORDER BY fill_ms").fetchall()
        for trade in trades:
            fill_i = int(np.searchsorted(a["ms"], int(trade["fill_ms"]), side="left"))
            if fill_i >= len(a["ms"]):
                continue
            iid = int(trade["instrument_id"])
            same = np.flatnonzero(a["iid"][fill_i:] == iid)
            if not len(same):
                continue
            last_same = fill_i + int(same[-1])
            exit_i = None; exit_price = None; reason = None
            for j in range(fill_i + 1, last_same + 1):
                if a["low"][j] <= float(trade["stop_price"]):
                    exit_i, exit_price, reason = j, min(float(a["open"][j]), float(trade["stop_price"])), "STRUCTURAL_SL"
                    break
                if a["high"][j] >= float(trade["target_price"]):
                    exit_i, exit_price, reason = j, float(trade["target_price"]), "FROZEN_OPEN_DOL"
                    break
            # A later physical epoch proves the roll. The current live epoch remains open.
            if exit_i is None and last_same < len(a["ms"]) - 1:
                exit_i, exit_price, reason = last_same, float(a["close"][last_same]), "CONTRACT_ROLL_TERMINATION"
            if exit_i is None:
                continue
            risk = float(trade["risk_points"])
            raw_r = (float(exit_price) - float(trade["entry_price"])) / risk
            cost_r = ROUND_TRIP_COST_USD / (risk * POINT_VALUE)
            con.execute(
                """UPDATE continuation_trades SET state='CLOSED',exit_ms=?,exit_price=?,exit_reason=?,
                raw_r=?,cost_r=?,net_r=?,updated_at=? WHERE trade_id=?""",
                (int(a["ms"][exit_i]), float(exit_price), reason, raw_r, cost_r, raw_r - cost_r,
                 now, trade["trade_id"]),
            )


def scan_once() -> dict[str, Any]:
    """Run one deterministic causal scan through the immutable baseline source."""
    if not ENABLED:
        return {"status": "disabled"}
    _init_db()
    _LAST.update(status="scanning", last_scan_started=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), last_error=None)
    provenance = _verify_freeze()
    raw = _load_history()
    if raw.empty:
        raise RuntimeError("history contains no valid bars")
    freeze = _freeze_module()
    theses = freeze.jade_theses(raw)
    outputs, triggers, detector_meta = freeze.generate_detector(raw)
    freeze.detector_meta = detector_meta
    candidates, orders, funnel = freeze.build_manifests(raw, outputs, triggers, theses)
    _upsert_scan(raw, outputs, triggers, candidates, orders, funnel)
    current_day = freeze.trading_day_at(int(raw.ts_event.iloc[-1].timestamp() * 1000))
    current_thesis = theses.get(current_day, {"thesis": "NONE", "reason": "missing_day"})
    with _connect() as con:
        _set_meta(con, "current_thesis", _json(current_thesis))
        _set_meta(con, "last_close", float(raw.close.iloc[-1]))
    _reconcile(raw)
    completed = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _LAST.update(status="ok", last_bar=raw.ts_event.iloc[-1].isoformat(), last_scan_completed=completed,
                 rows_scanned=len(raw), detector_outputs=len(outputs), worker_running=False)
    return {"status": "ok", "funnel": funnel, "rows": len(raw), "last_bar": _LAST["last_bar"], "provenance": provenance}


def _worker() -> None:
    global _WORKER_RUNNING, _RESCAN_REQUESTED
    while True:
        try:
            scan_once()
        except Exception as exc:
            _LAST.update(status="error", last_error=f"{type(exc).__name__}: {exc}", worker_running=False)
            print("[continuation-shadow] scan error", repr(exc), flush=True)
        with _WORKER_LOCK:
            if _RESCAN_REQUESTED:
                _RESCAN_REQUESTED = False
                continue
            _WORKER_RUNNING = False
            _LAST["worker_running"] = False
            return


def notify_bar(bar: dict[str, Any] | None = None) -> dict[str, Any]:
    """Coalesce scan requests. This function never waits for detector work."""
    global _WORKER_RUNNING, _RESCAN_REQUESTED
    if bar and bar.get("ts_event"):
        _LAST["last_bar"] = str(bar["ts_event"])
    if not ENABLED:
        return {"scheduled": False, "reason": "disabled"}
    with _WORKER_LOCK:
        if _WORKER_RUNNING:
            _RESCAN_REQUESTED = True
            return {"scheduled": False, "coalesced": True}
        _WORKER_RUNNING = True
        _LAST["worker_running"] = True
        threading.Thread(target=_worker, name="continuation-shadow", daemon=True).start()
    return {"scheduled": True}


def on_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return notify_bar(bar)


def _pf(values: list[float]) -> float | None:
    wins = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    return None if losses == 0 else wins / losses


def _max_dd(values: list[float]) -> float:
    equity = np.r_[0.0, np.cumsum(values)]
    return float(np.min(equity - np.maximum.accumulate(equity)))


def _summary() -> dict[str, Any]:
    _init_db()
    with _connect() as con:
        counts = {row["status"]: row["n"] for row in con.execute(
            "SELECT status,COUNT(*) n FROM continuation_candidates GROUP BY status").fetchall()}
        stages = {row["stage"]: row["n"] for row in con.execute(
            "SELECT stage,COUNT(*) n FROM continuation_candidates GROUP BY stage").fetchall()}
        orders = {row["state"]: row["n"] for row in con.execute(
            "SELECT state,COUNT(*) n FROM continuation_orders GROUP BY state").fetchall()}
        trades = [dict(x) for x in con.execute("SELECT * FROM continuation_trades ORDER BY fill_ms").fetchall()]
        closed = [x for x in trades if x["state"] == "CLOSED" and x["net_r"] is not None]
        net = [float(x["net_r"]) for x in closed]
        funnel_raw = _meta(con, "last_funnel", "{}") or "{}"
        armed = _meta(con, "armed_after_ms")
        thesis_raw = _meta(con, "current_thesis", "{}") or "{}"
        last_close = float(_meta(con, "last_close", "nan") or "nan")
        open_positions = []
        for x in trades:
            if x["state"] != "OPEN":
                continue
            position = dict(x)
            if math.isfinite(last_close):
                position["mark_price"] = last_close
                position["unrealized_raw_r"] = (last_close - float(x["entry_price"])) / float(x["risk_points"])
                position["unrealized_net_r_after_full_round_trip_cost"] = (
                    position["unrealized_raw_r"] - ROUND_TRIP_COST_USD / (float(x["risk_points"]) * POINT_VALUE)
                )
            open_positions.append(position)
        metrics = {
            "closed_trades": len(closed), "open_trades": sum(x["state"] == "OPEN" for x in trades),
            "winners": sum(x > 0 for x in net), "losers": sum(x < 0 for x in net),
            "flat": sum(x == 0 for x in net),
            "win_rate_pct": (100.0 * sum(x > 0 for x in net) / len(net)) if net else None,
            "pf_after_cost": _pf(net), "net_r_after_cost": sum(net),
            "avg_r_after_cost": float(np.mean(net)) if net else None,
            "max_drawdown_r_after_cost": _max_dd(net) if net else None,
        }
        return {
            "identity": IDENTITY, "schema": SCHEMA, "broker_inert": True,
            "policy": "B_UNMANAGED_FROZEN_OPEN_DOL", "round_trip_cost_usd": ROUND_TRIP_COST_USD,
            "point_value_usd": POINT_VALUE, "tick_points": TICK,
            "historical_development_reference_only": {
                "fills": 126, "win_rate_pct": 30.95, "pf_after_cost": 1.7045,
                "net_r_after_cost": 66.9784, "not_live_expectation": True,
            },
            "engine": dict(_LAST), "armed_after": None if armed is None else _iso(pd.Timestamp(int(armed), unit="ms", tz="UTC")),
            "collection_started_at": None if armed is None else _iso(pd.Timestamp(int(armed), unit="ms", tz="UTC")),
            "history_path": str(ARCHIVE_PATH), "physical_contract_ids_available": "instrument_id" in (pd.read_csv(ARCHIVE_PATH, nrows=0).columns if ARCHIVE_PATH.exists() else []),
            "candidate_counts": counts, "stage_counts": stages, "order_counts": orders,
            "funnel": json.loads(funnel_raw), "metrics": metrics,
            "current_thesis": json.loads(thesis_raw),
            "latest_thesis": json.loads(thesis_raw).get("thesis"),
            "open_positions": open_positions,
        }


def _candidate_rows() -> list[dict[str, Any]]:
    clauses, args = [], []
    status = request.args.get("status", "").strip().upper()
    reason = request.args.get("reason", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if status:
        clauses.append("status=?"); args.append(status)
    if reason:
        clauses.append("COALESCE(rejection_reason,'')=?"); args.append(reason)
    if start:
        clauses.append("decision_ms>=?"); args.append(_ms(start))
    if end:
        clauses.append("decision_ms<?"); args.append(_ms(end) + 86_400_000)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = min(max(int(request.args.get("limit", "500")), 1), 2000)
    with _connect() as con:
        rows = con.execute("SELECT * FROM continuation_candidates" + where + " ORDER BY decision_ms DESC LIMIT ?", (*args, limit)).fetchall()
    result = []
    for x in rows:
        row = dict(x); row["payload"] = json.loads(row.pop("payload_json")); row["decision_at"] = _iso(pd.Timestamp(row["decision_ms"], unit="ms", tz="UTC"))
        result.append(row)
    return result


def _bars_for(candidate: sqlite3.Row, before: int = 45, after: int = 90) -> list[dict[str, Any]]:
    raw = _load_history()
    ms = raw.ts_event.astype("int64").to_numpy() // 1_000_000
    center = int(np.searchsorted(ms, int(candidate["decision_ms"]), side="left"))
    frame = raw.iloc[max(0, center - before):min(len(raw), center + after + 1)]
    return [{"ts": x.ts_event.isoformat(), "open": float(x.open), "high": float(x.high),
             "low": float(x.low), "close": float(x.close), "volume": float(x.volume)}
            for x in frame.itertuples()]


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Continuation Shadow</title><style>
:root{--bg:#08111f;--panel:#101d31;--line:#263952;--text:#e5edf8;--mut:#8ca0ba;--cyan:#2dd4bf;--red:#fb7185;--amber:#fbbf24;--blue:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,system-ui,sans-serif}.wrap{max-width:1500px;margin:auto;padding:24px}h1{font-size:22px;margin:0 0 4px}a{color:var(--blue)}.mut{color:var(--mut)}.bar,.grid{display:grid;gap:12px}.bar{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:20px 0}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.big{font-size:24px;font-weight:700;margin-top:6px}.ok{color:var(--cyan)}.bad{color:var(--red)}.warn{color:var(--amber)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mut);position:sticky;top:0;background:var(--panel)}.scroll{overflow:auto;max-height:68vh}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}input,select,button{background:#0b1728;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px}button{cursor:pointer}.pill{padding:3px 7px;border-radius:999px;background:#1b2c45}.detail{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:12px}svg{width:100%;height:auto;background:#08111f;border-radius:9px}pre{white-space:pre-wrap;word-break:break-word;color:#c7d5e8}@media(max-width:900px){.detail{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><h1>MNQ Continuation · Policy B unmanaged</h1><div class="mut">MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1 · forward shadow only · no broker actions</div><div id="app"></div></div>
<script>
const $=s=>document.querySelector(s), fmt=(x,n=3)=>x==null?'—':Number(x).toFixed(n), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function j(u){let r=await fetch(u,{cache:'no-store'});if(!r.ok)throw Error(await r.text());return r.json()}
function cards(s){let m=s.metrics,e=s.engine,f=s.funnel||{},o=s.order_counts||{};return `<div class="bar"><div class="card">Engine / feed<div class="big ${e.status==='ok'?'ok':'warn'}">${esc(e.status)}</div><div class="mut">last bar ${esc(e.last_bar||'—')}</div></div><div class="card">Current HTF thesis<div class="big">${esc(s.latest_thesis||'brak danych')}</div><div class="mut">since ${esc(s.collection_started_at||'brak danych')}</div></div><div class="card">Candidates / rejected<div class="big">${f.canonical_outputs??0} / ${s.candidate_counts.REJECTED??0}</div><div class="mut">pending orders ${o.PENDING??0}</div></div><div class="card">Fills / closed<div class="big">${m.open_trades+m.closed_trades} / ${m.closed_trades}</div><div class="mut">W/L/flat ${m.winners}/${m.losers}/${m.flat}</div></div><div class="card">PF net<div class="big">${m.pf_after_cost==null?'brak danych':fmt(m.pf_after_cost)}</div><div class="mut">Net ${fmt(m.net_r_after_cost)}R · DD ${m.max_drawdown_r_after_cost==null?'brak danych':fmt(m.max_drawdown_r_after_cost)+'R'}</div></div></div>`}
async function dash(){let s=await j('/continuation/api/status');$('#app').innerHTML=cards(s)+`<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"><div class="card"><h3>Current funnel</h3><pre>${esc(JSON.stringify(s.funnel,null,2))}</pre></div><div class="card"><h3>State</h3><pre>${esc(JSON.stringify({candidates:s.candidate_counts,stages:s.stage_counts,orders:s.order_counts,armed_after:s.armed_after},null,2))}</pre></div><div class="card"><h3>Open positions</h3><pre>${esc(JSON.stringify(s.open_positions,null,2))}</pre></div><div class="card"><h3>Historical Development reference only</h3><pre>${esc(JSON.stringify(s.historical_development_reference_only,null,2))}</pre><p class="mut">Comparison point only; not a live expectation.</p></div><div class="card"><h3>Execution contract</h3><p>Final target is the causal OPEN bullish DOL frozen at decision. Structural SL, one-tick-through fill, 10-minute expiry, no manager, fill-bar brackets disabled, then adverse-first.</p><p>Cost: $3.50 / (risk points × $2.00). Physical contract IDs: <b>${s.physical_contract_ids_available?'available':'not supplied by live archive'}</b>.</p><a href="/continuation/candidates">Open candidates →</a></div></div>`}
async function list(){let q=new URLSearchParams(location.search),rows=await j('/continuation/api/candidates?'+q);let reasons=[...new Set(rows.map(x=>x.rejection_reason).filter(Boolean))].sort();$('#app').innerHTML=`<div class="filters"><select id="status"><option value="">all status</option>${['TRACKING','REJECTED','SUPERSEDED','ORDER'].map(x=>`<option>${x}</option>`)}</select><select id="reason"><option value="">all reasons</option>${reasons.map(x=>`<option>${esc(x)}</option>`)}</select><input id="start" type="date"><input id="end" type="date"><button id="go">Filter</button><a href="/continuation">Dashboard</a></div><div class="card scroll"><table><thead><tr><th>Time</th><th>Stage</th><th>Status</th><th>BSL</th><th>Thesis</th><th>DOL</th><th>Entry</th><th>SL</th><th>Target</th><th>Reason</th></tr></thead><tbody>${rows.map(x=>`<tr><td><a href="/continuation/candidate/${encodeURIComponent(x.candidate_id)}">${esc(x.decision_at)}</a></td><td>${esc(x.stage)}</td><td><span class="pill">${esc(x.status)}</span></td><td>${esc(x.bsl_name)}</td><td>${esc(x.payload.jade_thesis)}</td><td>${esc(x.dol_id)}</td><td>${fmt(x.entry_price,2)}</td><td>${fmt(x.stop_price,2)}</td><td>${fmt(x.target_price,2)}</td><td>${esc(x.rejection_reason)}</td></tr>`).join('')}</tbody></table></div>`;for(let k of ['status','reason','start','end'])$('#'+k).value=q.get(k)||'';$('#go').onclick=()=>{let z=new URLSearchParams();for(let k of ['status','reason','start','end'])if($('#'+k).value)z.set(k,$('#'+k).value);location.search=z}}
function chart(b,d){if(!b.length)return '<div class="card">No bars available.</div>';let W=1000,H=520,pad=55,vals=b.flatMap(x=>[x.low,x.high]),marks=[d.entry_price,d.stop_price,d.target_price].filter(x=>x!=null),lo=Math.min(...vals,...marks),hi=Math.max(...vals,...marks),p=(hi-lo)*.06||1;lo-=p;hi+=p;let x=i=>pad+(i+.5)*(W-2*pad)/b.length,y=v=>pad+(hi-v)*(H-2*pad)/(hi-lo),bw=Math.max(1,Math.min(6,(W-2*pad)/b.length*.65)),s=`<svg viewBox="0 0 ${W} ${H}">`;for(let n=0;n<6;n++){let v=lo+n*(hi-lo)/5,yy=y(v);s+=`<line x1="${pad}" y1="${yy}" x2="${W-pad}" y2="${yy}" stroke="#263952"/><text x="4" y="${yy+4}" fill="#8ca0ba" font-size="12">${v.toFixed(2)}</text>`}b.forEach((c,i)=>{let col=c.close>=c.open?'#2dd4bf':'#fb7185',xx=x(i),top=Math.min(y(c.open),y(c.close)),bot=Math.max(y(c.open),y(c.close));s+=`<line x1="${xx}" y1="${y(c.high)}" x2="${xx}" y2="${y(c.low)}" stroke="${col}"/><rect x="${xx-bw/2}" y="${top}" width="${bw}" height="${Math.max(1,bot-top)}" fill="${col}"/>`});[['ENTRY',d.entry_price,'#60a5fa'],['SL',d.stop_price,'#fb7185'],['DOL',d.target_price,'#fbbf24']].forEach(z=>{if(z[1]!=null)s+=`<line x1="${pad}" y1="${y(z[1])}" x2="${W-pad}" y2="${y(z[1])}" stroke="${z[2]}" stroke-width="2" stroke-dasharray="7 5"/><text x="${W-pad+5}" y="${y(z[1])+4}" fill="${z[2]}">${z[0]}</text>`});return s+'</svg>'}
async function detail(){let id=decodeURIComponent(location.pathname.split('/').pop()),d=await j('/continuation/api/candidate/'+encodeURIComponent(id));$('#app').innerHTML=`<p><a href="/continuation/candidates">← Candidates</a></p><div class="detail"><div class="card">${chart(d.bars,d.candidate)}</div><div class="card"><h3>${esc(id)}</h3><p><b>${esc(d.candidate.stage)}</b> · ${esc(d.candidate.status)}</p><h4>Order / trade state</h4><pre>${esc(JSON.stringify({order:d.order,trade:d.trade},null,2))}</pre><h4>Frozen evidence</h4><pre>${esc(JSON.stringify(d.candidate.payload,null,2))}</pre></div></div>`}
(async()=>{try{if(location.pathname.includes('/candidate/'))await detail();else if(location.pathname.endsWith('/candidates'))await list();else await dash()}catch(e){$('#app').innerHTML='<div class="card bad">'+esc(e)+'</div>'}})();
</script></body></html>'''


def register(app, archive_path: str | os.PathLike[str] | None = None) -> None:
    global ARCHIVE_PATH
    if archive_path is not None:
        ARCHIVE_PATH = Path(archive_path)
    _init_db()

    @app.get("/continuation")
    @app.get("/continuation/candidates")
    @app.get("/continuation/candidate/<candidate_id>")
    def continuation_page(candidate_id: str | None = None):
        return Response(PAGE, mimetype="text/html")

    @app.get("/continuation/api/status")
    def continuation_status():
        return jsonify(_summary())

    @app.get("/continuation/api/candidates")
    def continuation_candidates():
        return jsonify(_candidate_rows())

    @app.get("/continuation/api/candidate/<candidate_id>")
    def continuation_candidate(candidate_id: str):
        with _connect() as con:
            row = con.execute("SELECT * FROM continuation_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            order = con.execute("SELECT * FROM continuation_orders WHERE candidate_id=?", (candidate_id,)).fetchone()
            trade = None if order is None else con.execute("SELECT * FROM continuation_trades WHERE order_id=?", (order["order_id"],)).fetchone()
        if row is None:
            return jsonify(error="candidate not found"), 404
        value = dict(row); value["payload"] = json.loads(value.pop("payload_json")); value["decision_at"] = _iso(pd.Timestamp(value["decision_ms"], unit="ms", tz="UTC"))
        return jsonify(candidate=value, order=None if order is None else dict(order),
                       trade=None if trade is None else dict(trade), bars=_bars_for(row))

    # Startup is warm-up only. The first successful scan arms strictly after
    # its last historical bar, preventing historical records from becoming
    # fake forward trades.
    notify_bar()


if __name__ == "__main__":
    _init_db()
    print(json.dumps(_safe(scan_once()), indent=2))
