"""Activation and immutable identities for DOL Delivery Reversal fixed brackets."""
from __future__ import annotations

import hashlib
import os
from typing import Any

VALID_MODES = {"OFF", "SHADOW", "LIVE"}


def _mode(name: str) -> str:
    value = str(os.environ.get(name, "SHADOW") or "SHADOW").strip().upper()
    return value if value in VALID_MODES else "OFF"


def requested_modes() -> dict[str, str]:
    return {"reversal": _mode("DOL_REVERSAL_MODE")}


def killed() -> bool:
    return str(os.environ.get("DOL_KILL_SWITCH", "0")).strip().lower() in {"1", "true", "yes", "on"}


def broker_capabilities() -> dict[str, bool]:
    return {
        "entry_submit": bool(os.environ.get("EXEC_WEBHOOK", "").strip()),
        "traderspost_signal_and_log_id": True,
        "local_causal_fill_detection": True,
        "confirmed_broker_order_id": False,
        "confirmed_broker_position_id": False,
    }


def readiness() -> dict[str, Any]:
    modes = requested_modes()
    capabilities = broker_capabilities()
    live_blockers = [] if capabilities["entry_submit"] else ["EXEC_WEBHOOK_MISSING"]
    blockers = ["DOL_KILL_SWITCH"] if killed() else []
    if modes["reversal"] == "LIVE":
        blockers.extend(live_blockers)
    effective = "OFF" if killed() else modes["reversal"]
    if blockers and effective == "LIVE":
        effective = "SHADOW"
    live_ready = effective == "LIVE" and not blockers
    return {
        "status": "LIVE READY" if live_ready else "SHADOW READY" if effective == "SHADOW" else "OFF",
        "requested_modes": modes,
        "effective_modes": {"reversal": effective},
        "exit_policy": "FIXED_SL_TP_2R",
        "kill_switch": killed(),
        "broker_capabilities": capabilities,
        "activation_blockers": blockers,
        "live_activation_blockers": live_blockers,
        "live_activation_allowed": live_ready,
        "note": "HTTP acknowledgement is WEBHOOK_ACCEPTED; local causal fills are LOCAL_FILL_DETECTED, never BROKER_FILL_CONFIRMED.",
    }


def enabled(component: str) -> bool:
    return readiness()["effective_modes"].get(component) in {"SHADOW", "LIVE"}


def signal_id(candidate_id: Any) -> str:
    return "DOLR-" + hashlib.sha256(str(candidate_id).encode("utf-8")).hexdigest()[:24]


def client_order_id(candidate_id: Any, account: str) -> str:
    raw = f"{signal_id(candidate_id)}|{account}"
    return "DOLR-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def shadow_payloads(candidate_id: Any, direction: str, entry: float, sl: float, tp: float) -> list[dict[str, Any]]:
    result = []
    for account in ("100K", "50K"):
        result.append({
            "account": account,
            "signal_id": signal_id(candidate_id),
            "client_order_id": client_order_id(candidate_id, account),
            "strategy": "DOL_DELIVERY_REVERSAL",
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "mode": "SHADOW",
            "submitted": False,
            "broker_order_id": None,
            "broker_position_id": None,
        })
    return result


def register(app):
    from flask import jsonify

    app.add_url_rule("/dol-reversal/readiness", "dol_reversal_readiness", lambda: jsonify(readiness()))
    return app
