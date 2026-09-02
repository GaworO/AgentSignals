"""Causal ICT-style market context for the Monitor page.

This module is deliberately informational.  It never imports the executor, never
changes a signal and never returns an order quantity.  It combines three views:

* weekly market regime (trend/range x volatility),
* weekly directional narrative / draw on liquidity,
* daily bias refined by the current Globex trading day.

Every historical row is rebuilt only from bars available at that timestamp.  A
small JSONL audit trail records the exact Sunday and daily snapshots seen live.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


NY = ZoneInfo("America/New_York")
HISTORY_FILE = "market_context_history.jsonl"
_CACHE = {"key": None, "bars": None}
_CACHE_LOCK = threading.Lock()


def _num(value, digits=2):
    try:
        value = float(value)
        return round(value, digits) if math.isfinite(value) else None
    except Exception:
        return None


def _iso_date(value):
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    return str(value)


def _source_key(paths):
    out = []
    for raw in paths:
        p = os.path.abspath(str(raw))
        try:
            st = os.stat(p)
            out.append((p, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return tuple(out)


def load_bars(paths):
    """Load and de-duplicate OHLCV from seed/archive/buffer sources."""
    paths = [str(p) for p in paths if p and os.path.exists(str(p))]
    key = _source_key(paths)
    with _CACHE_LOCK:
        if key and key == _CACHE.get("key") and _CACHE.get("bars") is not None:
            return _CACHE["bars"].copy()

    frames = []
    required = ["ts_event", "open", "high", "low", "close"]
    for path in paths:
        try:
            cols = list(pd.read_csv(path, nrows=0).columns)
            if not all(c in cols for c in required):
                continue
            use = required + (["volume"] if "volume" in cols else [])
            part = pd.read_csv(path, usecols=use)
            if "volume" not in part:
                part["volume"] = 0.0
            frames.append(part)
        except Exception:
            continue
    if not frames:
        raise ValueError("no readable OHLCV source")

    bars = pd.concat(frames, ignore_index=True)
    bars["ts"] = pd.to_datetime(bars["ts_event"], utc=True, format="ISO8601", errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = (bars.dropna(subset=["ts", "open", "high", "low", "close"])
                 .drop_duplicates(subset=["ts"], keep="last")
                 .sort_values("ts").reset_index(drop=True))
    if bars.empty:
        raise ValueError("OHLCV sources are empty")

    local = bars["ts"].dt.tz_convert(NY)
    bars["ny"] = local
    bars["trading_date"] = (local.dt.normalize()
                            + pd.to_timedelta((local.dt.hour >= 18).astype(int), unit="D"))
    with _CACHE_LOCK:
        _CACHE["key"] = key
        _CACHE["bars"] = bars.copy()
    return bars


def _aggregate(bars):
    agg = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"),
               close=("close", "last"), volume=("volume", "sum"),
               first_ts=("ts", "first"), last_ts=("ts", "last"))
    daily = bars.groupby("trading_date", sort=True).agg(**agg)
    daily.index = pd.DatetimeIndex(daily.index).tz_localize(None)
    daily.index.name = "date"
    if daily.empty:
        return daily, daily
    week_end = daily.index.to_period("W-FRI").end_time.normalize()
    tmp = daily.copy(); tmp["week_end"] = week_end
    weekly = tmp.groupby("week_end", sort=True).agg(**{
        "open": ("open", "first"), "high": ("high", "max"), "low": ("low", "min"),
        "close": ("close", "last"), "volume": ("volume", "sum"),
        "first_ts": ("first_ts", "first"), "last_ts": ("last_ts", "last"),
    })
    weekly.index = pd.DatetimeIndex(weekly.index)
    # A week is complete only when its Friday is present.  On Sunday evening the
    # new Monday trade date is deliberately excluded from the weekly narrative.
    weekly = weekly[weekly.index <= daily.index.max()]
    # A Friday trading date starts on Thursday at 18:00 ET.  Do not let that
    # still-forming bar leak into the "closed week" narrative during Friday's
    # session.  The final 1-minute bar is normally stamped 16:59 ET; once a later
    # trade date exists the old week is complete regardless of holiday hours.
    if len(weekly) and weekly.index[-1] == daily.index[-1]:
        last_ts = pd.Timestamp(daily.iloc[-1]["last_ts"])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        last_ny = last_ts.tz_convert(NY)
        friday_closed = last_ny.weekday() == 4 and (last_ny.hour, last_ny.minute) >= (16, 59)
        if not friday_closed:
            weekly = weekly.iloc[:-1]
    return daily, weekly


def _true_range(frame):
    prev = frame["close"].shift(1)
    return pd.concat([(frame["high"] - frame["low"]),
                      (frame["high"] - prev).abs(),
                      (frame["low"] - prev).abs()], axis=1).max(axis=1)


def _adx(frame, period=14):
    if len(frame) < 4:
        return pd.Series(np.nan, index=frame.index)
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = _true_range(frame).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _percentile(series, lookback):
    s = pd.Series(series).dropna().iloc[-lookback:]
    if len(s) < 5:
        return None
    return float(100.0 * s.rank(pct=True).iloc[-1])


def _structure(frame, bars=3):
    if len(frame) < bars + 1:
        return "neutral"
    recent = frame.iloc[-bars:]
    if recent["high"].is_monotonic_increasing and recent["low"].is_monotonic_increasing:
        return "up"
    if recent["high"].is_monotonic_decreasing and recent["low"].is_monotonic_decreasing:
        return "down"
    return "neutral"


def _factor(label, direction, weight, detail):
    return {"label": label, "direction": direction, "weight": _num(weight, 2), "detail": detail}


def _liquidity_map(daily, weekly, price, atr):
    levels = []
    if len(weekly) >= 2:
        p = weekly.iloc[-2]
        levels += [("Previous Week High", float(p.high), "above"),
                   ("Previous Week Low", float(p.low), "below")]
    if len(weekly) >= 5:
        old = weekly.iloc[-5:-1]
        levels += [("4-Week High", float(old.high.max()), "above"),
                   ("4-Week Low", float(old.low.min()), "below")]
    if len(daily) >= 2:
        p = daily.iloc[-2]
        levels += [("Previous Day High", float(p.high), "above"),
                   ("Previous Day Low", float(p.low), "below")]
    if len(daily) >= 21:
        old = daily.iloc[-21:-1]
        levels += [("20-Day High", float(old.high.max()), "above"),
                   ("20-Day Low", float(old.low.min()), "below")]

    seen = set(); out = []
    for name, value, side in levels:
        key = (name, round(value, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "price": _num(value), "side": side,
                    "distance_atr": _num(abs(value - price) / atr if atr else None, 2),
                    "open": bool(value > price) if side == "above" else bool(value < price)})
    return out


def _pick_draw(levels, direction, price):
    side = "above" if direction == "BULLISH" else "below"
    candidates = [x for x in levels if x["side"] == side and x["open"]]
    if not candidates:
        candidates = [x for x in levels if x["side"] == side]
    if not candidates:
        return {"name": "No clean external draw", "price": None, "distance_atr": None}
    return min(candidates, key=lambda x: abs(float(x["price"]) - price))


def _regime(frame):
    tr = _true_range(frame)
    atr = tr.rolling(14, min_periods=5).mean()
    atr_now = float(atr.iloc[-1]) if len(atr) and pd.notna(atr.iloc[-1]) else float(tr.iloc[-5:].mean())
    vol_pct = _percentile(atr, 52)
    adx_s = _adx(frame, 14)
    adx_now = float(adx_s.iloc[-1]) if len(adx_s) and pd.notna(adx_s.iloc[-1]) else None
    ema = frame.close.ewm(span=13, adjust=False).mean()
    slope = float(ema.iloc[-1] - ema.iloc[-3]) if len(ema) >= 3 else 0.0
    direction = "up" if frame.close.iloc[-1] > ema.iloc[-1] and slope > 0 else (
        "down" if frame.close.iloc[-1] < ema.iloc[-1] and slope < 0 else "neutral")
    if adx_now is not None and adx_now >= 25 and direction != "neutral":
        trend = "trending"
    elif adx_now is not None and adx_now < 18:
        trend = "ranging"
    else:
        trend = "transitional"
    volatility = "low" if vol_pct is not None and vol_pct < 30 else (
        "high" if vol_pct is not None and vol_pct > 70 else "normal")
    range_ratio = float((frame.high.iloc[-1] - frame.low.iloc[-1]) /
                        max(frame.iloc[-20:].high.sub(frame.iloc[-20:].low).median(), 1e-9))
    delivery = "choppy" if trend == "ranging" and volatility == "high" else (
        "clean" if trend == "trending" and range_ratio >= 0.8 else "mixed")
    transition = "high" if trend == "transitional" or range_ratio > 1.8 else (
        "medium" if volatility == "high" or range_ratio < 0.65 else "low")
    return {"trend": trend, "direction": direction, "volatility": volatility,
            "delivery": delivery, "transition_risk": transition,
            "code": f"{volatility}_vol_{trend}", "atr": _num(atr_now),
            "atr_percentile": _num(vol_pct, 1), "adx": _num(adx_now, 1),
            "range_ratio": _num(range_ratio, 2)}


def _bias_label(score, threshold=1.25):
    if score >= threshold:
        return "BULLISH"
    if score <= -threshold:
        return "BEARISH"
    return "NEUTRAL"


def _confidence(score, factors, enough_data=True):
    directional = sum(abs(float(x["weight"] or 0)) for x in factors)
    if directional <= 0:
        return 30
    agreement = min(1.0, abs(score) / directional)
    value = 50 + 35 * agreement
    if not enough_data:
        value = min(value, 55)
    return int(round(max(30, min(85, value))))


def weekly_context(weekly, daily):
    if len(weekly) < 3 or len(daily) < 10:
        return {"ok": False, "error": "insufficient weekly history"}
    w = weekly.copy(); d = daily[daily.index <= w.index[-1]].copy()
    cur, prev = w.iloc[-1], w.iloc[-2]
    reg = _regime(w)
    factors = []
    score = 0.0

    struct = _structure(w, 3)
    if struct == "up":
        score += 1.5; factors.append(_factor("Weekly structure", "bullish", 1.5, "higher highs and higher lows"))
    elif struct == "down":
        score -= 1.5; factors.append(_factor("Weekly structure", "bearish", -1.5, "lower highs and lower lows"))
    else:
        factors.append(_factor("Weekly structure", "neutral", 0, "overlapping / mixed structure"))

    ema13 = w.close.ewm(span=13, adjust=False).mean()
    ema_up = cur.close > ema13.iloc[-1] and ema13.iloc[-1] > ema13.iloc[-3]
    ema_dn = cur.close < ema13.iloc[-1] and ema13.iloc[-1] < ema13.iloc[-3]
    if ema_up:
        score += 1.0; factors.append(_factor("Weekly order flow", "bullish", 1, "close above a rising 13-week EMA"))
    elif ema_dn:
        score -= 1.0; factors.append(_factor("Weekly order flow", "bearish", -1, "close below a falling 13-week EMA"))
    else:
        factors.append(_factor("Weekly order flow", "neutral", 0, "EMA and price do not agree"))

    if cur.low < prev.low and cur.close > prev.low:
        score += 1.25; factors.append(_factor("Weekly liquidity raid", "bullish", 1.25, "PWL swept and reclaimed"))
    elif cur.high > prev.high and cur.close < prev.high:
        score -= 1.25; factors.append(_factor("Weekly liquidity raid", "bearish", -1.25, "PWH swept and rejected"))

    if cur.close > prev.high:
        score += 0.75; factors.append(_factor("Weekly acceptance", "bullish", .75, "closed above PWH"))
    elif cur.close < prev.low:
        score -= 0.75; factors.append(_factor("Weekly acceptance", "bearish", -.75, "closed below PWL"))

    rng = max(float(cur.high - cur.low), 1e-9)
    close_location = float((cur.close - cur.low) / rng)
    if close_location >= .7:
        score += .5; factors.append(_factor("Weekly close", "bullish", .5, "closed in upper 30% of range"))
    elif close_location <= .3:
        score -= .5; factors.append(_factor("Weekly close", "bearish", -.5, "closed in lower 30% of range"))

    if len(d) >= 22:
        dema = d.close.ewm(span=20, adjust=False).mean()
        if d.close.iloc[-1] > dema.iloc[-1] and dema.iloc[-1] > dema.iloc[-5]:
            score += .75; factors.append(_factor("Daily order flow", "bullish", .75, "D1 above rising EMA20"))
        elif d.close.iloc[-1] < dema.iloc[-1] and dema.iloc[-1] < dema.iloc[-5]:
            score -= .75; factors.append(_factor("Daily order flow", "bearish", -.75, "D1 below falling EMA20"))

    direction = _bias_label(score)
    five = w.iloc[-5:]
    dealing_high, dealing_low = float(five.high.max()), float(five.low.min())
    equilibrium = (dealing_high + dealing_low) / 2.0
    location = "discount" if cur.close < equilibrium else "premium"
    factors.append(_factor("5-week dealing range", "neutral", 0,
                           f"price in {location}; EQ {equilibrium:.2f}"))

    levels = _liquidity_map(d, w, float(cur.close), float(reg["atr"] or 0))
    draw = _pick_draw(levels, direction, float(cur.close)) if direction != "NEUTRAL" else {
        "name": "Two-sided / wait for a raid", "price": _num(equilibrium), "distance_atr": None}
    invalidation = float(prev.low if direction == "BULLISH" else prev.high if direction == "BEARISH" else equilibrium)
    narrative = (f"Prefer delivery toward {draw['name']} while price holds the invalidation level. "
                 f"Regime is {reg['code'].replace('_', ' ')}; treat the opposite side as the alternate scenario.")
    return {"ok": True, "scope": "weekly", "as_of": _iso_date(w.index[-1]),
            "bias": direction, "score": _num(score, 2),
            "confidence": _confidence(score, factors, len(w) >= 20),
            "regime": reg, "close": _num(cur.close), "weekly_open": _num(cur.open),
            "dealing_range": {"high": _num(dealing_high), "low": _num(dealing_low),
                              "equilibrium": _num(equilibrium), "location": location},
            "primary_draw": draw, "invalidation": _num(invalidation),
            "factors": factors, "liquidity": levels, "narrative": narrative}


def _overnight(raw, trade_date):
    if raw is None or raw.empty:
        return None
    td = pd.Timestamp(trade_date)
    part = raw[raw["trading_date"].dt.tz_localize(None) == td]
    if part.empty:
        return None
    mins = part.ny.dt.hour * 60 + part.ny.dt.minute
    ov = part[(mins >= 18 * 60) | (mins < 9 * 60 + 30)]
    if ov.empty:
        return None
    return {"open": _num(ov.open.iloc[0]), "high": _num(ov.high.max()),
            "low": _num(ov.low.min()), "close": _num(ov.close.iloc[-1])}


def daily_context(daily, weekly, raw=None):
    if len(daily) < 6:
        return {"ok": False, "error": "insufficient daily history"}
    d = daily.copy(); cur, prev = d.iloc[-1], d.iloc[-2]
    # Weekly input must be completed before the current week's Friday.
    current_week_end = d.index[-1].to_period("W-FRI").end_time.normalize()
    prior_weeks = weekly[weekly.index < current_week_end]
    wctx = weekly_context(prior_weeks, d[d.index <= prior_weeks.index[-1]]) if len(prior_weeks) >= 3 else None
    factors = []; score = 0.0
    if wctx and wctx.get("ok"):
        if wctx["bias"] == "BULLISH":
            score += 1.25; factors.append(_factor("Weekly narrative", "bullish", 1.25, "weekly bias bullish"))
        elif wctx["bias"] == "BEARISH":
            score -= 1.25; factors.append(_factor("Weekly narrative", "bearish", -1.25, "weekly bias bearish"))
        else:
            factors.append(_factor("Weekly narrative", "neutral", 0, "weekly bias neutral"))

    ema20 = d.close.ewm(span=20, adjust=False).mean()
    if cur.close > ema20.iloc[-1] and ema20.iloc[-1] > ema20.iloc[max(0, len(ema20) - 5)]:
        score += 1; factors.append(_factor("Daily order flow", "bullish", 1, "above rising EMA20"))
    elif cur.close < ema20.iloc[-1] and ema20.iloc[-1] < ema20.iloc[max(0, len(ema20) - 5)]:
        score -= 1; factors.append(_factor("Daily order flow", "bearish", -1, "below falling EMA20"))
    else:
        factors.append(_factor("Daily order flow", "neutral", 0, "price and EMA slope disagree"))

    if cur.low < prev.low and cur.close > prev.low:
        score += 1.25; factors.append(_factor("PDL raid", "bullish", 1.25, "previous-day low swept and reclaimed"))
    elif cur.high > prev.high and cur.close < prev.high:
        score -= 1.25; factors.append(_factor("PDH raid", "bearish", -1.25, "previous-day high swept and rejected"))
    elif cur.close > prev.high:
        score += .75; factors.append(_factor("Daily acceptance", "bullish", .75, "accepted above PDH"))
    elif cur.close < prev.low:
        score -= .75; factors.append(_factor("Daily acceptance", "bearish", -.75, "accepted below PDL"))

    week_days = d[d.index.to_period("W-FRI") == d.index[-1].to_period("W-FRI")]
    week_open = float(week_days.open.iloc[0])
    if cur.close > week_open:
        score += .5; factors.append(_factor("Weekly open", "bullish", .5, "price above weekly open"))
    elif cur.close < week_open:
        score -= .5; factors.append(_factor("Weekly open", "bearish", -.5, "price below weekly open"))

    ov = _overnight(raw, d.index[-1])
    if ov:
        if cur.close > ov["high"]:
            score += .5; factors.append(_factor("Overnight range", "bullish", .5, "price above overnight high"))
        elif cur.close < ov["low"]:
            score -= .5; factors.append(_factor("Overnight range", "bearish", -.5, "price below overnight low"))
        else:
            factors.append(_factor("Overnight range", "neutral", 0, "price inside overnight range"))

    direction = _bias_label(score, 1.0)
    tr = _true_range(d); atr = float(tr.rolling(14, min_periods=5).mean().iloc[-1])
    levels = _liquidity_map(d, prior_weeks if len(prior_weeks) else weekly, float(cur.close), atr)
    draw = _pick_draw(levels, direction, float(cur.close)) if direction != "NEUTRAL" else {
        "name": "Wait for PDH/PDL or session raid", "price": None, "distance_atr": None}
    invalidation = float(prev.low if direction == "BULLISH" else prev.high if direction == "BEARISH"
                         else (prev.high + prev.low) / 2.0)
    return {"ok": True, "scope": "daily", "as_of": _iso_date(d.index[-1]),
            "bias": direction, "score": _num(score, 2),
            "confidence": _confidence(score, factors, len(d) >= 40),
            "regime": (wctx.get("regime") or {}) if wctx else {},
            "close": _num(cur.close), "weekly_open": _num(week_open),
            "previous_day": {"high": _num(prev.high), "low": _num(prev.low), "close": _num(prev.close)},
            "overnight": ov, "primary_draw": draw, "invalidation": _num(invalidation),
            "weekly_parent": {"bias": wctx.get("bias"), "as_of": wctx.get("as_of")} if wctx else None,
            "factors": factors, "liquidity": levels,
            "narrative": f"Daily context refines, but never overrides silently, the completed weekly narrative. Primary draw: {draw['name']}."}


def _history_row(snapshot, previous=None):
    if not snapshot or not snapshot.get("ok"):
        return None
    reg = snapshot.get("regime") or {}
    changed = bool(previous and (snapshot.get("bias") != previous.get("bias") or
                                 reg.get("code") != (previous.get("regime") or {}).get("code")))
    return {"date": snapshot.get("as_of"), "scope": snapshot.get("scope"),
            "bias": snapshot.get("bias"), "confidence": snapshot.get("confidence"),
            "score": snapshot.get("score"), "close": snapshot.get("close"),
            "regime": reg.get("code"), "trend": reg.get("trend"),
            "volatility": reg.get("volatility"), "transition_risk": reg.get("transition_risk"),
            "primary_draw": (snapshot.get("primary_draw") or {}).get("name"), "changed": changed}


def build_report(paths, daily_limit=90, weekly_limit=52, snapshot_file=None):
    """Return current weekly/daily context plus causal price history."""
    try:
        bars = load_bars(paths)
        daily, weekly = _aggregate(bars)
        if daily.empty:
            raise ValueError("no daily bars")
        wcur = weekly_context(weekly, daily) if len(weekly) >= 3 else {"ok": False, "error": "insufficient weekly history"}
        dcur = daily_context(daily, weekly, bars.iloc[-20000:])

        wh = []; prev = None
        start_w = max(0, len(weekly) - int(weekly_limit))
        for i in range(start_w, len(weekly)):
            snap = weekly_context(weekly.iloc[:i + 1], daily[daily.index <= weekly.index[i]])
            row = _history_row(snap, prev)
            if row: wh.append(row); prev = snap

        dh = []; prev = None
        start_d = max(0, len(daily) - int(daily_limit))
        for i in range(start_d, len(daily)):
            ds = daily.iloc[:i + 1]
            snap = daily_context(ds, weekly[weekly.index <= ds.index[-1]], None)
            row = _history_row(snap, prev)
            if row: dh.append(row); prev = snap

        recorded = read_recorded_history(snapshot_file) if snapshot_file else []
        latest_ts = bars.ts.iloc[-1]
        return {"ok": True, "informational_only": True,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "data": {"bars": int(len(bars)), "days": int(len(daily)), "weeks": int(len(weekly)),
                         "first_bar": bars.ts.iloc[0].isoformat(), "last_bar": latest_ts.isoformat(),
                         "sources": [os.path.basename(str(x)) for x in paths if x and os.path.exists(str(x))]},
                "weekly": wcur, "daily": dcur,
                "history": {"weekly": wh, "daily": dh, "recorded": recorded}}
    except Exception as exc:
        return {"ok": False, "informational_only": True,
                "error": f"{type(exc).__name__}: {exc}", "history": {"weekly": [], "daily": [], "recorded": []}}


def read_recorded_history(path):
    if not path or not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict): rows.append(row)
                except Exception:
                    continue
    except Exception:
        return []
    return rows[-400:]


def _append_snapshot(path, row):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_if_due(paths, data_dir, now=None):
    """Persist at most one daily (08:45 ET) and one Sunday (18:15 ET) snapshot.

    Missed runs are caught later in the same day/week.  Stale local data is never
    recorded as if it were a current live observation.
    """
    now = (now or dt.datetime.now(NY)).astimezone(NY)
    path = os.path.join(data_dir, HISTORY_FILE)
    existing = read_recorded_history(path)
    keys = {(x.get("kind"), x.get("key")) for x in existing}
    due = []
    if now.weekday() < 5 and (now.hour, now.minute) >= (8, 45):
        due.append(("daily", now.date().isoformat()))
    days_since_sunday = (now.weekday() + 1) % 7
    sunday = now.date() - dt.timedelta(days=days_since_sunday)
    sunday_due = dt.datetime.combine(sunday, dt.time(18, 15), tzinfo=NY)
    if now >= sunday_due:
        due.append(("weekly", (sunday + dt.timedelta(days=1)).isoformat()))
    due = [(kind, key) for kind, key in due if (kind, key) not in keys]
    if not due:
        return []

    report = build_report(paths, daily_limit=1, weekly_limit=1)
    if not report.get("ok"):
        return []
    try:
        last_bar = pd.Timestamp(report["data"]["last_bar"]).tz_convert(NY)
        if abs((now - last_bar.to_pydatetime()).total_seconds()) > 72 * 3600:
            return []
    except Exception:
        return []

    written = []
    for kind, key in due:
        snap = report.get(kind) or {}
        if not snap.get("ok"):
            continue
        row = {"kind": kind, "key": key, "captured_at": now.isoformat(),
               "informational_only": True, "snapshot": snap}
        _append_snapshot(path, row); written.append(row)
    return written
