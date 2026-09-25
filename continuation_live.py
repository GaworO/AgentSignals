#!/usr/bin/env python3
"""Fail-closed live dispatch adapter for frozen Continuation LONG/SHORT.

The detector and forward ledger remain owned by ``continuation_shadow``.  This
adapter consumes only newly-created, forward-eligible orders after a completed
scan.  It owns an independent durable dispatch ledger and delegates the actual
account Guard/broker decision back to ``agent.py``.

Live routing is opt-in per direction.  On first start the adapter arms after the
latest scanned bar so historical/pending shadow rows can never be catch-up sent.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import continuation_shadow
import trade_classification


DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
DB_PATH = Path(os.environ.get("CONTINUATION_LIVE_DB", str(DATA_DIR / "continuation_live.sqlite3")))
_DISPATCHER: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _enabled(direction: str, strategy: str = "CONTINUATION") -> bool:
    prefix = "AB_DIRECTIONAL_LIVE" if strategy == "AB_DIRECTIONAL" else "CONTINUATION_LIVE"
    key = prefix + ("_LONG" if direction == "LONG" else "_SHORT")
    return os.environ.get(key, "0") == "1"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _init_db() -> None:
    with _connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_meta(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_dispatches(
              order_id TEXT PRIMARY KEY,
              candidate_id TEXT NOT NULL,
              strategy TEXT NOT NULL DEFAULT 'CONTINUATION',
              direction TEXT NOT NULL,
              activation_ms INTEGER NOT NULL,
              state TEXT NOT NULL,
              guard_reason TEXT,
              account_label TEXT,
              quantity INTEGER,
              route_id TEXT,
              source_json TEXT NOT NULL,
              result_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_live_dispatch_state
              ON live_dispatches(state, activation_ms DESC);
            """
        )
        columns = {row[1] for row in con.execute("PRAGMA table_info(live_dispatches)")}
        if "strategy" not in columns:
            con.execute("ALTER TABLE live_dispatches ADD COLUMN strategy TEXT NOT NULL DEFAULT 'CONTINUATION'")


def configure(dispatcher: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    if not callable(dispatcher):
        raise TypeError("dispatcher must be callable")
    global _DISPATCHER
    _DISPATCHER = dispatcher
    _init_db()


def _shadow_meta(con: sqlite3.Connection, key: str) -> int | None:
    row = con.execute("SELECT value FROM continuation_meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row[0]) if row else None
    except Exception:
        return None


def _source_orders() -> tuple[int | None, list[dict[str, Any]]]:
    continuation_shadow._init_db()
    with continuation_shadow._connect() as con:
        last_bar_ms = _shadow_meta(con, "last_bar_ms")
        rows = con.execute(
            """SELECT o.*,c.forward_eligible,c.decision_ms,c.trading_day
               FROM continuation_orders o
               JOIN continuation_candidates c USING(candidate_id)
               WHERE c.forward_eligible=1 AND o.state='PENDING'
               ORDER BY o.activation_ms,o.order_id"""
        ).fetchall()
    return last_bar_ms, [dict(row) for row in rows]


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM live_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def _set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO live_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
    )


def _claim(row: dict[str, Any], now: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO live_dispatches
               (order_id,candidate_id,strategy,direction,activation_ms,state,source_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (row["order_id"], row["candidate_id"], row.get("strategy") or "CONTINUATION",
             row["direction"], int(row["activation_ms"]),
             "DISPATCHING", json.dumps(row, sort_keys=True, separators=(",", ":"), default=str), now, now),
        )
        return cur.rowcount == 1


def _finish(order_id: str, result: dict[str, Any], now: str) -> None:
    state = str(result.get("state") or "ERROR")
    with _connect() as con:
        con.execute(
            """UPDATE live_dispatches SET state=?,guard_reason=?,account_label=?,quantity=?,
               route_id=?,result_json=?,updated_at=? WHERE order_id=?""",
            (state, result.get("reason"), result.get("account_label"), result.get("quantity"),
             result.get("route_id"), json.dumps(result, sort_keys=True, separators=(",", ":"), default=str),
             now, order_id),
        )


def drain(_scan_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch each new forward order at most once.

    ``BLOCKED``, ``DISABLED``, ``ERROR`` and ``SUBMISSION_UNKNOWN`` are terminal
    by design.  An ambiguous network result is never retried automatically.
    """
    _init_db()
    last_bar_ms, rows = _source_orders()
    if last_bar_ms is None:
        return {"status": "blocked", "reason": "shadow_last_bar_unknown", "processed": 0}
    with _connect() as con:
        armed = _meta(con, "armed_after_ms")
        if armed is None:
            _set_meta(con, "armed_after_ms", last_bar_ms)
            _set_meta(con, "armed_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            return {"status": "armed", "armed_after_ms": last_bar_ms, "processed": 0}
        armed_ms = int(armed)

    processed = []
    for row in rows:
        if int(row["activation_ms"]) <= armed_ms:
            continue
        if int(row["expiry_ms"]) <= last_bar_ms:
            continue
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if not _claim(row, now):
            continue
        if not _enabled(str(row["direction"]), str(row.get("strategy") or "CONTINUATION")):
            result = {"state": "DISABLED", "reason": "direction_not_enabled"}
        elif _DISPATCHER is None:
            result = {"state": "ERROR", "reason": "dispatcher_unavailable"}
        else:
            try:
                result = dict(_DISPATCHER(dict(row)) or {})
                result.setdefault("state", "ERROR")
            except Exception as exc:
                result = {"state": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        _finish(str(row["order_id"]), result, now)
        processed.append({"order_id": row["order_id"], "direction": row["direction"], **result})
    return {"status": "ok", "processed": len(processed), "results": processed}


def rows(limit: int = 500) -> list[dict[str, Any]]:
    _init_db()
    with _connect() as con:
        result = [dict(row) for row in con.execute(
            "SELECT * FROM live_dispatches ORDER BY activation_ms DESC LIMIT ?", (max(1, min(int(limit), 2000)),)
        ).fetchall()]
    for row in result:
        try:
            source = json.loads(row.get("source_json") or "{}")
        except Exception:
            source = {}
        meta = trade_classification.continuation_order({**source, **row})
        row["classification"] = meta["label"]
        row["strategy_class"] = meta["family"]
        row["setup_class"] = meta["setup_class"]
        row["quality_tier"] = meta["quality_tier"]
        row["quality_mode"] = meta["quality_mode"]
        row["dol_id"] = meta.get("dol_id") or source.get("dol_id")
    return result


def status() -> dict[str, Any]:
    _init_db()
    with _connect() as con:
        armed = _meta(con, "armed_after_ms")
        counts = {row["state"]: row["n"] for row in con.execute(
            "SELECT state,COUNT(*) n FROM live_dispatches GROUP BY state").fetchall()}
    return {"armed_after_ms": int(armed) if armed is not None else None,
            "long_enabled": _enabled("LONG"), "short_enabled": _enabled("SHORT"),
            "ab_directional_long_enabled": _enabled("LONG", "AB_DIRECTIONAL"),
            "ab_directional_short_enabled": _enabled("SHORT", "AB_DIRECTIONAL"),
            "dispatcher_ready": _DISPATCHER is not None, "counts": counts, "db_path": str(DB_PATH)}
