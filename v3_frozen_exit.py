"""V3 F2/B11/B13/MSS plus M3 OBFH expansion on completed M1 bars.

The original four reasons port the causal event loop in
research_v3_lifetime_ict_manager_20261003/engine.py and remain locked by
FROZEN_EXIT_LIBRARY_V1.json. OBFH_EXPANSION is a separately audited M3
extension. This module has no broker side effects or outcome-label inputs.
"""
from __future__ import annotations

import math
import numpy as np

REASONS = ("F2", "B11", "B13", "MSS", "OBFH_EXPANSION", "OBFH_3_CLOSES_10M")
PRECEDENCE = ("F2", "B11", "B13", "MSS", "OBFH_EXPANSION", "OBFH_3_CLOSES_10M")
MANAGER_REASONS = {"M1": ("F2",), "M2": ("B13", "MSS"),
                   "M3": ("F2", "B11", "OBFH_EXPANSION", "OBFH_3_CLOSES_10M")}
EXPLANATIONS = {
    "F2": "Supportive FVG failed by completed body close and opposing displacement was confirmed.",
    "B11": "No re-expansion after supportive FVG retest and opposing displacement was confirmed.",
    "B13": "No re-expansion after supportive FVG retest and opposing MSS was confirmed.",
    "MSS": "Opposing market structure shift was confirmed.",
    "OBFH_3_CLOSES_10M": "After no-expansion warning, opposing BOS and held FVG: three adverse closes within ten minutes; invalidation cancels the zone, expiry alone never exits.",
    "OBFH_EXPANSION": "Opposing BOS formed a fresh FVG; it held, then a later strong opposing candle expanded away from it.",
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


def _prior_atr20(b, k):
    if k < 21:
        return None
    p = b[k-20:k]
    pc = b[k-21:k-1, 3]
    tr = np.maximum(p[:, 1]-p[:, 2], np.maximum(abs(p[:, 1]-pc), abs(p[:, 2]-pc)))
    return float(tr.mean())


def opposing_fvg_expansion(b, ms, fi, z):
    """First causal BOS -> linked FVG -> H1 hold -> later 1xATR expansion.

    Both directions use the same signed logic. The BOS/source candle cannot
    supply the later expansion; a body close through the FVG cancels its pair.
    """
    adverse = -z
    bos_bars = set()
    linked_bos = set()
    zones = []
    for j in range(fi+1, len(ms)):
        known = int(ms[j])+60000
        close = float(b[j, 3])
        if bos(b, j, adverse):
            bos_bars.add(j)
        candidates = []
        for zone in zones:
            if not zone["valid"]:
                continue
            invalid = (close > zone["upper"]+1e-9 if adverse == -1
                       else close < zone["lower"]-1e-9)
            if invalid:
                zone["valid"] = False
                continue
            if j == zone["creation_bar"]+1:
                zone["held"] = True
            elif j > zone["creation_bar"]+1 and zone["held"]:
                o, h, l, _ = map(float, b[j])
                rng = h-l
                body = adverse*(close-o)
                atr = _prior_atr20(b, j)
                fresh = adverse*(close-zone["best_close"]) >= TICK-1e-9
                if (fresh and rng > 0 and body > 0
                        and body/rng >= .5-1e-9 and atr is not None
                        and rng >= atr-1e-9):
                    candidates.append((zone["bos_bar"],zone["creation_bar"],dict(
                        known_ms=known,bos_time=int(ms[zone["bos_bar"]])+60000,
                        fvg_created_time=int(ms[zone["creation_bar"]])+60000,
                        hold_time=int(ms[zone["creation_bar"]+1])+60000,
                        fvg_low=zone["lower"],fvg_high=zone["upper"],
                        expansion_bar_ms=int(ms[j]),expansion_close=close,
                        expansion_range=rng,expansion_atr20=atr,
                        expansion_body_fraction=body/rng)))
            if adverse*(close-zone["best_close"]) > 0:
                zone["best_close"] = close
        if candidates:
            return min(candidates,key=lambda item:(item[0],item[1]))[2]
        if j-1 <= fi:
            continue
        bounds = gap(b, j, adverse)
        if bounds is None:
            continue
        source = j-1 if j-1 in bos_bars else j if j in bos_bars else None
        if source is None or source in linked_bos:
            continue
        linked_bos.add(source)
        zones.append(dict(bos_bar=source,creation_bar=j,lower=bounds[0],upper=bounds[1],
                          valid=True,held=False,best_close=close))
    return None


def opposing_fvg_three_closes(b, ms, fi, z, entry, risk, fill_ms):
    """Independent frozen 10-minute branch, measured from H1 confirmation.

    A prior NE3 warning with MFE < .75R must exist by BOS confirmation.
    Each linked FVG has its own clock; expiry alone is never an exit.
    """
    adverse = -z
    best_close = float(b[fi, 3])
    mfe = 0.0
    ne_run = 0
    warned = False
    eligible_bos = set()
    zones = []
    for j in range(fi+1, len(ms)):
        known = int(ms[j])+60000
        close = float(b[j, 3])
        mfe = max(mfe, z*(float(b[j, 1 if z == 1 else 2])-entry)/risk)
        expansion = (z*(close-best_close) >= TICK-1e-9
                     or displacement(b, j, z) or bos(b, j, z))
        if z*(close-best_close) > 0:
            best_close = close
        ne_run = 0 if expansion else ne_run+1
        warned = warned or (ne_run >= 3 and known-fill_ms >= 180000 and mfe < .75)
        if warned and bos(b, j, adverse):
            eligible_bos.add(j)
        hits = []
        for q in zones:
            if not q['valid']:
                continue
            invalid = close > q['upper']+1e-9 if z == 1 else close < q['lower']-1e-9
            if invalid:
                q['valid'] = False
                continue
            if j == q['creation_bar']+1:
                q.update(hold_ms=known, hold_close=close, previous=close, run=0)
                continue
            if 'hold_ms' not in q:
                continue
            if known > q['hold_ms']+600000 or j > q['creation_bar']+11:
                q['valid'] = False
                continue
            q['run'] = q['run']+1 if adverse*(close-q['previous']) >= TICK-1e-9 else 0
            q['previous'] = close
            cumulative = adverse*(close-q['hold_close'])
            if q['run'] >= 3 and cumulative >= -1e-9:
                hits.append(q)
        if hits:
            q = min(hits, key=lambda v:(v['bos_bar'], v['creation_bar']))
            return dict(known_ms=known, bos_time=int(ms[q['bos_bar']])+60000,
                        fvg_created_time=int(ms[q['creation_bar']])+60000,
                        hold_time=q['hold_ms'], deadline_ms=q['hold_ms']+600000,
                        fvg_low=q['lower'], fvg_high=q['upper'], close_run=q['run'],
                        cumulative_expansion_points=adverse*(close-q['hold_close']))
        if j-1 <= fi:
            continue
        bounds = gap(b, j, adverse)
        if bounds is None:
            continue
        # Prefer the displacement/source candle exactly as in the research.
        source = j-1 if bos(b, j-1, adverse) else j if bos(b, j, adverse) else None
        if source not in eligible_bos:
            continue
        zones.append(dict(bos_bar=source, creation_bar=j, lower=bounds[0],
                          upper=bounds[1], valid=True))
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
    # Pandas may store the index at ms, us or ns resolution. Timestamp.value
    # is always nanoseconds, including on older pandas releases.
    ms = np.fromiter((stamp.value // 1_000_000 for stamp in m.index),
                     dtype=np.int64, count=len(m))
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
    obfh = opposing_fvg_expansion(b, ms, fi, z)
    first["OBFH_EXPANSION"] = obfh["known_ms"] if obfh else None
    ten = opposing_fvg_three_closes(b, ms, fi, z, entry, risk, fill_ms)
    first["OBFH_3_CLOSES_10M"] = ten["known_ms"] if ten else None
    current["obfh_3_closes_10m"] = ten
    current["first_events"] = first
    current["obfh_expansion"] = obfh
    current["signals"] = triggered(first, decision_ms, enabled)
    return current
