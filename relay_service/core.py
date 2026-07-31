"""Pure relay transformations; no Flask/network dependency."""
from __future__ import annotations

import json
import os
import re
from typing import Any

PLAN_RE = re.compile(r"\[plan:([^\]]+)\]")
SIGNAL_RE = re.compile(r"\[signal:([^\]]+)\]")
LEG_RE = re.compile(r"\[leg:(\d+)/(\d+)\]")


def identity(payload: dict[str, Any]) -> dict[str, Any]:
    extras = payload.get("extras") if isinstance(payload.get("extras"), dict) else {}
    text = str(payload.get("text") or payload.get("message") or "")
    plan_match = PLAN_RE.search(text)
    signal_match = SIGNAL_RE.search(text)
    leg_match = LEG_RE.search(text)
    return {
        "plan_id": extras.get("plan_id") or (plan_match.group(1) if plan_match else None),
        "signal_key": extras.get("signal_key") or (signal_match.group(1) if signal_match else None),
        "leg": extras.get("leg") or (int(leg_match.group(1)) if leg_match else None),
        "leg_count": extras.get("leg_count") or (int(leg_match.group(2)) if leg_match else None),
        "active_from_ms": extras.get("active_from_ms"),
        "valid_until_ms": extras.get("valid_until_ms"),
        "strategy": extras.get("strategy"),
    }


def prepare(payload: dict[str, Any], default_cancel_after_sec: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    out = json.loads(json.dumps(payload))
    ident = identity(out)
    extras = out.setdefault("extras", {})
    for key, value in ident.items():
        if value is not None:
            extras.setdefault(key, value)

    action = str(out.get("action") or "").lower()
    order_type = str(out.get("orderType") or "").lower()
    if action in {"buy", "sell", "add", "reverse"} and order_type in {"limit", "stop", "stop_limit"}:
        if not out.get("cancelAfter"):
            active = ident.get("active_from_ms")
            valid = ident.get("valid_until_ms")
            if active and valid and int(valid) > int(active):
                seconds = int((int(valid) - int(active) + 999) // 1000)
            else:
                seconds = default_cancel_after_sec
                if seconds is None:
                    seconds = int(float(os.environ.get("RELAY_CANCEL_AFTER_SEC", "600") or 600))
            out["cancelAfter"] = max(1, min(3600, int(seconds)))
    return out, ident


def relay_result(tp: dict[str, Any], ident: dict[str, Any], *, http_status: int, event_ms: int, cancel_after: int | None) -> dict[str, Any]:
    provider_success = tp.get("success") is not False
    ok = 200 <= int(http_status) < 300 and provider_success
    result = {
        "success": ok,
        "status": "accepted" if ok else "rejected",
        "provider": "traderspost-relay",
        "plan_id": ident.get("plan_id"),
        "signal_key": ident.get("signal_key"),
        "relay_signal_id": tp.get("id"),
        "relay_log_id": tp.get("logId"),
        "cancel_after": cancel_after,
        "event_ms": int(event_ms),
        "upstream_http": int(http_status),
    }
    if not ok:
        result["message"] = tp.get("message") or "TradersPost rejected signal"
        result["message_code"] = tp.get("messageCode")
    return result
