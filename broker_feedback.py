"""Broker truth ledger for the challenge executor.

AUTO is fail-closed unless a recent account snapshot and lifecycle callbacks are
available.  Relay HTTP 2xx means only "message accepted"; it never means that an
order was filled, canceled or closed.  Those states enter here exclusively via
token-authenticated broker/bridge callbacks.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import math
import os
import threading
import time
from typing import Any, Mapping


DATA_DIR = os.environ.get("DATA_DIR", ".")
BROKER_STATE = os.path.join(DATA_DIR, "broker_state.json")
EXECUTIONS = os.path.join(DATA_DIR, "execution_ledger.json")
EVENTS = os.path.join(DATA_DIR, "broker_events.json")
_LOCK = threading.RLock()

ACTIVE_STATUSES = frozenset(("relay_accepted", "working", "partial", "filled", "cancel_requested", "exit_requested"))
FINAL_STATUSES = frozenset(("canceled", "rejected", "closed", "expired"))
EVENT_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES


class FeedbackError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        raise FeedbackError(f"corrupt broker ledger: {os.path.basename(path)}") from exc


def _save(path: str, value: Any) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        raise FeedbackError(f"cannot persist {os.path.basename(path)}") from exc


def feedback_required() -> bool:
    return str(os.environ.get("BROKER_FEEDBACK_REQUIRED", "1")).strip() == "1"


def _number(value: Any, name: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise FeedbackError(f"invalid {name}") from exc
    if not math.isfinite(out):
        raise FeedbackError(f"invalid {name}")
    return out


def _timestamp_ms(value: Any) -> int:
    if value in (None, ""):
        return _now_ms()
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw * 1000 if raw < 10_000_000_000 else raw
    text = str(value).strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise FeedbackError("broker timestamp must include timezone")
    return int(parsed.timestamp() * 1000)


def _working_count(payload: Mapping[str, Any]) -> int:
    value = payload.get("working_orders", payload.get("working_orders_count", 0))
    if isinstance(value, list):
        return len(value)
    try:
        return max(0, int(value or 0))
    except Exception as exc:
        raise FeedbackError("invalid working_orders") from exc


def truth() -> dict[str, Any]:
    with _LOCK:
        return dict(_load(BROKER_STATE, {}))


def executions() -> dict[str, dict[str, Any]]:
    with _LOCK:
        data = _load(EXECUTIONS, {})
        if not isinstance(data, dict):
            raise FeedbackError("corrupt execution ledger")
        return data


def state_age_sec() -> float | None:
    try:
        stamp = int(truth().get("received_ms") or 0)
        return (_now_ms() - stamp) / 1000.0 if stamp else None
    except Exception:
        return None


def is_fresh(max_age_sec: float | None = None) -> bool:
    age = state_age_sec()
    if age is None or age < 0:
        return False
    if max_age_sec is None:
        max_age_sec = float(os.environ.get("BROKER_STATE_MAX_SEC", "90") or 90)
    return age <= float(max_age_sec)


def sync_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and persist an absolute broker/account snapshot."""
    account_expected = str(os.environ.get("BROKER_ACCOUNT_ID", "") or "").strip()
    account_received = str(payload.get("account_id") or "").strip()
    if account_expected and account_received != account_expected:
        raise FeedbackError("account_id mismatch")
    if payload.get("equity") is None:
        raise FeedbackError("equity is required")
    if payload.get("position_qty") is None:
        raise FeedbackError("position_qty is required")
    if "working_orders" not in payload and "working_orders_count" not in payload:
        raise FeedbackError("working_orders_count is required")
    previous = truth()
    as_of_ms = _timestamp_ms(payload.get("as_of_ms", payload.get("as_of")))
    now = _now_ms()
    if as_of_ms > now + int(float(os.environ.get("BROKER_MAX_FUTURE_SEC", "30")) * 1000):
        raise FeedbackError("snapshot timestamp is in the future")
    if now - as_of_ms > int(float(os.environ.get("BROKER_SNAPSHOT_MAX_DELAY_SEC", "120")) * 1000):
        raise FeedbackError("snapshot is too old")
    snap = {
        "snapshot_id": str(payload.get("snapshot_id") or f"snap_{as_of_ms}"),
        "account_id": account_received,
        "as_of_ms": as_of_ms,
        "received_ms": now,
        "equity": round(_number(payload.get("equity"), "equity"), 2),
        "balance": round(_number(payload.get("balance", payload.get("equity")), "balance"), 2),
        "position_qty": _number(payload.get("position_qty", 0), "position_qty"),
        "working_orders_count": _working_count(payload),
        "daily_realized_pnl": round(_number(payload.get("daily_realized_pnl", 0), "daily_realized_pnl"), 2),
        "trading_days": max(0, int(_number(payload.get("trading_days", previous.get("trading_days", 0)), "trading_days"))),
        "best_day_profit": round(max(0.0, _number(payload.get("best_day_profit", previous.get("best_day_profit", 0)), "best_day_profit")), 2),
        "evaluation_status": str(payload.get("evaluation_status") or previous.get("evaluation_status") or "active").strip().lower(),
        "trading_day": str(payload.get("trading_day") or previous.get("trading_day") or ""),
    }
    if payload.get("drawdown_floor") is not None:
        snap["drawdown_floor"] = round(_number(payload.get("drawdown_floor"), "drawdown_floor"), 2)
    if payload.get("total_realized_pnl") is not None:
        snap["total_realized_pnl"] = round(_number(payload.get("total_realized_pnl"), "total_realized_pnl"), 2)
    if snap["evaluation_status"] not in ("active", "passed", "failed", "breached"):
        raise FeedbackError("invalid evaluation_status")

    with _LOCK:
        previous = _load(BROKER_STATE, {})
        if int(previous.get("as_of_ms") or 0) > as_of_ms:
            raise FeedbackError("out-of-order broker snapshot")
        daily = dict(previous.get("daily_pnl") or {})
        if snap["trading_day"]:
            daily[snap["trading_day"]] = snap["daily_realized_pnl"]
        snap["daily_pnl"] = daily
        _save(BROKER_STATE, snap)
    return snap


def register_plan(plan: Mapping[str, Any]) -> bool:
    """Persist the exact guarded plan before the first network call."""
    execution_id = str(plan.get("execution_id") or "")
    if not execution_id:
        raise FeedbackError("execution_id is required")
    if str(plan.get("strategy")) not in ("A/B", "A/B-shallow"):
        raise FeedbackError("strategy_not_allowed")
    now = _now_ms()
    row = dict(plan)
    row.update(status="planned", registered_ms=now,
               expires_ms=now + int(plan.get("cancel_after_sec") or 600) * 1000,
               events=[])
    with _LOCK:
        ledger = executions()
        if execution_id in ledger:
            return ledger[execution_id].get("status") == "planned"
        ledger[execution_id] = row
        _save(EXECUTIONS, ledger)
    return True


def mark_relay_result(execution_id: str, accepted: bool, status_code: int | None, detail: Any = None) -> None:
    with _LOCK:
        ledger = executions()
        row = ledger.get(str(execution_id))
        if row is None:
            raise FeedbackError("unknown execution_id")
        row["status"] = "relay_accepted" if accepted else "rejected"
        row["relay_status"] = status_code
        row["relay_at_ms"] = _now_ms()
        if detail is not None:
            row["relay_detail"] = str(detail)[:300]
        ledger[str(execution_id)] = row
        _save(EXECUTIONS, ledger)


def mark_cleanup_requested(action: str, reason: str = "") -> None:
    now = _now_ms()
    with _LOCK:
        ledger = executions()
        for key, row in ledger.items():
            if row.get("status") in ACTIVE_STATUSES:
                row["status"] = "exit_requested" if action == "exit" else "cancel_requested"
                row["cleanup_reason"] = reason
                row["cleanup_requested_ms"] = now
                ledger[key] = row
        _save(EXECUTIONS, ledger)


def apply_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    event_id = str(payload.get("event_id") or "").strip()
    execution_id = str(payload.get("execution_id") or payload.get("client_order_id") or "").strip()
    status = str(payload.get("status") or payload.get("order_status") or "").strip().lower()
    if not event_id or not execution_id or status not in EVENT_STATUSES:
        raise FeedbackError("event_id, execution_id and valid status are required")
    if status == "closed" and payload.get("realized_pnl") is None:
        raise FeedbackError("closed event requires realized_pnl")
    if status == "canceled" and float(payload.get("filled_qty") or 0) > 0 and abs(float(payload.get("position_qty") or 0)) > 0:
        raise FeedbackError("partially filled position cannot be finalized as canceled")
    event_ms = _timestamp_ms(payload.get("event_ms", payload.get("broker_ts")))
    if payload.get("equity") is not None:
        sync_snapshot(payload)

    with _LOCK:
        seen = _load(EVENTS, [])
        if any(e.get("event_id") == event_id for e in seen[-5000:]):
            return {"duplicate": True, "event_id": event_id}
        ledger = executions()
        row = ledger.get(execution_id)
        if row is None:
            raise FeedbackError("unknown execution_id")
        if row.get("status") in FINAL_STATUSES and status in ACTIVE_STATUSES:
            raise FeedbackError("final execution cannot return to active state")
        if event_ms < int(row.get("last_event_ms") or 0):
            raise FeedbackError("out-of-order execution event")

        event = {
            "event_id": event_id,
            "execution_id": execution_id,
            "status": status,
            "event_ms": event_ms,
            "received_ms": _now_ms(),
            "order_id": str(payload.get("order_id") or ""),
        }
        for name in ("filled_qty", "remaining_qty", "fill_price", "realized_pnl", "position_qty"):
            if payload.get(name) is not None:
                event[name] = _number(payload.get(name), name)
        row["status"] = status
        row["last_event_ms"] = event_ms
        row["last_event_id"] = event_id
        row["order_id"] = event["order_id"] or row.get("order_id")
        if payload.get("filled_qty") is not None:
            row["filled_qty"] = event["filled_qty"]
        if payload.get("fill_price") is not None:
            row["fill_price"] = event["fill_price"]
        if payload.get("realized_pnl") is not None:
            pnl = event["realized_pnl"]
            row["realized_pnl"] = round(pnl, 2)
            if status == "closed":
                row["outcome"] = "win" if pnl > 0 else ("loss" if pnl < 0 else "timeout")
        row_events = list(row.get("events") or [])
        row_events.append(event_id)
        row["events"] = row_events[-100:]
        ledger[execution_id] = row
        seen.append(event)
        _save(EXECUTIONS, ledger)
        _save(EVENTS, seen[-5000:])

    return {"duplicate": False, "event_id": event_id, "execution_id": execution_id, "status": status}


def has_live_commitment() -> bool:
    try:
        return any(row.get("status") in ACTIVE_STATUSES for row in executions().values())
    except Exception:
        return True


def broker_open() -> bool:
    """Fail closed: unknown/corrupt truth is treated as an open commitment."""
    try:
        snap = truth()
        if not snap:
            return feedback_required()
        return abs(float(snap.get("position_qty") or 0)) > 0 or int(snap.get("working_orders_count") or 0) > 0
    except Exception:
        return True


def trade_truth(execution_id: str | None) -> dict[str, Any] | None:
    if not execution_id:
        return None
    try:
        row = executions().get(str(execution_id))
        if not row:
            return None
        status = row.get("status")
        if status == "closed":
            return {"outcome": row.get("outcome", "timeout"), "net": row.get("realized_pnl"), "source": "broker"}
        if status in ("canceled", "rejected", "expired"):
            return {"outcome": "canceled", "net": 0.0, "source": "broker"}
        return {"outcome": "open", "net": None, "source": "broker", "status": status}
    except Exception:
        return {"outcome": "open", "net": None, "source": "broker_error"}


def expired_execution_ids() -> list[str]:
    now = _now_ms()
    try:
        return [key for key, row in executions().items()
                if row.get("status") in ("relay_accepted", "working")
                and int(row.get("expires_ms") or 0) < now]
    except Exception:
        return []


def flat_confirmed_since(request_ms: int) -> bool:
    try:
        snap = truth()
        return (int(snap.get("received_ms") or 0) >= int(request_ms)
                and abs(float(snap.get("position_qty") or 0)) == 0
                and int(snap.get("working_orders_count") or 0) == 0)
    except Exception:
        return False


def status() -> dict[str, Any]:
    try:
        snap = truth()
        live = [key for key, row in executions().items() if row.get("status") in ACTIVE_STATUSES]
        return {
            "required": feedback_required(),
            "fresh": is_fresh(),
            "age_sec": None if state_age_sec() is None else round(state_age_sec(), 1),
            "position_qty": snap.get("position_qty"),
            "working_orders_count": snap.get("working_orders_count"),
            "evaluation_status": snap.get("evaluation_status"),
            "live_execution_count": len(live),
        }
    except Exception as exc:
        return {"required": feedback_required(), "fresh": False, "error": str(exc), "live_execution_count": None}


def _token_ok(request: Any) -> bool:
    expected = str(os.environ.get("BROKER_CALLBACK_TOKEN", "") or "")
    if not expected:
        return False
    supplied = str(request.headers.get("X-Broker-Token") or "")
    auth = str(request.headers.get("Authorization") or "")
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = str(request.args.get("t") or "")
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def register(app: Any) -> Any:
    try:
        from flask import jsonify, request
    except Exception:
        return app

    def _callback():
        if not _token_ok(request):
            return jsonify(ok=False, error="auth"), 401
        try:
            result = apply_event(request.get_json(force=True, silent=False) or {})
            return jsonify(ok=True, **result)
        except FeedbackError as exc:
            return jsonify(ok=False, error=str(exc)), 409

    def _sync():
        if not _token_ok(request):
            return jsonify(ok=False, error="auth"), 401
        try:
            snap = sync_snapshot(request.get_json(force=True, silent=False) or {})
            return jsonify(ok=True, snapshot_id=snap["snapshot_id"], as_of_ms=snap["as_of_ms"])
        except FeedbackError as exc:
            return jsonify(ok=False, error=str(exc)), 409

    def _status():
        return jsonify(**status())

    app.add_url_rule("/broker/callback", "broker_callback", _callback, methods=["POST"])
    app.add_url_rule("/broker/sync", "broker_sync", _sync, methods=["POST"])
    app.add_url_rule("/broker/status", "broker_status", _status)
    return app
