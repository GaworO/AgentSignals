"""Causal A/B-shallow sibling entry with one shared setup risk budget.

The sibling is built only from information known when the A/B signal bar closes.
It does not use the retrospective target-before-entry label.

Production rules:
- A/B and A/B-shallow share one absolute ``SETUP_GROUP_RISK_USD`` budget;
- the budget is split equally and unused sibling capacity is not reallocated;
- final integer quantities must keep the combined planned stop loss and configured
  round-trip costs at or below the setup-group budget;
- both orders use the detector's structural stop;
- shallow target is fixed 2R by default (3R is optional);
- both orders use the same normal fill window managed by the existing system.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

TICK_DEFAULT = 0.25
GROUP_SHARE = 0.50


def enabled(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return str(e.get("AB_SHALLOW_ENABLED", "0")).strip() == "1"


def _float(e: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(e.get(key, str(default)) or default)
    except Exception:
        return float(default)


def tick_align(value: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    return round(round(float(value) / tick) * tick, 10)


def setup_group_budget_usd(env: Mapping[str, str] | None = None) -> float:
    """Return the one absolute risk ceiling shared by both A/B siblings."""
    e = os.environ if env is None else env
    return max(0.0, _float(e, "SETUP_GROUP_RISK_USD", 900.0))


def setup_group_leg_budget_usd(env: Mapping[str, str] | None = None) -> float:
    return setup_group_budget_usd(env) * GROUP_SHARE


def setup_group_id(signal: Mapping[str, Any]) -> str:
    raw = "|".join([
        str(signal.get("date") or ""),
        str(signal.get("model") or ""),
        str(signal.get("dir") or ""),
        str(int(signal.get("bos_ms") or 0)),
        f"{float(signal.get('entry')):.4f}",
        f"{float(signal.get('SL')):.4f}",
    ])
    return "abg_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def build_shallow_signal(
    signal: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a causal A/B-shallow sibling using signal-time fields only."""
    e = os.environ if env is None else env
    fraction = max(0.0, min(1.0, _float(e, "AB_SHALLOW_FRACTION", 0.25)))
    rr = _float(e, "AB_SHALLOW_RR", 2.0)
    if rr not in (2.0, 3.0):
        raise ValueError("AB_SHALLOW_RR must be 2 or 3")
    tick = _float(e, "EXEC_TICK", TICK_DEFAULT)

    direction = str(signal.get("dir") or "").upper()
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")

    deep_entry = float(signal["entry"])
    stop = float(signal["SL"])
    signal_close = signal.get("_signal_close", signal.get("signal_close"))
    if signal_close is None:
        raise ValueError("signal close is required for A/B-shallow")
    signal_close = float(signal_close)

    entry = tick_align(signal_close + fraction * (deep_entry - signal_close), tick)
    risk_pts = abs(entry - stop)
    min_sl = _float(e, "AB_SHALLOW_MIN_SL_PTS", 5.0)
    max_sl = _float(e, "AB_SHALLOW_MAX_SL_PTS", 0.0)
    if risk_pts < min_sl:
        raise ValueError("A/B-shallow stop is too tight")
    if max_sl > 0 and risk_pts > max_sl:
        raise ValueError("A/B-shallow stop exceeds AB_SHALLOW_MAX_SL_PTS")

    # Refuse a sibling that cannot fit even one MNQ contract inside its equal
    # share of the setup-group budget. Costs are included so a nominal $900
    # ceiling is not silently exceeded by commissions/slippage allowance.
    account = _float(e, "ACCOUNT", 100000.0)
    point_value = _float(e, "POINT_VALUE", 2.0)
    if point_value <= 0:
        raise ValueError("POINT_VALUE must be positive")
    budget = setup_group_leg_budget_usd(e)
    risk_pct = 100.0 * budget / account if account > 0 else 0.0
    risk_per_contract = risk_pts * point_value + max(
        0.0, _float(e, "SETUP_GROUP_RT_COST_USD", 2.24)
    )
    if risk_per_contract > budget:
        raise ValueError("A/B-shallow risk budget cannot fit one contract")

    sign = 1.0 if direction == "LONG" else -1.0
    target = tick_align(entry + sign * rr * risk_pts, tick)
    gid = str(signal.get("_setup_group_id") or setup_group_id(signal))

    child = dict(signal)
    child.update({
        "_strat": "A/B-shallow",
        "_setup_group_id": gid,
        "entry": entry,
        "SL": tick_align(stop, tick),
        "TP": target,
        "risk": risk_pts,
        "tp_src": f"shallow_{int(rr)}R",
        "sl_src": signal.get("sl_src") or "detector_structural",
        "kind": "A/B shallow",
        "cat": (str(signal.get("cat") or "A/B") + " · SHALLOW").strip(),
        "_shallow_fraction": fraction,
        "_shallow_rr": rr,
        "_deep_entry": deep_entry,
        "_signal_close": signal_close,
        "_risk_budget_usd": budget,
        "_risk_pct_override": risk_pct,
        "_strict_risk_budget": True,
        "_risk_mode": "shared_group",
    })
    # Do not inherit size-up tags from the deep sibling.  Reductions such as
    # Monday/session sizing are still applied by the normal executor, but the
    # shallow order is never increased above its own configured risk budget.
    child.pop("_size_mult", None)
    child.pop("_select", None)
    return child


def apply_shared_group_budget(
    deep_signal: dict[str, Any],
    shallow_signal: dict[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Stamp the equal shared budget on the final siblings.

    The deep sibling is made strict too, which disables every size-up path. A
    later executor cap may reduce quantity, but it may never raise it above the
    quantity implied by this budget.
    """
    e = os.environ if env is None else env
    meta = risk_metadata(deep_signal, shallow_signal or deep_signal, e)
    account = max(0.0, _float(e, "ACCOUNT", 100000.0))
    leg_budget = float(meta["deep_budget"])
    risk_pct = 100.0 * leg_budget / account if account > 0 else 0.0
    for item in (deep_signal, shallow_signal):
        if item is None:
            continue
        item["_risk_budget_usd"] = leg_budget
        item["_risk_pct_override"] = risk_pct
        item["_strict_risk_budget"] = True
        item["_risk_mode"] = "shared_group"
        item.pop("_size_mult", None)
        item.pop("_select", None)
    return meta


def risk_metadata(
    deep_signal: Mapping[str, Any],
    shallow_signal: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, float]:
    e = os.environ if env is None else env
    account = _float(e, "ACCOUNT", 100000.0)
    group_budget = setup_group_budget_usd(e)
    deep_budget = group_budget * GROUP_SHARE
    shallow_budget = group_budget - deep_budget
    deep_pct = 100.0 * deep_budget / account if account > 0 else 0.0
    shallow_pct = 100.0 * shallow_budget / account if account > 0 else 0.0
    point_value = _float(e, "POINT_VALUE", 2.0)
    return {
        "deep_risk_pct": deep_pct,
        "shallow_risk_pct": shallow_pct,
        "combined_max_risk_pct": deep_pct + shallow_pct,
        "deep_budget": deep_budget,
        "shallow_budget": shallow_budget,
        "combined_max_budget": group_budget,
        "deep_risk_per_contract": abs(float(deep_signal["entry"]) - float(deep_signal["SL"])) * point_value,
        "shallow_risk_per_contract": abs(float(shallow_signal["entry"]) - float(shallow_signal["SL"])) * point_value,
    }
