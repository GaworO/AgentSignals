#!/usr/bin/env python3
"""Broker-inert LONG baseline and exploratory SHORT Continuation shadow.

LONG detector and order geometry are delegated to the immutable outcome-free
freeze; SHORT uses a separately identified source-locked research mirror.
This module owns forward observation state, simulated orders, unmanaged
Policy-B exits, and read-only dashboards. It has no webhook, broker, Guard,
or production-Reversal integration.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import html
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Response, jsonify, request


IDENTITY = "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1"
SCHEMA = "CONTINUATION_DIRECTIONAL_POLICY_B_SHADOW_V2"
TICK = 0.25
POINT_VALUE = 2.0
ROUND_TRIP_COST_USD = 3.50
SHORT_IDENTITY = "MNQ_CONTINUATION_HTF_CANONICAL_SHORT_RESEARCH_V1"
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
_SCAN_LISTENERS: list[Any] = []
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
              direction TEXT NOT NULL DEFAULT 'LONG',
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
              direction TEXT NOT NULL DEFAULT 'LONG',
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
              direction TEXT NOT NULL DEFAULT 'LONG',
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
        for table in ("continuation_candidates", "continuation_orders", "continuation_trades"):
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if "direction" not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN direction TEXT NOT NULL DEFAULT 'LONG'")


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


def _verify_freeze() -> dict[str, Any]:
    root = HERE / "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE"
    manifest_path = root / "SHA256_MANIFEST.json"
    root_path = root / "FREEZE.sha256"
    if not manifest_path.is_file() or not root_path.is_file():
        missing = [str(p.relative_to(HERE)) for p in (manifest_path, root_path) if not p.is_file()]
        raise RuntimeError("complete immutable Continuation freeze is unavailable; missing: " + ", ".join(missing))
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    expected = root_path.read_text(encoding="utf-8").split()[0]
    if digest != expected:
        raise RuntimeError("Continuation freeze root hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = []
    for name, wanted in manifest.items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
            bad.append(name)
    if bad:
        raise RuntimeError("Continuation freeze member mismatch: " + ", ".join(bad[:5]))
    source_hashes = json.loads((root / "SOURCE_HASHES.json").read_text(encoding="utf-8"))
    source_mismatch = []
    for name, wanted in source_hashes.items():
        if name.startswith("jadecap_research_20260921/data_dev/"):
            continue  # research input is intentionally not a runtime dependency
        path = (HERE / "continuation_runtime" / name) if name.startswith("detcore/") else (HERE / name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
            source_mismatch.append(name)
    if source_mismatch:
        raise RuntimeError("Continuation source missing/hash mismatch: " + ", ".join(source_mismatch[:5]))
    short_lock = json.loads((HERE / "CONTINUATION_SHORT_SOURCE_LOCK.json").read_text(encoding="utf-8"))
    short_source = HERE / short_lock["short_engine_path"]
    if (short_lock["reused_long_freeze_root_sha256"] != expected
            or not short_source.is_file()
            or hashlib.sha256(short_source.read_bytes()).hexdigest() != short_lock["short_engine_sha256"]):
        raise RuntimeError("SHORT research source lock mismatch")
    baseline = json.loads((root / "BASELINE_CONFIGURATION.json").read_text(encoding="utf-8"))
    return {
        "verification_mode": "complete_outcome_free_freeze",
        "freeze_root_sha256": expected,
        "configuration_path": baseline["selected_generic_configuration"]["path"],
        "configuration_sha256": baseline["selected_generic_configuration"]["sha256"],
        "effective_detector_environment": baseline["effective_detector_environment"],
        "short_research_identity": SHORT_IDENTITY,
        "short_engine_sha256": short_lock["short_engine_sha256"],
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
        (int(x.get("source_event", {}).get("trigger_ms", -1)),
         str(x.get("source_event", {}).get("bsl_name") or x.get("source_event", {}).get("ssl_name", "")))
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
            direction = str(trigger.get("direction", "LONG"))
            name = str(trigger.get("bsl_name") or trigger.get("ssl_name"))
            price = trigger.get("bsl_price", trigger.get("ssl_price"))
            identity = f"{trigger['epoch']}|{tms}|{name}|{price}"
            if direction != "LONG":
                identity = f"{direction}|{identity}"
            cid = "TRIGGER_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
            emitted = (tms, name) in output_sources
            expired = latest_ms >= tms + 120 * 60_000
            stage = "CANONICAL_OUTPUT_EMITTED" if emitted else ("EXPIRED_NO_CANONICAL_CONFIRMATION" if expired else "TRACKING_CONFIRMATION")
            status = "SUPERSEDED" if emitted else ("REJECTED" if expired else "TRACKING")
            payload = dict(trigger, candidate_id=cid, stage=stage, status=status)
            con.execute(
                """INSERT INTO continuation_candidates
                (candidate_id,direction,event_kind,decision_ms,trading_day,stage,status,rejection_reason,eligible,
                 forward_eligible,bsl_name,payload_json,first_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET stage=excluded.stage,status=excluded.status,
                  rejection_reason=excluded.rejection_reason,payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at""",
                (cid, direction, "CLOSE_THROUGH", tms, _iso(trigger.get("trading_day")), stage, status,
                 "NO_CANONICAL_CONFIRMATION" if expired and not emitted else None, 0, 0, name,
                 _json(payload), now, now),
            )

        order_by_candidate = {str(x["candidate_id"]): x for x in orders}
        for row in candidates:
            cid = str(row["candidate_id"])
            direction = str(row.get("dir", "LONG"))
            decision_ms = int(row.get("entry_ms") or row.get("bos_ms"))
            stage, status = _candidate_state(row)
            forward = bool(row.get("eligible") and decision_ms > armed_ms)
            payload = dict(row, forward_eligible=forward, shadow_schema=SCHEMA)
            con.execute(
                """INSERT INTO continuation_candidates
                (candidate_id,direction,event_kind,decision_ms,trading_day,stage,status,rejection_reason,eligible,
                 forward_eligible,bsl_name,entry_price,stop_price,target_price,dol_id,payload_json,first_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET stage=excluded.stage,status=excluded.status,
                  rejection_reason=excluded.rejection_reason,eligible=excluded.eligible,
                  forward_eligible=MAX(continuation_candidates.forward_eligible,excluded.forward_eligible),
                  entry_price=excluded.entry_price,stop_price=excluded.stop_price,target_price=excluded.target_price,
                  dol_id=excluded.dol_id,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (cid, direction, "CANONICAL_OUTPUT", decision_ms, _iso(row.get("trading_day")), stage, status,
                 row.get("rejection_reason"), int(bool(row.get("eligible"))), int(forward),
                 row.get("source_event", {}).get("bsl_name") or row.get("source_event", {}).get("ssl_name"), row.get("final_entry"),
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
                (order_id,direction,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,stop_price,
                 target_price,risk_points,dol_id,state,fill_ms,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
                (order["order_id"], direction, cid, int(order["instrument_id"]), activation, expiry,
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
            direction = str(order["direction"])
            left = int(np.searchsorted(a["ms"], order["activation_ms"], side="left"))
            right = int(np.searchsorted(a["ms"], order["expiry_ms"], side="left"))
            right_seen = min(right, len(a["ms"]))
            crossed = (a["low"][left:right_seen] <= float(order["entry_price"]) - TICK) if direction == "LONG" else (
                a["high"][left:right_seen] >= float(order["entry_price"]) + TICK)
            hits = np.flatnonzero((a["iid"][left:right_seen] == int(order["instrument_id"])) & crossed)
            if len(hits):
                i = left + int(hits[0]); fill_ms = int(a["ms"][i])
                con.execute("UPDATE continuation_orders SET state='FILLED',fill_ms=?,updated_at=? WHERE order_id=?",
                            (fill_ms, now, order["order_id"]))
                trade_id = "TRADE_" + hashlib.sha256(str(order["order_id"]).encode()).hexdigest()[:20]
                con.execute(
                    """INSERT OR IGNORE INTO continuation_trades
                    (trade_id,direction,order_id,candidate_id,instrument_id,fill_ms,entry_price,stop_price,target_price,
                     risk_points,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                    (trade_id, direction, order["order_id"], order["candidate_id"], int(order["instrument_id"]), fill_ms,
                     float(order["entry_price"]), float(order["stop_price"]), float(order["target_price"]),
                     float(order["risk_points"]), now, now),
                )
            elif len(a["ms"]) and int(a["ms"][-1]) >= int(order["expiry_ms"]):
                con.execute("UPDATE continuation_orders SET state='UNFILLED_EXPIRED',updated_at=? WHERE order_id=?",
                            (now, order["order_id"]))

        trades = con.execute("SELECT * FROM continuation_trades WHERE state='OPEN' ORDER BY fill_ms").fetchall()
        for trade in trades:
            direction = str(trade["direction"])
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
                if direction == "LONG":
                    if a["low"][j] <= float(trade["stop_price"]):
                        exit_i, exit_price, reason = j, min(float(a["open"][j]), float(trade["stop_price"])), "STRUCTURAL_SL"
                        break
                    if a["high"][j] >= float(trade["target_price"]):
                        exit_i, exit_price, reason = j, float(trade["target_price"]), "FROZEN_OPEN_DOL"
                        break
                else:
                    if a["high"][j] >= float(trade["stop_price"]):
                        exit_i, exit_price, reason = j, max(float(a["open"][j]), float(trade["stop_price"])), "STRUCTURAL_SL"
                        break
                    if a["low"][j] <= float(trade["target_price"]):
                        exit_i, exit_price, reason = j, float(trade["target_price"]), "FROZEN_OPEN_DOL"
                        break
            # A later physical epoch proves the roll. The current live epoch remains open.
            if exit_i is None and last_same < len(a["ms"]) - 1:
                exit_i, exit_price, reason = last_same, float(a["close"][last_same]), "CONTRACT_ROLL_TERMINATION"
            if exit_i is None:
                continue
            risk = float(trade["risk_points"])
            sign = 1 if direction == "LONG" else -1
            raw_r = sign * (float(exit_price) - float(trade["entry_price"])) / risk
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
    scan_env = os.environ.copy()
    scan_env["CONTINUATION_HISTORY_CSV"] = str(ARCHIVE_PATH)
    scan_env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(HERE / "continuation_scan_runtime.py"), "--scan"],
        cwd=str(HERE), env=scan_env, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("isolated Continuation scan failed: " + result.stderr[-3000:])
    scan = json.loads(result.stdout)
    raw = _load_history()
    outputs = scan["outputs"]
    triggers = scan["triggers"]
    candidates = scan["candidates"]
    orders = scan["orders"]
    outputs += scan["short_outputs"]
    triggers += scan["short_triggers"]
    candidates += scan["short_candidates"]
    orders += scan["short_orders"]
    funnel = {"LONG": scan["funnel"], "SHORT": scan["short_funnel"],
              "canonical_outputs": scan["funnel"]["canonical_outputs"] + scan["short_funnel"]["canonical_outputs"]}
    _upsert_scan(raw, outputs, triggers, candidates, orders, funnel)
    with _connect() as con:
        _set_meta(con, "current_thesis", _json(scan["current_thesis"]))
        _set_meta(con, "last_close", float(raw.close.iloc[-1]))
    _reconcile(raw)
    completed = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _LAST.update(status="ok", last_bar=raw.ts_event.iloc[-1].isoformat(), last_scan_completed=completed,
                 rows_scanned=len(raw), detector_outputs=len(outputs), worker_running=False)
    scan_result = {"status": "ok", "funnel": funnel, "rows": len(raw),
                   "last_bar": _LAST["last_bar"], "provenance": provenance}
    # Optional consumers run only after the deterministic shadow scan and DB
    # transaction have completed.  A listener failure can never corrupt or
    # downgrade the shadow ledger; live consumers must fail closed themselves.
    for listener in list(_SCAN_LISTENERS):
        try:
            listener(dict(scan_result))
        except Exception as exc:
            print("[continuation-shadow] post-scan listener error", repr(exc), flush=True)
    return scan_result


def register_scan_listener(listener: Any) -> None:
    """Register one in-process post-scan consumer, idempotently.

    The shadow remains broker-inert: it neither imports nor calls a broker.
    The application may register a separately gated consumer.
    """
    if not callable(listener):
        raise TypeError("scan listener must be callable")
    with _LOCK:
        if listener not in _SCAN_LISTENERS:
            _SCAN_LISTENERS.append(listener)


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
                sign = 1 if x["direction"] == "LONG" else -1
                position["unrealized_raw_r"] = sign * (last_close - float(x["entry_price"])) / float(x["risk_points"])
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
        by_direction = {}
        for direction in ("LONG", "SHORT"):
            side = [float(x["net_r"]) for x in closed if x["direction"] == direction]
            by_direction[direction] = {
                "closed_trades": len(side), "winners": sum(x > 0 for x in side),
                "losers": sum(x < 0 for x in side), "flat": sum(x == 0 for x in side),
                "win_rate_pct": 100 * sum(x > 0 for x in side) / len(side) if side else None,
                "pf_after_cost": _pf(side), "net_r_after_cost": sum(side),
                "max_drawdown_r_after_cost": _max_dd(side) if side else None,
            }
        return {
            "identity": IDENTITY, "research_identities": {"LONG": IDENTITY, "SHORT": SHORT_IDENTITY},
            "schema": SCHEMA, "broker_inert": True,
            "policy": "B_UNMANAGED_FROZEN_OPEN_DOL", "round_trip_cost_usd": ROUND_TRIP_COST_USD,
            "point_value_usd": POINT_VALUE, "tick_points": TICK,
            "historical_development_reference_only": {
                "direction": "LONG", "identity": IDENTITY,
                "fills": 126, "win_rate_pct": 30.95, "pf_after_cost": 1.7045,
                "net_r_after_cost": 66.9784, "not_live_expectation": True,
            },
            "engine": dict(_LAST), "armed_after": None if armed is None else _iso(pd.Timestamp(int(armed), unit="ms", tz="UTC")),
            "collection_started_at": None if armed is None else _iso(pd.Timestamp(int(armed), unit="ms", tz="UTC")),
            "history_path": str(ARCHIVE_PATH), "physical_contract_ids_available": "instrument_id" in (pd.read_csv(ARCHIVE_PATH, nrows=0).columns if ARCHIVE_PATH.exists() else []),
            "candidate_counts": counts, "stage_counts": stages, "order_counts": orders,
            "funnel": json.loads(funnel_raw), "metrics": metrics, "metrics_by_direction": by_direction,
            "current_thesis": json.loads(thesis_raw),
            "latest_thesis": json.loads(thesis_raw).get("thesis"),
            "open_positions": open_positions,
        }


def _candidate_rows() -> list[dict[str, Any]]:
    clauses, args = [], []
    status = request.args.get("status", "").strip().upper()
    execution = request.args.get("execution", "").strip().upper()
    direction = request.args.get("direction", "").strip().upper()
    reason = request.args.get("reason", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if status:
        clauses.append("c.status=?"); args.append(status)
    execution_clauses = {
        "FILLED": "o.state='FILLED'",
        "OPEN": "t.state='OPEN'",
        "CLOSED": "t.state='CLOSED'",
        "PENDING": "o.state='PENDING'",
        "UNFILLED_EXPIRED": "o.state='UNFILLED_EXPIRED'",
    }
    if execution in execution_clauses:
        clauses.append(execution_clauses[execution])
    if direction in {"LONG", "SHORT"}:
        clauses.append("c.direction=?"); args.append(direction)
    if reason:
        clauses.append("COALESCE(c.rejection_reason,'')=?"); args.append(reason)
    if start:
        clauses.append("c.decision_ms>=?"); args.append(_ms(start))
    if end:
        clauses.append("c.decision_ms<?"); args.append(_ms(end) + 86_400_000)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = min(max(int(request.args.get("limit", "500")), 1), 2000)
    with _connect() as con:
        rows = con.execute(
            "SELECT c.*,o.order_id,o.state order_state,o.fill_ms,t.trade_id,t.state trade_state,"
            "t.exit_reason,t.net_r FROM continuation_candidates c "
            "LEFT JOIN continuation_orders o ON o.candidate_id=c.candidate_id "
            "LEFT JOIN continuation_trades t ON t.order_id=o.order_id" + where
            + " ORDER BY c.decision_ms DESC LIMIT ?", (*args, limit)).fetchall()
    result = []
    for x in rows:
        row = dict(x); row["payload"] = json.loads(row.pop("payload_json")); row["decision_at"] = _iso(pd.Timestamp(row["decision_ms"], unit="ms", tz="UTC"))
        result.append(row)
    return result


def _candidate_funnel_stats() -> dict[str, int]:
    with _connect() as con:
        canonical = con.execute("SELECT COUNT(*) FROM continuation_candidates WHERE event_kind='CANONICAL_OUTPUT'").fetchone()[0]
        return {
            "candidates": con.execute("SELECT COUNT(*) FROM continuation_candidates").fetchone()[0],
            "close_through": con.execute("SELECT COUNT(*) FROM continuation_candidates WHERE event_kind='CLOSE_THROUGH'").fetchone()[0],
            "canonical_confirmed": canonical,
            "htf_long": con.execute("SELECT COUNT(*) FROM continuation_candidates WHERE event_kind='CANONICAL_OUTPUT' AND direction='LONG' AND json_extract(payload_json,'$.jade_thesis')='LONG'").fetchone()[0],
            "htf_short": con.execute("SELECT COUNT(*) FROM continuation_candidates WHERE event_kind='CANONICAL_OUTPUT' AND direction='SHORT' AND json_extract(payload_json,'$.jade_thesis')='SHORT'").fetchone()[0],
            "dol_aligned": con.execute("SELECT COUNT(*) FROM continuation_candidates WHERE eligible=1").fetchone()[0],
            "ready": con.execute("SELECT COUNT(*) FROM continuation_orders WHERE state='PENDING'").fetchone()[0],
            "filled": con.execute("SELECT COUNT(*) FROM continuation_orders WHERE state='FILLED'").fetchone()[0],
            "open_trades": con.execute("SELECT COUNT(*) FROM continuation_trades WHERE state='OPEN'").fetchone()[0],
            "closed_trades": con.execute("SELECT COUNT(*) FROM continuation_trades WHERE state='CLOSED'").fetchone()[0],
        }


def _forward_pine_trades(limit: int = 50) -> tuple[list[dict[str, Any]], int, int | None]:
    """Read actual forward shadow fills only; never infer trades from candidates."""
    limit = min(max(int(limit), 1), 100)  # At most 300 lines and 200 labels in Pine.
    with _connect() as con:
        total = con.execute("SELECT COUNT(*) FROM continuation_trades WHERE state IN ('OPEN','CLOSED')").fetchone()[0]
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM continuation_trades WHERE state IN ('OPEN','CLOSED') "
            "ORDER BY fill_ms DESC, trade_id DESC LIMIT ?", (limit,)
        ).fetchall()]
        last_bar = _meta(con, "last_bar_ms")
    rows.reverse()
    return rows, int(total), None if last_bar is None else int(last_bar)


def _pine_forward_source(trades: list[dict[str, Any]], last_bar_ms: int | None) -> str:
    """Render ledger fills as a display-only Pine v6 overlay, not a TV strategy."""
    lines = [
        "//@version=6",
        'indicator("MNQ Continuation forward shadow fills", overlay=true, max_lines_count=500, max_labels_count=500)',
        "// Display-only export of forward shadow ledger. No alerts, orders, or signal recalculation.",
        "// Use a matching MNQ 1-minute contract chart; continuous back-adjusted prices may differ.",
        "plot(na, title=\"Display only\", display=display.none)",
    ]
    if not trades:
        lines.append("// No forward shadow fills recorded yet.")
        return "\n".join(lines) + "\n"
    lines.append("if barstate.islastconfirmedhistory")
    for trade in trades:
        side = str(trade["direction"])
        if side not in {"LONG", "SHORT"}:
            raise ValueError("invalid trade direction in shadow ledger")
        fill_ms = int(trade["fill_ms"])
        closed = trade["state"] == "CLOSED"
        end_ms = int(trade["exit_ms"]) if closed and trade["exit_ms"] is not None else int(last_bar_ms or fill_ms)
        end_ms = max(end_ms, fill_ms + 60_000)
        entry, stop, target = (float(trade[name]) for name in ("entry_price", "stop_price", "target_price"))
        order_suffix = str(trade["order_id"])[-8:]
        entry_text = json.dumps(f"{side} ENTRY {order_suffix}")
        lines.extend([
            f"    // {side} {order_suffix} | {trade['state']}",
            f"    line.new({fill_ms}, {entry:.2f}, {end_ms}, {entry:.2f}, xloc=xloc.bar_time, color=color.blue, width=2)",
            f"    line.new({fill_ms}, {stop:.2f}, {end_ms}, {stop:.2f}, xloc=xloc.bar_time, color=color.red, style=line.style_dashed)",
            f"    line.new({fill_ms}, {target:.2f}, {end_ms}, {target:.2f}, xloc=xloc.bar_time, color=color.orange, style=line.style_dotted)",
            f"    label.new({fill_ms}, {entry:.2f}, {entry_text}, xloc=xloc.bar_time, style={'label.style_label_up' if side == 'LONG' else 'label.style_label_down'}, color={'color.teal' if side == 'LONG' else 'color.purple'}, textcolor=color.white, size=size.tiny)",
        ])
        if closed:
            if trade["exit_price"] is None or trade["net_r"] is None:
                raise ValueError("closed shadow trade lacks exit or net R")
            net_r = float(trade["net_r"])
            exit_color = "color.green" if net_r > 0 else "color.red" if net_r < 0 else "color.gray"
            reason = str(trade["exit_reason"] or "EXIT").replace("_", " ")
            exit_text = json.dumps(f"{reason} {net_r:+.3f}R")
            lines.append(
                f"    label.new({end_ms}, {float(trade['exit_price']):.2f}, {exit_text}, "
                f"xloc=xloc.bar_time, style=label.style_label_left, color={exit_color}, textcolor=color.white, size=size.tiny)"
            )
        else:
            lines.append(
                f'    label.new({end_ms}, {entry:.2f}, "OPEN SHADOW", '
                'xloc=xloc.bar_time, style=label.style_label_left, color=color.blue, textcolor=color.white, size=size.tiny)'
            )
    return "\n".join(lines) + "\n"


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
:root{--bg:#0b0e14;--panel:#111827;--line:#243047;--text:#e7ebf2;--mut:#8993a6;--green:#4ade80;--red:#f87171;--amber:#fbbf24;--blue:#7ab8f5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,system-ui,sans-serif;padding:18px}h2{margin:0}a{color:var(--blue)}.mut{color:var(--mut)}.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.badge{border:1px solid #274264;border-radius:999px;padding:5px 9px;color:var(--blue);font-size:11px}.kpis{display:grid;grid-template-columns:repeat(6,minmax(100px,1fr));gap:9px;margin:16px 0}.kpi,.card,.help{background:var(--panel);border:1px solid var(--line);border-radius:12px}.kpi{padding:12px}.kv{font-size:22px;font-weight:800}.kk{font-size:10px;color:var(--mut);text-transform:uppercase}.help{padding:16px;margin:12px 0}.cards{display:grid;gap:12px}.card{padding:0;overflow:hidden}.head{display:flex;justify-content:space-between;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)}.title{font-weight:800}.green,.ok{color:var(--green)}.red,.bad{color:var(--red)}.warn{color:var(--amber)}.steps{display:grid;grid-template-columns:repeat(6,1fr);gap:9px;padding:14px}.step{border:1px solid #26334a;border-radius:10px;padding:11px;min-height:108px}.step.ok{border-color:#17633c}.step.bad{border-color:#6b2730}.step.wait{border-color:#66541f}.n{width:25px;height:25px;border-radius:50%;display:inline-grid;place-items:center;background:#25344b;margin-right:6px;font-weight:800}.step.ok .n{background:#1c7a49}.step.bad .n{background:#7f2834}.step.wait .n{background:#78651f}.st{font-weight:750}.sd{font-size:12px;color:#98a3b7;margin-top:8px}.levels{display:flex;gap:20px;flex-wrap:wrap;padding:0 16px 14px;color:#b8c0ce}.reason{padding:11px 16px;background:#0d1420;border-top:1px solid var(--line)}.empty{padding:30px;text-align:center;color:var(--mut)}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}input,select,button{background:#0b1728;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px}button{cursor:pointer}.bar,.grid{display:grid;gap:12px}.bar{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:20px 0}.metric{padding:14px}.big{font-size:24px;font-weight:700;margin-top:6px}.detail{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:12px}.detail .card{padding:14px}svg{width:100%;height:auto;background:#08111f;border-radius:9px}pre{white-space:pre-wrap;word-break:break-word;color:#c7d5e8}@media(max-width:1050px){.kpis{grid-template-columns:repeat(3,1fr)}.steps{grid-template-columns:repeat(2,1fr)}}@media(max-width:700px){.detail{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,1fr)}}
</style></head><body><p><a href="/continuation/pine" target="_blank">Pine · executed forward trades ↗</a></p><div id="app"></div>
<script>
const $=s=>document.querySelector(s), fmt=(x,n=3)=>x==null?'—':Number(x).toFixed(n), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function j(u){let r=await fetch(u,{cache:'no-store'});if(!r.ok)throw Error(await r.text());return r.json()}
function k(k,v){return `<div class="kpi"><div class="kk">${k}</div><div class="kv">${v}</div></div>`}function step(n,t,d,state){return `<div class="step ${state}"><div><span class="n">${n}</span><span class="st">${t}</span></div><div class="sd">${d}</div></div>`}
function cards(s){let m=s.metrics,e=s.engine,f=s.funnel||{},o=s.order_counts||{};return `<div class="bar"><div class="card metric">Engine / feed<div class="big ${e.status==='ok'?'ok':'warn'}">${esc(e.status)}</div><div class="mut">last bar ${esc(e.last_bar||'—')}</div></div><div class="card metric">Current HTF thesis<div class="big">${esc(s.latest_thesis||'brak danych')}</div><div class="mut">since ${esc(s.collection_started_at||'brak danych')}</div></div><div class="card metric">Candidates / rejected<div class="big">${f.canonical_outputs??0} / ${s.candidate_counts.REJECTED??0}</div><div class="mut">pending orders ${o.PENDING??0}</div></div><div class="card metric">Fills / closed<div class="big">${m.open_trades+m.closed_trades} / ${m.closed_trades}</div><div class="mut">W/L/flat ${m.winners}/${m.losers}/${m.flat}</div></div><div class="card metric">PF net<div class="big">${m.pf_after_cost==null?'brak danych':fmt(m.pf_after_cost)}</div><div class="mut">Net ${fmt(m.net_r_after_cost)}R · DD ${m.max_drawdown_r_after_cost==null?'brak danych':fmt(m.max_drawdown_r_after_cost)+'R'}</div></div></div>`}
async function dash(){let s=await j('/continuation/api/status');let fault=s.engine.last_error?`<div class="card metric bad" role="alert"><b>Scanner error</b><pre>${esc(s.engine.last_error)}</pre></div>`:'';$('#app').innerHTML=`<div class="top"><div><h2>MNQ Continuation · LONG + SHORT shadow</h2><div class="mut">Policy B unmanaged · frozen directional OPEN DOL · no broker actions</div></div><div class="badge">SHADOW ONLY · ${esc(s.engine.status)}</div></div>`+fault+cards(s)+`<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"><div class="card metric"><h3>LONG forward metrics</h3><pre>${esc(JSON.stringify(s.metrics_by_direction.LONG,null,2))}</pre></div><div class="card metric"><h3>SHORT exploratory forward metrics</h3><pre>${esc(JSON.stringify(s.metrics_by_direction.SHORT,null,2))}</pre></div><div class="card metric"><h3>Open positions</h3><pre>${esc(JSON.stringify(s.open_positions,null,2))}</pre></div><div class="card metric"><h3>Historical LONG Development reference only</h3><pre>${esc(JSON.stringify(s.historical_development_reference_only,null,2))}</pre></div><div class="card metric"><h3>Execution contract</h3><p>Directional causal OPEN DOL frozen at decision. Structural SL, one-tick-through fill, 10-minute expiry, no manager, fill-bar brackets disabled, then adverse-first. SHORT has no inherited Development profitability claim.</p></div></div>`}
async function list(){
  let q=new URLSearchParams(location.search),d=await j('/continuation/api/candidates?'+q),rows=d.rows||[],s=d.funnel_stats||{};
  let reasons=[...new Set(rows.map(x=>x.rejection_reason).filter(Boolean))].sort();
  let body=rows.map(x=>{
    let p=x.payload||{},side=x.direction||p.dir||p.direction||'LONG',short=side==='SHORT';
    let canonical=x.event_kind==='CANONICAL_OUTPUT',thesis=p.jade_thesis===side;
    let close=x.event_kind==='CLOSE_THROUGH'||p.source_event?.close_through===true;
    let dol=x.eligible===1&&!!x.dol_id,risk=dol&&x.entry_price!=null&&x.stop_price!=null;
    let closed=x.trade_state==='CLOSED',open=x.trade_state==='OPEN',filled=x.order_state==='FILLED',ready=x.order_state==='PENDING',tracking=x.status==='TRACKING';
    let outcome=closed?(x.net_r>0?'WIN':x.net_r<0?'LOSS':'FLAT'):null;
    let status=closed?'CLOSED · '+outcome:open?'OPEN TRADE':filled?'FILLED':ready?'PENDING ORDER':x.order_state==='UNFILLED_EXPIRED'?'UNFILLED EXPIRED':tracking?'TRACKING':x.status==='SUPERSEDED'?'CANONICAL CONFIRMED':'REJECTED';
    let cls=closed?(x.net_r>0?'green':x.net_r<0?'red':'warn'):open?'warn':filled||ready?'green':tracking||x.status==='SUPERSEDED'?'warn':'red';
    let finalState=(filled||ready||closed||open)?'ok':tracking?'wait':'bad';
    let source=p.source_event||{},level=source.bsl_price??source.ssl_price??p.bsl_price??p.ssl_price;
    return `<div class="card"><div class="head"><div class="title"><a href="/continuation/candidate/${encodeURIComponent(x.candidate_id)}"><span class="${short?'red':'green'}">${side}</span> · ${esc(x.bsl_name||p.cat||'LIQUIDITY')} · ${esc(x.decision_at)}</a></div><div class="${cls}"><b>${esc(status)}</b></div></div><div class="steps">
      ${step(1,'HTF '+side+' thesis',esc(p.jade_thesis||'not yet available')+' · fixed at 18:00 ET',thesis?'ok':'bad')}
      ${step(2,(short?'SSL':'BSL')+' close-through',esc(x.bsl_name||source.bsl_name||source.ssl_name)+' @ '+esc(level),close?'ok':'bad')}
      ${step(3,'Displacement + owned FVG',canonical?('FVG '+esc(p.fvg_lo)+'–'+esc(p.fvg_hi)):'Waiting for canonical chain',canonical?'ok':tracking?'wait':'bad')}
      ${step(4,'Pullback + hold + BOS',canonical?('BOS '+esc(p.bos_iso||p.bos_ms)):'Not confirmed',canonical?'ok':tracking?'wait':'bad')}
      ${step(5,'OPEN '+(short?'bearish':'bullish')+' DOL',dol?(esc(x.dol_id)+' @ '+fmt(x.target_price,2)):esc(x.rejection_reason||'not selected'),dol?'ok':'bad')}
      ${step(6,'Entry + risk / fill',risk?('Entry '+fmt(x.entry_price,2)+' · SL '+fmt(x.stop_price,2)+' · DOL TP '+fmt(x.target_price,2)):status,finalState)}</div>
      <div class="levels"><b>Candidate:</b> ${esc(x.candidate_id)} <b>Stage:</b> ${esc(x.stage)} <b>Order:</b> ${esc(x.order_state)} <b>Trade:</b> ${esc(x.trade_state)} ${x.net_r==null?'':('<b>Net:</b> '+fmt(x.net_r)+'R')}</div><div class="reason"><b>${closed?'Exit':open?'Execution':'Decision reason'}:</b> ${esc(closed?x.exit_reason:open?'Position open · no realized P&L':x.rejection_reason||status)}</div></div>`;
  }).join('')||`<div class="empty">${s.filled===0?'No forward shadow fills yet. Historical candidates are warm-up records, not executed trades.':q.get('execution')?'No trades match this execution-state filter.':'No candidates match these filters.'}</div>`;
  $('#app').innerHTML=`<div class="top"><div><h2>MNQ Continuation · LONG + SHORT candidates</h2><div class="mut">Independent directional shadow; SHORT is exploratory and has no inherited LONG performance claim.</div></div><div class="badge">SHADOW ONLY · independent scanner</div></div><div class="kpis">${k('All event records',s.candidates||0)}${k('Close-through events',s.close_through||0)}${k('Canonical setups',s.canonical_confirmed||0)}${k('HTF LONG matched',s.htf_long||0)}${k('HTF SHORT matched',s.htf_short||0)}${k('OPEN DOL eligible',s.dol_aligned||0)}${k('Forward fills',s.filled||0)}${k('Open trades',s.open_trades||0)}${k('Closed trades',s.closed_trades||0)}</div><div class="help"><b>How to read these totals</b><br><span class="mut">All event records = close-through events + canonical setups. One setup can appear in both stages; these are not independent trades. The totals include historical scanner warm-up and are not changed by the list filters below. Only orders armed after warm-up can become forward fills.</span><br><br><b>What each step means</b><br><span class="mut">1 matching JadeCap HTF thesis fixed at 18:00 ET → 2 registered BSL/SSL close-through → 3 directional displacement + event-owned FVG → 4 later pullback/hold + BOS → 5 causal directional OPEN DOL → 6 frozen Entry/SL/DOL target and shadow fill.</span></div><div class="filters"><select id="direction"><option value="">LONG + SHORT</option><option>LONG</option><option>SHORT</option></select><select id="execution"><option value="">all execution states</option><option value="FILLED">all filled trades</option><option value="OPEN">open trades</option><option value="CLOSED">closed trades</option><option value="PENDING">pending orders</option><option value="UNFILLED_EXPIRED">unfilled expired</option></select><select id="status"><option value="">all candidate statuses</option>${['TRACKING','REJECTED','SUPERSEDED','UNFILLED_OR_PENDING','FILLED_OR_PENDING_REPLAY'].map(x=>`<option>${x}</option>`)}</select><select id="reason"><option value="">all reasons</option>${reasons.map(x=>`<option>${esc(x)}</option>`)}</select><input id="start" type="date"><input id="end" type="date"><button id="go">Filter</button></div><div class="cards">${body}</div>`;
  for(let z of ['direction','execution','status','reason','start','end'])$('#'+z).value=q.get(z)||'';
  $('#go').onclick=()=>{let z=new URLSearchParams();for(let a of ['direction','execution','status','reason','start','end'])if($('#'+a).value)z.set(a,$('#'+a).value);location.search=z};
}
function chart(b,d){if(!b.length)return '<div class="card">No bars available.</div>';let W=1000,H=520,pad=55,vals=b.flatMap(x=>[x.low,x.high]),marks=[d.entry_price,d.stop_price,d.target_price].filter(x=>x!=null),lo=Math.min(...vals,...marks),hi=Math.max(...vals,...marks),p=(hi-lo)*.06||1;lo-=p;hi+=p;let x=i=>pad+(i+.5)*(W-2*pad)/b.length,y=v=>pad+(hi-v)*(H-2*pad)/(hi-lo),bw=Math.max(1,Math.min(6,(W-2*pad)/b.length*.65)),s=`<svg viewBox="0 0 ${W} ${H}">`;for(let n=0;n<6;n++){let v=lo+n*(hi-lo)/5,yy=y(v);s+=`<line x1="${pad}" y1="${yy}" x2="${W-pad}" y2="${yy}" stroke="#263952"/><text x="4" y="${yy+4}" fill="#8ca0ba" font-size="12">${v.toFixed(2)}</text>`}b.forEach((c,i)=>{let col=c.close>=c.open?'#2dd4bf':'#fb7185',xx=x(i),top=Math.min(y(c.open),y(c.close)),bot=Math.max(y(c.open),y(c.close));s+=`<line x1="${xx}" y1="${y(c.high)}" x2="${xx}" y2="${y(c.low)}" stroke="${col}"/><rect x="${xx-bw/2}" y="${top}" width="${bw}" height="${Math.max(1,bot-top)}" fill="${col}"/>`});[['ENTRY',d.entry_price,'#60a5fa'],['SL',d.stop_price,'#fb7185'],['DOL',d.target_price,'#fbbf24']].forEach(z=>{if(z[1]!=null)s+=`<line x1="${pad}" y1="${y(z[1])}" x2="${W-pad}" y2="${y(z[1])}" stroke="${z[2]}" stroke-width="2" stroke-dasharray="7 5"/><text x="${W-pad+5}" y="${y(z[1])+4}" fill="${z[2]}">${z[0]}</text>`});return s+'</svg>'}
async function detail(){let id=decodeURIComponent(location.pathname.split('/').pop()),d=await j('/continuation/api/candidate/'+encodeURIComponent(id)),state=d.trade?.state==='CLOSED'?'CLOSED':d.trade?.state==='OPEN'?'OPEN TRADE':d.order?.state||d.candidate.status;$('#app').innerHTML=`<p><a href="/continuation/candidates">← Candidates</a></p><div class="detail"><div class="card">${chart(d.bars,d.candidate)}</div><div class="card"><h3>${esc(id)}</h3><p><b>${esc(d.candidate.stage)}</b> · ${esc(state)}${d.trade?.state==='CLOSED'?' · '+esc(d.trade.exit_reason)+' · '+fmt(d.trade.net_r)+'R':''}</p><h4>Order / trade state</h4><pre>${esc(JSON.stringify({order:d.order,trade:d.trade},null,2))}</pre><h4>Frozen evidence</h4><pre>${esc(JSON.stringify(d.candidate.payload,null,2))}</pre></div></div>`}
(async()=>{try{if(location.pathname.includes('/candidate/'))await detail();else if(location.pathname.endsWith('/dashboard'))await dash();else await list()}catch(e){$('#app').innerHTML='<div class="card metric bad">'+esc(e)+'</div>'}})();
</script></body></html>'''


def register(app, archive_path: str | os.PathLike[str] | None = None) -> None:
    global ARCHIVE_PATH
    if archive_path is not None:
        ARCHIVE_PATH = Path(archive_path)
    _init_db()

    @app.get("/continuation")
    @app.get("/continuation/candidates")
    @app.get("/continuation/dashboard")
    @app.get("/continuation/candidate/<candidate_id>")
    def continuation_page(candidate_id: str | None = None):
        return Response(PAGE, mimetype="text/html")

    @app.get("/continuation/api/status")
    def continuation_status():
        return jsonify(_summary())

    @app.get("/continuation/api/candidates")
    def continuation_candidates():
        return jsonify(rows=_candidate_rows(), funnel_stats=_candidate_funnel_stats())

    @app.get("/continuation/pine")
    def continuation_pine():
        trades, total, last_bar_ms = _forward_pine_trades()
        source = _pine_forward_source(trades, last_bar_ms)
        if request.args.get("raw") == "1":
            return Response(source, mimetype="text/plain")
        notice = ("No forward shadow fills have been recorded yet; this script contains no trade markers."
                  if total == 0 else
                  f"Showing the latest {len(trades)} of {total} recorded forward shadow fills.")
        page = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Continuation Pine · forward fills</title>"
            "<style>body{margin:24px;background:#0b0e14;color:#e7ebf2;font:15px/1.5 system-ui,sans-serif}"
            "a{color:#7ab8f5}button{padding:9px 14px;border:1px solid #30557e;border-radius:8px;"
            "background:#18314e;color:#e7ebf2;cursor:pointer}textarea{display:block;width:100%;height:65vh;"
            "box-sizing:border-box;margin-top:16px;padding:14px;background:#101827;color:#d7e3f3;"
            "border:1px solid #2c3b50;border-radius:10px;font:12px/1.4 monospace}</style></head><body>"
            "<h2>MNQ Continuation · Pine for executed forward trades</h2>"
            f"<p>{html.escape(notice)}</p>"
            "<p>Display only: Entry (blue), structural SL (red), frozen OPEN DOL (orange), and actual shadow exit. "
            "This does not generate TradingView orders or import historical Development/CSV backtests. "
            "Use the matching MNQ 1-minute contract chart; back-adjusted continuous prices may not align.</p>"
            "<button id='copy'>Copy Pine script</button> "
            "<a href='/continuation/pine?raw=1' target='_blank'>Raw script</a> · "
            "<a href='/continuation/candidates?execution=FILLED'>Filled trades</a>"
            f"<textarea id='source' readonly>{html.escape(source)}</textarea>"
            "<script>document.getElementById('copy').onclick=async()=>{let s=document.getElementById('source');"
            "try{await navigator.clipboard.writeText(s.value);document.getElementById('copy').textContent='Copied';}"
            "catch(e){s.select();document.execCommand('copy');document.getElementById('copy').textContent='Copied';}}</script>"
            "</body></html>"
        )
        return Response(page, mimetype="text/html")

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
