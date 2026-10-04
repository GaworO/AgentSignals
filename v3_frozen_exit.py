"""Frozen V3 F2/B11/B13/MSS events on completed M1 bars.

This is the live port of the causal event loop in
research_v3_lifetime_ict_manager_20261003/engine.py.  The rule IDs and first
event semantics are locked by FROZEN_EXIT_LIBRARY_V1.json.  It has no broker
side effects and never reads outcome labels.
"""
from __future__ import annotations

import math
import numpy as np

REASONS = ("F2", "B11", "B13", "MSS")
PRECEDENCE = ("F2", "B11", "B13", "MSS")
MANAGER_REASONS = {"M1": ("F2",), "M2": ("B13", "MSS"), "M3": ("F2", "B11")}
EXPLANATIONS = {
    "F2": "Supportive FVG failed by completed body close and opposing displacement was confirmed.",
    "B11": "No re-expansion after supportive FVG retest and opposing displacement was confirmed.",
    "B13": "No re-expansion after supportive FVG retest and opposing MSS was confirmed.",
    "MSS": "Opposing market structure shift was confirmed.",
}
TICK = .25


def displacement(b, k, z):
    if k < 21:
        return False
    o, h, l, c = map(float, b[k])
    body = z * (c-o)
    bodymax = float(np.max(np.abs(b[k-10:k, 3]-b[k-10:k, 0])))
    p = b[k-20:k]
    pc = b[k-21:k-1, 3]
    tr = np.maximum(p[:, 1]-p[:, 2], np.maximum(abs(p[:, 1]-pc), abs(p[:, 2]-pc)))
    atr = float(tr.mean())
    prior = float(np.max(b[k-15:k, 1]) if z == 1 else np.min(b[k-15:k, 2]))
    return bool(body > 0 and body+1e-9 >= bodymax and body+1e-9 >= atr and z*(c-prior) > 1e-9)


def bos(b, k, z):
    if k < 15:
        return False
    o, h, l, c = map(float, b[k])
    rng = h-l
    if rng <= 0 or z*(c-o)/rng < .5-1e-9:
        return False
    level = float(np.max(b[k-15:k, 1]) if z == 1 else np.min(b[k-15:k, 2]))
    return bool(z*(c-level) > 1e-9)


def gap(b, j, z):
    if j < 2:
        return None
    if z == 1 and b[j, 2]-b[j-2, 1] >= 3-1e-9:
        return float(b[j-2, 1]), float(b[j, 2])
    if z == -1 and b[j-2, 2]-b[j, 1] >= 3-1e-9:
        return float(b[j, 1]), float(b[j-2, 2])
    return None


def triggered(first, known_ms, enabled=None):
    """Only the first frozen occurrence of each rule is actionable."""
    enabled = enabled or {k: True for k in MANAGER_REASONS}
    hits = []
    for manager, reasons in MANAGER_REASONS.items():
        if enabled.get(manager, False):
            hits.extend((manager, reason) for reason in reasons if first.get(reason) == known_ms)
    reasons = [r for r in PRECEDENCE if any(reason == r for _, reason in hits)]
    managers = [m for m in MANAGER_REASONS if any(manager == m for manager, _ in hits)]
    primary = next(((m, r) for r in PRECEDENCE for m, reason in hits if reason == r), None)
    return dict(primary_manager=primary[0] if primary else None,
                primary_reason=primary[1] if primary else None,
                triggered_managers=managers, triggered_reasons=reasons,
                manager_status={m: m in managers for m in MANAGER_REASONS})


def evaluate(order, position, minutes, decision_ms, enabled=None, require_dense=True):
    """Rebuild first events through the last closed M1 candle, never the open candle.

    `minutes` is an OHLCV DataFrame indexed by UTC M1 open.  A data gap inside
    the trade invalidates the decision instead of silently changing event clocks.
    """
    fill_ms = int(position["fill_ms"])
    if minutes.empty or decision_ms % 60000 or fill_ms >= decision_ms:
        raise ValueError("no_completed_trade_bar")
    m = minutes.loc[minutes.index < __import__('pandas').Timestamp(decision_ms, unit='ms', tz='UTC')]
    ms = np.asarray(m.index.asi8 // 1_000_000, dtype=np.int64)
    b = m[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not len(ms) or not np.isfinite(b).all() or ms[-1]+60000 != decision_ms:
        raise ValueError("stale_or_invalid_m1")
    fi = int(np.searchsorted(ms, fill_ms//60000*60000))
    if fi >= len(ms) or ms[fi] != fill_ms//60000*60000 or len(ms)-fi < 2:
        raise ValueError("missing_fill_or_postfill_m1")
    if require_dense and np.any(np.diff(ms[fi:]) != 60000):
        raise ValueError("trade_m1_gap")
    z = 1 if order["direction"] == "LONG" else -1
    entry = float(position["entry_price"])
    risk = float(order["risk_points"])
    if risk <= 0 or not math.isfinite(risk):
        raise ValueError("invalid_risk")
    first = {k: None for k in ("f1", "f2", "ne2", "opposing_displacement", "opposing_mss", "fvg_reexpansion")}
    zones = []
    active = None
    last_protected = None
    best_close = float(b[fi, 3])
    best_favorable = 0.0
    pending_retest = None
    retest_armed = True
    invalidated_seen = opposing_seen = False
    current = {}
    for j in range(fi+1, len(ms)):
        known = int(ms[j])+60000
        close = float(b[j, 3])
        rr = z*(close-entry)/risk
        fav = z*((float(b[j, 1]) if z == 1 else float(b[j, 2]))-entry)/risk
        best_favorable = max(best_favorable, fav)
        sup = displacement(b, j, z)
        opp = displacement(b, j, -z)
        sup_bos = bos(b, j, z)
        if z*(close-best_close) > 0:
            extreme = z*(close-best_close) >= TICK-1e-9
            best_close = close
        else:
            extreme = False
        expansion = extreme or sup or sup_bos
        if opp:
            first["opposing_displacement"] = first["opposing_displacement"] or known
            opposing_seen = True
        for zone in zones:
            if not zone["valid"]:
                continue
            if z*(close-(zone["lower"] if z == 1 else zone["upper"])) < -1e-9:
                zone["valid"] = False
                if active is zone:
                    first["f1"] = first["f1"] or known
                    invalidated_seen = True
        active = next((q for q in reversed(zones) if q["valid"]), None)
        if active is not None and j > active["creation_bar"]:
            if expansion and pending_retest is None:
                active["reexpanded"] = True
            touched = bool(float(b[j, 2]) <= active["upper"] if z == 1 else float(b[j, 1]) >= active["lower"])
            if not touched:
                retest_armed = True
            if touched and retest_armed and pending_retest is None:
                active["reexpanded"] = False
                pending_retest = dict(zone=active, start_bar=j, reference=active["best_close"])
                retest_armed = False
            if z*(close-active["best_close"]) > 0:
                active["best_close"] = close
        if pending_retest is not None and j > pending_retest["start_bar"]:
            ref = pending_retest["reference"]
            reexp = z*(close-ref) >= TICK-1e-9 or sup or sup_bos
            if reexp:
                pending_retest["zone"]["reexpanded"] = True
                first["fvg_reexpansion"] = first["fvg_reexpansion"] or known
                pending_retest = None
            elif j-pending_retest["start_bar"] >= 3:
                first["ne2"] = first["ne2"] or known
                pending_retest = None
        for direction in (z, -z):
            bounds = gap(b, j, direction)
            if bounds is None or j-1 <= fi:
                continue
            if not displacement(b, j-1, direction) or direction != z:
                continue
            zone = dict(creation_bar=j, lower=bounds[0], upper=bounds[1], valid=True,
                        reexpanded=False, best_close=close,
                        protected=float(np.min(b[j-3:j, 2]) if z == 1 else np.max(b[j-3:j, 1])))
            zones.append(zone)
            active = zone
            last_protected = zone["protected"]
            retest_armed = True
        protected_break = last_protected is not None and z*(close-last_protected) < -1e-9
        if protected_break and opp:
            first["opposing_mss"] = first["opposing_mss"] or known
        if invalidated_seen and opposing_seen:
            first["f2"] = first["f2"] or known
        current = dict(current_r=rr, mfe_r=best_favorable,
                       active_fvg=(dict(lower=active["lower"], upper=active["upper"],
                                        protected=active["protected"], reexpanded=active["reexpanded"])
                                   if active else None), ne2=first["ne2"] is not None,
                       opposing_displacement=first["opposing_displacement"] is not None,
                       mss=first["opposing_mss"] is not None,
                       f1=first["f1"] is not None, f2=first["f2"] is not None,
                       completed_bar_ms=int(ms[j]), known_ms=known)
    # AND policies use the later of the two first completed-bar events, including ties.
    first["F2"] = first["f2"]
    first["B11"] = max(first["ne2"], first["opposing_displacement"]) if first["ne2"] and first["opposing_displacement"] else None
    first["B13"] = max(first["ne2"], first["opposing_mss"]) if first["ne2"] and first["opposing_mss"] else None
    first["MSS"] = first["opposing_mss"]
    current["first_events"] = first
    current["signals"] = triggered(first, decision_ms, enabled)
    return current
