"""Deterministic, challenge-safe order planning for A/B and A/B-shallow.

The guard must evaluate the exact quantity that will be sent to the broker.  This
module therefore owns sizing, price alignment and the projected stop-loss cost.
The executor is only allowed to serialize an already-created plan; it must not
change quantity after the guard decision.
"""
from __future__ import annotations

import math
import os
import uuid
from typing import Any, Mapping


ALLOWED_STRATEGIES = frozenset(("A/B", "A/B-shallow"))


class PlanError(ValueError):
    """The signal cannot be represented as a safe challenge order."""


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(env.get(key, str(default)) or default)
    except Exception as exc:
        raise PlanError(f"invalid {key}") from exc
    if not math.isfinite(value):
        raise PlanError(f"invalid {key}")
    return value


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(float(env.get(key, str(default)) or default))
    except Exception as exc:
        raise PlanError(f"invalid {key}") from exc


def _tick(value: float, tick: float) -> float:
    if tick <= 0:
        raise PlanError("EXEC_TICK must be positive")
    return round(round(float(value) / tick) * tick, 10)


def _session_reduction(signal: Mapping[str, Any], env: Mapping[str, str]) -> float:
    """Return a reduction in [0, 1]. Challenge mode never permits an upsize."""
    session = str(signal.get("sess") or "")
    raw = str(env.get("SESSION_SIZE_MULT", "") or "").strip()
    if not raw or not session:
        return 1.0
    for item in raw.split(","):
        try:
            name, value = item.split(":", 1)
            if name.strip() == session:
                return max(0.0, min(1.0, float(value)))
        except Exception:
            raise PlanError("invalid SESSION_SIZE_MULT")
    return 1.0


def build(signal: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build one immutable execution plan from a signal.

    In ``CHALLENGE_MODE=1`` (the default in the supplied deployment profile):
    - only A/B and A/B-shallow are accepted;
    - fixed ``EXEC_QTY`` and every upsize tag are ignored;
    - session/Monday/ramp rules may only reduce size;
    - a trade that cannot fit one contract inside its risk budget is rejected.
    """
    e = os.environ if env is None else env
    strategy = str(signal.get("_strat") or "A/B")
    if strategy not in ALLOWED_STRATEGIES:
        raise PlanError("strategy_not_allowed")

    direction = str(signal.get("dir") or "").upper()
    if direction not in ("LONG", "SHORT"):
        raise PlanError("bad direction")
    try:
        entry = float(signal["entry"])
        stop = float(signal["SL"])
        target = float(signal["TP"])
    except Exception as exc:
        raise PlanError("missing or invalid price") from exc
    if not all(math.isfinite(v) for v in (entry, stop, target)):
        raise PlanError("non-finite price")
    if direction == "LONG" and not (stop < entry < target):
        raise PlanError("invalid LONG price geometry")
    if direction == "SHORT" and not (target < entry < stop):
        raise PlanError("invalid SHORT price geometry")

    tick = _float(e, "EXEC_TICK", 0.25)
    offset = _float(e, "PRICE_OFFSET", 0.0)
    entry_broker = _tick(entry + offset, tick)
    stop_broker = _tick(stop + offset, tick)
    target_broker = _tick(target + offset, tick)
    risk_points = abs(entry_broker - stop_broker)
    point_value = _float(e, "POINT_VALUE", 2.0)
    account = _float(e, "ACCOUNT", 100000.0)
    risk_pct = float(signal.get("_risk_pct_override") if signal.get("_risk_pct_override") is not None
                     else _float(e, "RISK_PCT", 0.35))
    if risk_points <= 0 or point_value <= 0 or account <= 0 or risk_pct <= 0:
        raise PlanError("invalid risk inputs")

    budget = account * risk_pct / 100.0
    per_contract = risk_points * point_value
    qty = int(math.floor((budget + 1e-9) / per_contract))
    if qty < 1:
        raise PlanError("risk_budget_below_one_contract")

    # Ramp, Monday and session rules are reductions only.  The executor may not
    # apply any multiplier after this function returns.
    override = signal.get("_exec_qty_override")
    if override is not None:
        try:
            qty = min(qty, max(0, int(override)))
        except Exception as exc:
            raise PlanError("invalid ramp quantity") from exc
    if signal.get("_mon_quarter"):
        qty = int(math.floor(qty * 0.5))
    qty = int(math.floor(qty * _session_reduction(signal, e)))

    cap = _int(e, "EXEC_MAX_QTY", 15)
    if cap > 0:
        qty = min(qty, cap)
    if qty < 1:
        raise PlanError("risk_reduction_below_one_contract")

    challenge = str(e.get("CHALLENGE_MODE", "1")).strip() == "1"
    if not challenge:
        # Kept only for non-challenge research compatibility.  Challenge mode
        # deliberately ignores Magnet, Select and dynamic equity upsizing.
        mult = max(0.0, _float(e, "NON_CHALLENGE_SIZE_MULT", 1.0))
        qty = max(1, min(cap if cap > 0 else qty, int(math.floor(qty * mult))))

    stop_risk = qty * per_contract
    slip_points = max(0.0, _float(e, "DD_SLIPPAGE_PTS", 2.0))
    commission_rt = max(0.0, _float(e, "DD_COMMISSION_RT_USD", 1.24))
    projected_cost = qty * (slip_points * point_value + commission_rt)
    projected_risk = stop_risk + projected_cost

    cancel_after = _int(e, "EXEC_CANCEL_AFTER_SEC", int(round(_float(e, "FILL_WIN_MIN", 10) * 60)))
    cancel_after = max(1, min(3600, cancel_after))
    execution_id = str(signal.get("_execution_id") or ("exec_" + uuid.uuid4().hex))
    group_id = str(signal.get("_setup_group_id") or signal.get("_batch_group_id") or execution_id)
    created_ms = int(signal.get("_plan_created_ms") or 0)

    return {
        "execution_id": execution_id,
        "group_id": group_id,
        "strategy": strategy,
        "direction": direction,
        "ticker": str(e.get("EXEC_TICKER", e.get("CONTRACT", "MNQ1!"))),
        "entry": entry_broker,
        "stop": stop_broker,
        "target": target_broker,
        "qty": qty,
        "risk_pct_budget": risk_pct,
        "risk_budget_usd": round(budget, 2),
        "stop_risk_usd": round(stop_risk, 2),
        "projected_cost_usd": round(projected_cost, 2),
        "projected_risk_usd": round(projected_risk, 2),
        "risk_points": round(risk_points, 10),
        "point_value": point_value,
        "cancel_after_sec": cancel_after,
        "created_ms": created_ms,
    }


def attach(signal: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build and attach a plan exactly once; return the attached object."""
    plan = signal.get("_execution_plan")
    if plan is None:
        plan = build(signal, env)
        signal["_execution_plan"] = plan
        signal["_execution_id"] = plan["execution_id"]
        signal["_sent_qty"] = plan["qty"]
        signal["_exec_entry"] = plan["entry"]
        signal["_exec_tp"] = plan["target"]
    return plan


def group_risk(signals: list[Mapping[str, Any]]) -> float:
    """Sum exact post-rounding risk across independently risked sibling orders."""
    return round(sum(float((s.get("_execution_plan") or {}).get("projected_risk_usd") or 0.0)
                     for s in signals), 2)
