"""Activation gate and immutable identity helpers for DOL Reversal.

This module deliberately contains no broker calls.  LIVE is permitted only
for the explicitly configured TradersPost lifecycle.  An HTTP acknowledgement
and a causal market-data fill remain distinct from broker confirmation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MODEL = HERE / "dol_reversal_manager_v1/model.json"
THRESHOLD = HERE / "dol_reversal_manager_v1/threshold.json"
EXPECTED_MODEL_SHA256 = "13dc0cdc5044b86cb5c60796c760e98f08f234344fafcd4aa7ce53f65bf703e6"
EXPECTED_THRESHOLD_SHA256 = "01af5db5980e9b0672a5e72d8ce33098dcb29934fcd48e6ed897db7b9559dfe8"
FROZEN_MODEL = "DOL_REVERSAL_MANAGER_58_V1"
FROZEN_THRESHOLD = 0.892916
VALID_MODES = {"OFF", "SHADOW", "LIVE"}


def _mode(name: str) -> str:
    value = str(os.environ.get(name, "SHADOW") or "SHADOW").strip().upper()
    return value if value in VALID_MODES else "OFF"


def requested_modes() -> dict[str, str]:
    return {"reversal": _mode("DOL_REVERSAL_MODE"), "manager": _mode("DOL_MANAGER_MODE")}


def killed() -> bool:
    return str(os.environ.get("DOL_KILL_SWITCH", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def frozen_artifacts() -> dict[str, Any]:
    model_hash = _sha256(MODEL)
    threshold_hash = _sha256(THRESHOLD)
    try:
        threshold_value = float(json.loads(THRESHOLD.read_text(encoding="utf-8"))["threshold"])
    except Exception:
        threshold_value = None
    return {
        "model": FROZEN_MODEL,
        "threshold": threshold_value,
        "model_sha256": model_hash,
        "threshold_sha256": threshold_hash,
        "model_hash_ok": model_hash == EXPECTED_MODEL_SHA256,
        "threshold_hash_ok": threshold_hash == EXPECTED_THRESHOLD_SHA256,
        "threshold_value_ok": threshold_value == FROZEN_THRESHOLD,
    }


def broker_capabilities() -> dict[str, bool]:
    webhook_manager = os.environ.get("DOL_MANAGER_EXECUTION", "").upper() == "TRADERSPOST_WEBHOOK"
    return {
        "entry_submit": True,
        "traderspost_signal_and_log_id": True,
        "local_causal_fill_detection": True,
        "breakeven_webhook": webhook_manager,
        "full_close_webhook": webhook_manager,
        "virtual_protected_stop": webhook_manager,
        "confirmed_broker_order_id": False,
        "confirmed_broker_position_id": False,
    }


def readiness() -> dict[str, Any]:
    modes = requested_modes()
    artifacts = frozen_artifacts()
    capabilities = broker_capabilities()
    blockers: list[str] = []
    if killed():
        blockers.append("DOL_KILL_SWITCH")
    if not all((artifacts["model_hash_ok"], artifacts["threshold_hash_ok"], artifacts["threshold_value_ok"])):
        blockers.append("FROZEN_ARTIFACT_MISMATCH")
    required = ("entry_submit","traderspost_signal_and_log_id","local_causal_fill_detection",
                "breakeven_webhook","full_close_webhook","virtual_protected_stop")
    missing = [name for name in required if not capabilities.get(name)]
    live_blockers = ["TRADERSPOST_LIVE_CAPABILITY:" + name for name in missing]
    if "LIVE" in modes.values(): blockers.extend(live_blockers)
    live_ready = not blockers and modes["reversal"] == "LIVE" and modes["manager"] == "LIVE"
    shadow_ready = (not killed() and all((artifacts["model_hash_ok"], artifacts["threshold_hash_ok"],
                                         artifacts["threshold_value_ok"])))
    effective = {
        "reversal": "OFF" if killed() or modes["reversal"] == "OFF" else modes["reversal"],
        "manager": "OFF" if killed() or modes["manager"] == "OFF" else modes["manager"],
    }
    if blockers and "LIVE" in modes.values():
        effective = {key: ("SHADOW" if value == "LIVE" else value) for key, value in effective.items()}
    return {
        "status": "LIVE READY" if live_ready else "SHADOW READY" if shadow_ready else "NOT READY",
        "requested_modes": modes,
        "effective_modes": effective,
        "kill_switch": killed(),
        "frozen_artifacts": artifacts,
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
