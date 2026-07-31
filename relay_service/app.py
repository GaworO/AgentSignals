#!/usr/bin/env python3
"""AgentSignals execution relay for TradersPost.

This service deliberately separates three different identities/states:

1. AgentSignals ExecutionPlan (`plan_id`, `signal_key`).
2. TradersPost webhook Signal ID (`relay_signal_id`, `relay_log_id`).
3. Broker order ID/status, which TradersPost does not expose to strategy code.

The relay injects documented TradersPost fields (`extras`, `cancelAfter`) and returns
relay acceptance only. A true broker webhook/API can be connected to `/broker-event`,
which securely forwards lifecycle events to AgentSignals `/guard/broker-event`.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests
from flask import Flask, jsonify, request

try:
    from relay_service.core import prepare, relay_result
except ImportError:  # deployment with relay_service as Railway root directory
    from core import prepare, relay_result

app = Flask(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _authed(secret_name: str = "RELAY_SECRET") -> bool:
    expected = _env(secret_name)
    if not expected:
        return False
    supplied = (
        request.args.get("secret", "")
        or request.headers.get("X-Relay-Secret", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    return supplied == expected


def _traderspost_url() -> str:
    return _env("TRADERSPOST_WEBHOOK")


def _forward_guard(body: dict[str, Any]) -> requests.Response:
    base = _env("AGENT_BASE_URL").rstrip("/")
    token = _env("BROKER_EVENT_TOKEN")
    if not base or not token:
        raise RuntimeError("AGENT_BASE_URL and BROKER_EVENT_TOKEN are required")
    return requests.post(
        base + "/guard/broker-event",
        params={"t": token},
        json=body,
        timeout=float(_env("GUARD_CALLBACK_TIMEOUT_SEC", "10") or 10),
    )


@app.post("/execute")
@app.post("/stage")
def execute():
    if not _authed("RELAY_SECRET"):
        return jsonify(success=False, status="rejected", message="bad relay secret"), 401
    target = _traderspost_url()
    if not target:
        return jsonify(success=False, status="rejected", message="TRADERSPOST_WEBHOOK missing"), 503
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return jsonify(success=False, status="rejected", message="empty JSON"), 400

    prepared, ident = prepare(payload)
    started_ms = int(time.time() * 1000)
    try:
        response = requests.post(
            target,
            json=prepared,
            timeout=float(_env("TRADERSPOST_TIMEOUT_SEC", "15") or 15),
        )
    except requests.RequestException as exc:
        return jsonify(
            success=False,
            status="rejected",
            provider="traderspost-relay",
            plan_id=ident.get("plan_id"),
            signal_key=ident.get("signal_key"),
            message=str(exc),
        ), 502

    try:
        tp = response.json()
    except Exception:
        tp = {"message": (response.text or "")[:1000]}
    if not isinstance(tp, dict):
        tp = {"message": str(tp)}
    result = relay_result(
        tp, ident, http_status=response.status_code, event_ms=started_ms,
        cancel_after=prepared.get("cancelAfter"),
    )
    ok = bool(result["success"])
    return jsonify(result), (200 if ok else (response.status_code if response.status_code >= 400 else 502))


@app.post("/selftest")
def selftest():
    """Send a TradersPost test signal. `test=true` prevents broker order placement."""
    if not _authed("RELAY_SECRET"):
        return jsonify(success=False, status="rejected", message="bad relay secret"), 401
    target = _traderspost_url()
    if not target:
        return jsonify(success=False, status="rejected", message="TRADERSPOST_WEBHOOK missing"), 503
    now_ms = int(time.time() * 1000)
    payload = {
        "ticker": _env("RELAY_SELFTEST_TICKER", "MNQ1!"),
        "action": "buy",
        "orderType": "market",
        "quantity": 1,
        "test": True,
        "extras": {"strategy": "AgentSignals relay selftest", "event_ms": now_ms},
    }
    try:
        response = requests.post(
            target, json=payload,
            timeout=float(_env("TRADERSPOST_TIMEOUT_SEC", "15") or 15),
        )
    except requests.RequestException as exc:
        return jsonify(success=False, status="rejected", message=str(exc)), 502
    try:
        body = response.json()
    except Exception:
        body = {"message": (response.text or "")[:1000]}
    return jsonify(
        success=response.ok and not (isinstance(body, dict) and body.get("success") is False),
        upstream_http=response.status_code,
        traderspost=body,
        test=True,
    ), (200 if response.ok else response.status_code)


@app.post("/broker-event")
def broker_event():
    """Pass a real broker/provider lifecycle webhook to AgentSignals.

    TradersPost itself cannot supply this feedback. Use this endpoint only when the
    connected broker, direct API bridge or execution provider can emit order events.
    """
    if not _authed("BROKER_CALLBACK_SECRET"):
        return jsonify(ok=False, error="bad broker callback secret"), 401
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict) or not body:
        return jsonify(ok=False, error="empty JSON"), 400
    try:
        upstream = _forward_guard(body)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502
    try:
        data = upstream.json()
    except Exception:
        data = {"text": upstream.text[:1000]}
    return jsonify(ok=upstream.ok, guard_http=upstream.status_code, guard=data), upstream.status_code


@app.post("/reconcile-expired")
def reconcile_expired():
    """Operator-only reconciliation after checking the broker has no order/position."""
    if not _authed("RELAY_SECRET"):
        return jsonify(ok=False, error="bad relay secret"), 401
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("plan_id"):
        return jsonify(ok=False, error="plan_id required"), 400
    event = {
        "plan_id": body["plan_id"],
        "signal_key": body.get("signal_key"),
        "status": "expired",
        "filled_quantity": 0,
        "remaining_quantity": 0,
        "provider": "manual-reconciliation-via-relay",
        "reason": body.get("reason") or "Operator verified no broker position or working order",
        "event_ms": int(time.time() * 1000),
    }
    try:
        upstream = _forward_guard(event)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502
    try:
        data = upstream.json()
    except Exception:
        data = {"text": upstream.text[:1000]}
    return jsonify(ok=upstream.ok, guard_http=upstream.status_code, guard=data), upstream.status_code


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        service="AgentSignals TradersPost relay",
        version="32.1",
        traderspost_configured=bool(_traderspost_url()),
        agent_callback_configured=bool(_env("AGENT_BASE_URL") and _env("BROKER_EVENT_TOKEN")),
        true_broker_feedback="requires direct broker/provider webhook; unavailable from TradersPost",
        default_cancel_after_sec=int(float(_env("RELAY_CANCEL_AFTER_SEC", "600") or 600)),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT", "8080") or 8080))
