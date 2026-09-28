"""V3 outcome-free policy. No app, database, network or research imports.

Port of MTF_SOURCE_DOL_2R from the frozen exploratory AB study. Inputs must
already be causal and verified. This is not an entry filter or Safety85 model.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

POLICY = "AB_V3_MTF_SOURCE_DOL_2R"
TF = {"M5": 5, "M15": 15, "H1": 60}


def finite(v):
    return v is not None and math.isfinite(float(v))


def decide(m, context):
    known = finite(m["speed"]) and finite(m["efficiency"])
    adverse = known and m["speed"] <= -.15 and m["efficiency"] <= -.30
    support = sum(c.get("vote") == 1 for c in context.values())
    opposition = sum(c.get("vote") == -1 for c in context.values())
    streak = (1 if m["dol_delivered"] or opposition >= 2 else
              3 if support >= 2 and not m["source_failed"] else 2)
    local = ((m["mfe"] < .5 and m["progress"] < -.25 and m["failed_fvg"])
             or (m["be_returned"] and m["progress"] < -.15 and m["failed_entry"]))
    broken = {tf: context[tf].get("broken") is True for tf in TF}
    loss = m["progress"] < -.15 and (
        (m["source_failed"] and (broken["M5"] or opposition >= 2))
        or (local and broken["M5"] and (broken["M15"] or broken["H1"])))
    fire = bool(known and m["age_seconds"] >= 60 and m["weak_streak"] >= streak
                and adverse and (loss or m["m1_protect"]))
    return dict(exit=fire, cause=("M1_PROFIT_PROTECTION" if fire and m["m1_protect"] else
                "CONFIRMED_SETUP_INVALIDATION" if fire else "HOLD"),
                required_streak=streak, support=support, opposition=opposition,
                adverse=bool(adverse), local_loss=bool(local), loss_condition=bool(loss))


def acceptance(progress, anchor):
    if len(progress) < 60 or not finite(anchor):
        return False
    segment = np.asarray(progress[-60:], float)
    return bool(segment[-1] < anchor - .05 and np.mean(segment < anchor) >= .70
                and anchor - segment.min() >= .05 and not np.any(segment >= anchor + .05))


def pivots(bars, minutes):
    """Record a 2-left/2-right pivot on the confirmation bar, never its birth."""
    g = bars.copy()
    continuous = g.index.to_series().diff(4).eq(pd.Timedelta(minutes=4 * minutes))
    low, high = g.low, g.high
    masks = {
        1: (low.shift(2) < low.shift(4)) & (low.shift(2) < low.shift(3))
           & (low.shift(2) <= low.shift(1)) & (low.shift(2) <= low),
        -1: (high.shift(2) > high.shift(4)) & (high.shift(2) > high.shift(3))
            & (high.shift(2) >= high.shift(1)) & (high.shift(2) >= high),
    }
    for z, price in ((1, low), (-1, high)):
        mask = masks[z] & continuous
        g[f"pivot_{z}"] = price.shift(2).where(mask)
        g[f"pivot_time_{z}"] = g.index.to_series().shift(2).where(mask)
    g["available_at"] = g.index + pd.Timedelta(minutes=minutes)
    return g


def closed_context(m1, decision):
    """Aggregate only complete real M1 groups; gaps never become fake bars."""
    m1 = m1.loc[m1.index + pd.Timedelta(minutes=1) <= decision]
    result = {}
    for tf, mins in TF.items():
        grouped = m1.resample(f"{mins}min", origin="epoch")
        bars = grouped.agg(dict(open="first", high="max", low="min", close="last", volume="sum"))
        bars = bars.loc[grouped.close.count().eq(mins)]
        result[tf] = pivots(bars, mins)
    return result


def context_query(g, minutes, decision, activation, fill, entry, risk, z):
    out = dict(vote=None, move=None, available_at=None, close=None, protected=None,
               protected_available_at=None, protected_kind=None, broken=None)
    g = g.loc[g.available_at <= decision]
    if g.empty:
        return out
    last = g.iloc[-1]
    out["available_at"] = last.available_at.isoformat()
    if (decision - last.available_at).total_seconds() >= minutes * 60:
        return out
    prev = g.close.shift(1)
    tr = pd.concat([g.high - g.low, (g.high - prev).abs(), (g.low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean().iloc[-1]
    sma = g.close.rolling(8, min_periods=8).mean().iloc[-1]
    move = z * (last.close - g.close.shift(3).iloc[-1]) / atr if finite(atr) and atr > 0 else None
    if finite(sma) and finite(move):
        side = z * (last.close - sma)
        out.update(vote=(-1 if move < -.25 and side < 0 else 1 if move > .25 and side > 0 else 0),
                   move=float(move))
    out["close"] = float(last.close)
    col, timecol = f"pivot_{z}", f"pivot_time_{z}"
    initial = g.loc[g[col].notna() & (g.available_at <= activation)
                    & (g[timecol] >= activation - pd.Timedelta(hours=24))
                    & (z * (entry - g[col]) > 0)]
    at_activation = g.loc[g.available_at <= activation]
    if not initial.empty and not at_activation.empty:
        initial = initial.loc[z * (at_activation.close.iloc[-1] - initial[col]) > 0].iloc[-1:]
    later = g.loc[g[col].notna() & (g[timecol] >= fill)]
    protected = pd.concat([initial, later])
    if not protected.empty:
        row = protected.loc[protected[col].idxmax() if z == 1 else protected[col].idxmin()]
        price = float(row[col])
        out.update(protected=price, protected_available_at=row.available_at.isoformat(),
                   protected_kind=("CONFIRMED_POST_FILL_PIVOT" if row[timecol] >= fill else
                                   "CONFIRMED_CONTEXT_PIVOT_AT_ACTIVATION"),
                   broken=bool(z * (last.close - price) / risk < -.05))
    return out


def evaluate(order, seconds, m1, decision):
    """Recompute the causal prefix; service persists at most one result/minute.

    No second gap is interpolated here. Sparse no-trade seconds require upstream
    reconciliation, not a guess. Return HOLD_DATA_GAP when coverage is unknown.
    """
    z = 1 if order["direction"] == "LONG" else -1
    fill = pd.Timestamp(order["fill_ms"], unit="ms", tz="UTC")
    activation = pd.Timestamp(order["activation_ms"], unit="ms", tz="UTC")
    entry, risk = float(order["entry_price"]), float(order["risk_points"])
    decision = pd.Timestamp(decision)
    f = seconds.loc[(seconds.index >= fill.floor("s")) &
                    (seconds.index + pd.Timedelta(seconds=1) <= decision)].copy()
    unknown = dict(exit=False, cause="HOLD_DATA_GAP", policy=POLICY, decision_ms=int(decision.timestamp()*1000))
    expected = pd.date_range(fill.floor("s"), decision - pd.Timedelta(seconds=1), freq="s")
    if len(f) < 60 or not f.index.equals(expected):
        return dict(unknown, observed_seconds=len(f), expected_seconds=len(expected))
    # Fill-second extremes before the confirmed fill are not post-fill evidence.
    first = float(f.close.iloc[0])
    f.iloc[0, f.columns.get_loc("high")] = max(entry, first)
    f.iloc[0, f.columns.get_loc("low")] = min(entry, first)
    history = m1.loc[m1.index + pd.Timedelta(minutes=1) <= fill].tail(21)
    prev = history.close.shift(1)
    tr = pd.concat([history.high-history.low, (history.high-prev).abs(), (history.low-prev).abs()], axis=1).max(axis=1).tail(20)
    atr = float(tr.mean()) if len(tr) == 20 and tr.gt(0).any() else None
    cl = f.close.to_numpy(float)
    progress = z * (cl - entry) / risk
    favorable = z * ((f.high if z == 1 else f.low).to_numpy(float) - entry) / risk
    mfe = np.maximum.accumulate(np.maximum(favorable, 0))
    armed = np.maximum.accumulate(mfe >= .5)
    returned = np.maximum.accumulate(armed & (progress <= .05))
    delta = np.diff(cl, prepend=cl[0])
    lag = np.r_[np.repeat(cl[0], 60), cl[:-60]]
    path = pd.Series(np.abs(delta)).rolling(60, min_periods=60).sum().to_numpy()
    efficiency = np.divide(z*(cl-lag), path, out=np.zeros_like(path), where=path > 0)
    speed = z*(cl-lag)/atr if finite(atr) and atr > 0 else np.full(len(cl), np.nan)
    rv = pd.Series(delta**2).rolling(60, min_periods=60).sum().pow(.5)
    rv_ref = rv.shift(60).rolling(300, min_periods=60).median()
    ratio = np.divide(rv.to_numpy(), rv_ref.to_numpy(), out=np.full(len(rv), np.nan), where=rv_ref.to_numpy() > 0)
    snap = order["snapshot"]
    source_r = z*(float(snap["source_level"])-entry)/risk
    edge_r = z*(float(snap["fvg_edge"])-entry)/risk
    dol = snap.get("frozen_dol") or {}
    dol_price = dol.get("price")
    delivered = bool(finite(dol_price) and ((f.high >= dol_price).any() if z == 1 else (f.low <= dol_price).any()))
    pre = m1.loc[(m1.index >= activation) & (m1.index + pd.Timedelta(minutes=1) <= fill)]
    if finite(dol_price) and not pre.empty:
        delivered |= bool((pre.high >= dol_price).any() if z == 1 else (pre.low <= dol_price).any())
    before_fill = seconds.loc[(seconds.index >= activation) & (seconds.index < fill.floor('s'))
                              & (seconds.index + pd.Timedelta(seconds=1) <= decision)]
    if finite(dol_price) and not before_fill.empty:
        delivered |= bool((before_fill.high >= dol_price).any() if z == 1 else (before_fill.low <= dol_price).any())
    boundaries = np.flatnonzero(f.index.second == 59)
    weak_streak = source_streak = 0
    for i in boundaries:
        if i == 0:
            continue
        weak = finite(speed[i]) and (speed[i] <= .10 or efficiency[i] <= .10 or (finite(ratio[i]) and ratio[i] < .75))
        weak_streak = weak_streak + 1 if weak else 0
        source_streak = source_streak + 1 if progress[i] < source_r - .05 else 0
    bars = f.resample("min").agg(dict(open="first", high="max", low="min", close="last", volume="sum"))
    bars = bars.loc[(bars.index >= fill.ceil("min")) & (bars.index+pd.Timedelta(minutes=1) <= decision)]
    p = pivots(bars, 1)[f"pivot_{z}"].dropna()
    trail = None if p.empty else float((z*(p-entry)/risk).max())
    fee = 3.5/(risk*2) + .25/risk
    m = dict(age_seconds=(decision-fill).total_seconds(), progress=float(progress[-1]), mfe=float(mfe[-1]),
             speed=float(speed[-1]) if finite(speed[-1]) else None, efficiency=float(efficiency[-1]),
             rv=float(ratio[-1]) if finite(ratio[-1]) else None, weak_streak=weak_streak,
             source_streak=source_streak, source_failed=bool(source_streak >= 2 and acceptance(progress, source_r)),
             failed_fvg=acceptance(progress, edge_r), failed_entry=acceptance(progress, 0),
             be_returned=bool(returned[-1]),
             m1_protect=bool(mfe[-1]>=1 and mfe[-1]-progress[-1]>=.5 and progress[-1]>fee
                             and trail is not None and trail>fee and acceptance(progress, trail)),
             dol_delivered=delivered)
    contexts = closed_context(m1, decision)
    ctx = {tf: context_query(g, TF[tf], decision, activation, fill, entry, risk, z) for tf,g in contexts.items()}
    verdict = decide(m, ctx)
    return dict(verdict, policy=POLICY, decision_ms=int(decision.timestamp()*1000),
                measurements=m, context=ctx, atr20=atr, trail_r=trail,
                frozen_dol=dict(dol, state="DELIVERED" if delivered else "OPEN" if finite(dol_price) else "UNKNOWN"))
