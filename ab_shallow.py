"""Causal A/B shallow sibling entry.

The shallow order is known completely when the A/B signal bar closes.  It never
uses later price action or the retrospective "target-before-entry" label.

Production design:
- current A/B and A/B-shallow are sibling rows in one setup group;
- deep A/B keeps the normal ``RISK_PCT`` budget;
- A/B-shallow has its own ``AB_SHALLOW_RISK_PCT`` budget (defaults to ``RISK_PCT``);
- if both orders fill, maximum planned setup exposure is the sum of both budgets;
- both use the detector's structural stop;
- shallow target is fixed 2R by default (3R is configurable);
- both share the same active/expiry timestamps.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

TICK_DEFAULT = 0.25


def enabled(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return str(e.get("AB_SHALLOW_ENABLED", "0")).strip() == "1"


def _float(e: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(e.get(key, str(default)) or default)
    except Exception:
        return float(default)


def _int(e: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(float(e.get(key, str(default)) or default))
    except Exception:
        return int(default)


def tick_align(value: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    return round(round(float(value) / tick) * tick, 10)


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


def build_shallow_signal(signal: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a causal A/B-shallow sibling using only signal-time fields."""
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
    signal_close = signal.get("signal_close")
    if signal_close is None:
        # The detector does not expose close explicitly. agent.py stamps the latest
        # completed bar close before this function is called.
        signal_close = signal.get("_signal_close")
    if signal_close is None:
        raise ValueError("signal close is required for A/B-shallow")
    signal_close = float(signal_close)

    entry = tick_align(signal_close + fraction * (deep_entry - signal_close), tick)
    risk = abs(entry - stop)
    min_sl = _float(e, "AB_SHALLOW_MIN_SL_PTS", 5.0)
    max_sl = _float(e, "AB_SHALLOW_MAX_SL_PTS", 0.0)
    if risk < min_sl:
        raise ValueError("A/B-shallow stop is too tight")
    if max_sl > 0 and risk > max_sl:
        raise ValueError("A/B-shallow stop is wider than AB_SHALLOW_MAX_SL_PTS")
    target = tick_align(entry + (1.0 if direction == "LONG" else -1.0) * rr * risk, tick)

    child = dict(signal)
    gid = str(signal.get("_setup_group_id") or setup_group_id(signal))
    shallow_risk_pct = _float(e, "AB_SHALLOW_RISK_PCT", _float(e, "RISK_PCT", 0.5))
    if shallow_risk_pct <= 0:
        raise ValueError("AB_SHALLOW_RISK_PCT must be positive")

    child.update({
        "_strat": "A/B-shallow",
        "_setup_group_id": gid,
        "entry": entry,
        "SL": tick_align(stop, tick),
        "TP": target,
        "risk": risk,
        "tp_src": f"shallow_{int(rr)}R",
        "sl_src": signal.get("sl_src") or "detector_structural",
        "kind": "A/B shallow split",
        "cat": (str(signal.get("cat") or "A/B") + " · SHALLOW").strip(),
        "_shallow_fraction": fraction,
        "_shallow_rr": rr,
        "_deep_entry": deep_entry,
        "_signal_close": signal_close,
        "_risk_pct_override": shallow_risk_pct,
        "_risk_mode": "independent",
    })
    return child


def independent_risk_metadata(
    deep_signal: Mapping[str, Any],
    shallow_signal: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Describe the two independent risk budgets without sizing either order.

    Actual quantities are calculated by the normal execution sizing path for each
    sibling separately. This preserves the same caps and size multipliers used by
    ordinary A/B orders.
    """
    e = os.environ if env is None else env
    account = _float(e, "ACCOUNT", 100000.0)
    deep_pct = _float(e, "RISK_PCT", 0.5)
    shallow_pct = _float(e, "AB_SHALLOW_RISK_PCT", deep_pct)
    point_value = _float(e, "POINT_VALUE", 2.0)
    return {
        "deep_risk_pct": deep_pct,
        "shallow_risk_pct": shallow_pct,
        "combined_max_risk_pct": deep_pct + shallow_pct,
        "deep_budget": account * deep_pct / 100.0,
        "shallow_budget": account * shallow_pct / 100.0,
        "combined_max_budget": account * (deep_pct + shallow_pct) / 100.0,
        "deep_risk_per_contract": abs(float(deep_signal["entry"]) - float(deep_signal["SL"])) * point_value,
        "shallow_risk_per_contract": abs(float(shallow_signal["entry"]) - float(shallow_signal["SL"])) * point_value,
    }
