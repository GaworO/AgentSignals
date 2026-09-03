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
import argparse
import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import market_models


NY = ZoneInfo("America/New_York")
HISTORY_FILE = "market_context_history.jsonl"
DATABASE_FILE = "market_history.db"
PREDICTION_DATABASE_FILE = "market_predictions.db"
DATABASE_SCHEMA = 1
PREDICTION_SCHEMA = 1
_CACHE = {"key": None, "bars": None}
_DB_CACHE = {"key": None, "daily": None, "meta": None}
_CACHE_LOCK = threading.Lock()
_PREDICTION_LOCK = threading.Lock()


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
            optional = [c for c in ("volume", "instrument_id", "symbol") if c in cols]
            use = required + optional
            part = pd.read_csv(path, usecols=use)
            if "volume" not in part:
                part["volume"] = 0.0
            if "instrument_id" not in part:
                part["instrument_id"] = np.nan
            if "symbol" not in part:
                part["symbol"] = ""
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
    # Convert to a timezone-naive *wall date* before adding the Globex rollover.
    # Adding a 24-hour Timedelta to tz-aware Sunday midnight crosses the spring
    # DST boundary at 01:00 and can manufacture two different "Monday" instants.
    wall_date = local.dt.tz_localize(None).dt.normalize()
    bars["trading_date"] = wall_date + pd.to_timedelta((local.dt.hour >= 18).astype(int), unit="D")
    with _CACHE_LOCK:
        _CACHE["key"] = key
        _CACHE["bars"] = bars.copy()
    return bars


def _session_values(bars, mask, prefix):
    part = bars.loc[mask]
    if part.empty:
        return pd.DataFrame()
    grouped = part.groupby("trading_date", sort=True).agg(
        **{f"{prefix}_open": ("open", "first"), f"{prefix}_high": ("high", "max"),
           f"{prefix}_low": ("low", "min"), f"{prefix}_close": ("close", "last")})
    grouped.index = pd.DatetimeIndex(grouped.index).tz_localize(None)
    return grouped


def _weekly_from_daily(daily):
    if daily.empty:
        return daily.copy()
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
    return weekly


def _aggregate(bars):
    agg = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"),
               close=("close", "last"), volume=("volume", "sum"),
               first_ts=("ts", "first"), last_ts=("ts", "last"),
               instrument_id=("instrument_id", "last"), symbol=("symbol", "last"))
    daily = bars.groupby("trading_date", sort=True).agg(**agg)
    daily.index = pd.DatetimeIndex(daily.index).tz_localize(None)
    daily.index.name = "date"
    if daily.empty:
        return daily, daily

    mins = bars["ny"].dt.hour * 60 + bars["ny"].dt.minute
    sessions = {
        "overnight": (mins >= 18 * 60) | (mins < 9 * 60 + 30),
        "asia": (mins >= 20 * 60),
        "london": (mins >= 2 * 60) & (mins < 5 * 60),
        "nyam": (mins >= 9 * 60 + 30) & (mins < 12 * 60),
    }
    for prefix, mask in sessions.items():
        values = _session_values(bars, mask, prefix)
        if not values.empty:
            daily = daily.join(values, how="left")
    daily["roll"] = (daily["instrument_id"].notna() & daily["instrument_id"].shift(1).notna() &
                     (daily["instrument_id"] != daily["instrument_id"].shift(1)))
    return daily, _weekly_from_daily(daily)


def build_history_database(source_path, database_path):
    """Build the compact deployable daily/session database from raw one-minute data."""
    bars = load_bars([source_path])
    daily, weekly = _aggregate(bars)
    out = daily.reset_index().copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    for col in ("first_ts", "last_ts"):
        out[col] = out[col].map(lambda value: pd.Timestamp(value).isoformat() if pd.notna(value) else None)
    out["roll"] = out["roll"].fillna(False).astype(int)
    meta = {
        "schema_version": str(DATABASE_SCHEMA),
        "source": os.path.basename(str(source_path)),
        "raw_bars": str(len(bars)),
        "daily_bars": str(len(daily)),
        "weekly_bars": str(len(weekly)),
        "first_bar": bars["ts"].iloc[0].isoformat(),
        "last_bar": bars["ts"].iloc[-1].isoformat(),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "roll_events": str(int(daily["roll"].sum())),
        "symbol": str(next((x for x in daily.get("symbol", pd.Series(dtype=str)).dropna().astype(str) if x), "MNQ")),
    }
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("DROP TABLE IF EXISTS daily_bars")
        conn.execute("DROP TABLE IF EXISTS market_meta")
        out.to_sql("daily_bars", conn, index=False, if_exists="replace")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_market_daily_date ON daily_bars(date)")
        conn.execute("CREATE TABLE market_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO market_meta(key,value) VALUES(?,?)", sorted(meta.items()))
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {"database": str(database_path), **meta}


def load_history_database(database_path):
    if not database_path or not os.path.exists(database_path):
        return pd.DataFrame(), {}
    stat = os.stat(database_path)
    key = (os.path.abspath(database_path), stat.st_size, stat.st_mtime_ns)
    with _CACHE_LOCK:
        if key == _DB_CACHE.get("key") and _DB_CACHE.get("daily") is not None:
            return _DB_CACHE["daily"].copy(), dict(_DB_CACHE.get("meta") or {})
    conn = sqlite3.connect(database_path)
    try:
        daily = pd.read_sql_query("SELECT * FROM daily_bars ORDER BY date", conn)
        meta = dict(conn.execute("SELECT key,value FROM market_meta").fetchall())
    finally:
        conn.close()
    if daily.empty:
        return daily, meta
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date"]).set_index("date").sort_index()
    for col in ("first_ts", "last_ts"):
        if col in daily:
            daily[col] = pd.to_datetime(daily[col], utc=True, errors="coerce")
    if "roll" in daily:
        daily["roll"] = daily["roll"].fillna(0).astype(bool)
    with _CACHE_LOCK:
        _DB_CACHE["key"] = key; _DB_CACHE["daily"] = daily.copy(); _DB_CACHE["meta"] = dict(meta)
    return daily, meta


def _combined_market_data(paths, database_path=None):
    historic, meta = load_history_database(database_path)
    raw = None; live_daily = pd.DataFrame()
    existing = [str(path) for path in paths if path and os.path.exists(str(path))]
    if existing:
        raw = load_bars(existing)
        live_daily, _ = _aggregate(raw)
    if historic.empty and live_daily.empty:
        raise ValueError("no readable OHLCV source or market history database")
    if historic.empty:
        daily = live_daily
    elif live_daily.empty:
        daily = historic
    else:
        # Live/seed data is appended last and therefore wins on overlapping dates.
        daily = pd.concat([historic, live_daily]).sort_index()
        daily = daily[~daily.index.duplicated(keep="last")]
    return daily, _weekly_from_daily(daily), raw, meta


def _completed_daily(daily):
    """Exclude a still-forming final Globex trading day from model training."""
    if daily.empty:
        return daily
    try:
        last_ts = pd.Timestamp(daily.iloc[-1]["last_ts"])
        if last_ts.tzinfo is None: last_ts = last_ts.tz_localize("UTC")
        local = last_ts.tz_convert(NY)
        complete = (local.date() == daily.index[-1].date() and
                    (local.hour, local.minute) >= (16, 59))
        return daily if complete else daily.iloc[:-1]
    except Exception:
        return daily.iloc[:-1] if len(daily) > 1 else daily


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


def _period_context(daily):
    if daily.empty:
        return {}
    d = daily.sort_index(); current_date = d.index[-1]
    month = current_date.to_period("M")
    quarter = current_date.to_period("Q")
    year = current_date.to_period("Y")
    current_month = d[d.index.to_period("M") == month]
    current_quarter = d[d.index.to_period("Q") == quarter]
    current_year = d[d.index.to_period("Y") == year]
    previous_months = d[d.index.to_period("M") < month]
    previous_quarters = d[d.index.to_period("Q") < quarter]
    out = {
        "monthly_open": _num(current_month.open.iloc[0]) if len(current_month) else None,
        "quarterly_open": _num(current_quarter.open.iloc[0]) if len(current_quarter) else None,
        "yearly_open": _num(current_year.open.iloc[0]) if len(current_year) else None,
    }
    if len(previous_months):
        pm = previous_months[previous_months.index.to_period("M") == previous_months.index[-1].to_period("M")]
        out["previous_month"] = {"high": _num(pm.high.max()), "low": _num(pm.low.min()),
                                 "close": _num(pm.close.iloc[-1]), "month": str(pm.index[-1].to_period("M"))}
    if len(previous_quarters):
        pq = previous_quarters[previous_quarters.index.to_period("Q") == previous_quarters.index[-1].to_period("Q")]
        out["previous_quarter"] = {"high": _num(pq.high.max()), "low": _num(pq.low.min()),
                                   "close": _num(pq.close.iloc[-1]), "quarter": str(pq.index[-1].to_period("Q"))}
    return out


def _opening_gaps(daily):
    if len(daily) < 2:
        return {"ndog": None, "nwog": None}
    d = daily.sort_index(); cur, prev = d.iloc[-1], d.iloc[-2]
    nd_lo, nd_hi = sorted((float(prev.close), float(cur.open)))
    if cur.open > prev.close:
        nd_open = bool(cur.low > prev.close)
    elif cur.open < prev.close:
        nd_open = bool(cur.high < prev.close)
    else:
        nd_open = False
    ndog = {"name": "NDOG", "from": _num(prev.close), "to": _num(cur.open),
            "low": _num(nd_lo), "high": _num(nd_hi), "size": _num(abs(cur.open - prev.close)),
            "direction": "up" if cur.open > prev.close else "down" if cur.open < prev.close else "flat",
            "open": nd_open}
    current_period = d.index[-1].to_period("W-FRI")
    this_week = d[d.index.to_period("W-FRI") == current_period]
    earlier = d[d.index.to_period("W-FRI") < current_period]
    nwog = None
    if len(this_week) and len(earlier):
        previous_period = earlier.index[-1].to_period("W-FRI")
        previous_week = earlier[earlier.index.to_period("W-FRI") == previous_period]
        start, end = float(this_week.open.iloc[0]), float(previous_week.close.iloc[-1])
        lo, hi = sorted((start, end))
        if start > end:
            nw_open = bool(this_week.low.min() > end)
        elif start < end:
            nw_open = bool(this_week.high.max() < end)
        else:
            nw_open = False
        nwog = {"name": "NWOG", "from": _num(end), "to": _num(start), "low": _num(lo), "high": _num(hi),
                "size": _num(abs(start - end)), "direction": "up" if start > end else "down" if start < end else "flat",
                "open": nw_open}
    return {"ndog": ndog, "nwog": nwog}


def _pd_arrays(frame, lookback=80):
    if len(frame) < 3:
        return {"active_fvgs": [], "last_displacement": None}
    f = frame.sort_index(); tr = _true_range(f); atr = tr.rolling(14, min_periods=5).mean()
    fvgs = []
    for i in range(max(2, len(f) - lookback), len(f)):
        if float(f.low.iloc[i]) > float(f.high.iloc[i - 2]):
            lo, hi, side = float(f.high.iloc[i - 2]), float(f.low.iloc[i]), "bullish"
            future = f.iloc[i + 1:]
            active = future.empty or float(future.low.min()) > lo
        elif float(f.high.iloc[i]) < float(f.low.iloc[i - 2]):
            lo, hi, side = float(f.high.iloc[i]), float(f.low.iloc[i - 2]), "bearish"
            future = f.iloc[i + 1:]
            active = future.empty or float(future.high.max()) < hi
        else:
            continue
        if active:
            fvgs.append({"date": _iso_date(f.index[i]), "side": side, "low": _num(lo), "high": _num(hi),
                         "midpoint": _num((lo + hi) / 2.0), "size": _num(hi - lo)})
    displacements = []
    for i in range(max(1, len(f) - lookback), len(f)):
        if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
            continue
        body = abs(float(f.close.iloc[i] - f.open.iloc[i])); candle = max(float(f.high.iloc[i] - f.low.iloc[i]), 1e-9)
        if body >= float(atr.iloc[i]) and body / candle >= 0.6:
            displacements.append({"date": _iso_date(f.index[i]),
                                  "side": "bullish" if f.close.iloc[i] > f.open.iloc[i] else "bearish",
                                  "body_atr": _num(body / float(atr.iloc[i]), 2),
                                  "body_pct_range": _num(100.0 * body / candle, 1)})
    return {"active_fvgs": fvgs[-5:], "last_displacement": displacements[-1] if displacements else None}


def _session_context(daily):
    if daily.empty:
        return {}
    row = daily.iloc[-1]; out = {}
    labels = {"asia": "Asia 20:00–00:00 ET", "london": "London 02:00–05:00 ET",
              "nyam": "New York AM 09:30–12:00 ET", "overnight": "Overnight 18:00–09:30 ET"}
    for prefix, label in labels.items():
        high, low = row.get(f"{prefix}_high"), row.get(f"{prefix}_low")
        if pd.notna(high) and pd.notna(low):
            out[prefix] = {"label": label, "high": _num(high), "low": _num(low),
                           "open": _num(row.get(f"{prefix}_open")), "close": _num(row.get(f"{prefix}_close"))}
    return out


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
    periods = _period_context(daily)
    pm = periods.get("previous_month") or {}
    pq = periods.get("previous_quarter") or {}
    if pm:
        levels += [("Previous Month High", float(pm["high"]), "above"),
                   ("Previous Month Low", float(pm["low"]), "below")]
    if pq:
        levels += [("Previous Quarter High", float(pq["high"]), "above"),
                   ("Previous Quarter Low", float(pq["low"]), "below")]

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


def _hurst_rs(series, max_window=100):
    values = pd.Series(series).dropna().astype(float).iloc[-max_window:]
    if len(values) < 50:
        return None
    windows = [8, 12, 16, 24, 32]
    points = []
    for window in windows:
        if window >= len(values) // 2:
            continue
        rs = []
        for start in range(0, len(values) - window + 1, window):
            chunk = values.iloc[start:start + window]
            dev = chunk - chunk.mean(); sigma = float(chunk.std(ddof=1))
            if sigma > 1e-12:
                accumulated = dev.cumsum()
                rs.append(float((accumulated.max() - accumulated.min()) / sigma))
        if rs:
            points.append((window, float(np.mean(rs))))
    if len(points) < 3:
        return None
    slope = float(np.polyfit(np.log([x[0] for x in points]), np.log([x[1] for x in points]), 1)[0])
    return max(0.0, min(1.0, slope))


def _recent_cusum(returns, baseline=63, threshold=3.0):
    r = pd.Series(returns).dropna().astype(float)
    if len(r) < baseline + 5:
        return {"active": False, "direction": None, "bars_ago": None}
    pos = neg = 0.0; changes = []
    for i in range(baseline, len(r)):
        past = r.iloc[i - baseline:i]
        std = float(past.std(ddof=1))
        if std <= 1e-12:
            continue
        z = float((r.iloc[i] - past.mean()) / std)
        pos = max(0.0, pos + z - 0.5); neg = max(0.0, neg - z - 0.5)
        if pos >= threshold:
            changes.append((i, "up")); pos = neg = 0.0
        elif neg >= threshold:
            changes.append((i, "down")); pos = neg = 0.0
    if not changes:
        return {"active": False, "direction": None, "bars_ago": None}
    idx, direction = changes[-1]; bars_ago = len(r) - 1 - idx
    return {"active": bool(bars_ago <= 3), "direction": direction, "bars_ago": int(bars_ago)}


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
    returns = np.log(frame.close.astype(float) / frame.close.astype(float).shift(1))
    rv20 = float(returns.rolling(20, min_periods=15).std().iloc[-1] * np.sqrt(252.0) * 100.0) if len(frame) >= 15 else None
    mid = frame.close.rolling(20, min_periods=15).mean()
    std = frame.close.rolling(20, min_periods=15).std()
    bb_width = (4.0 * std) / mid.replace(0, np.nan)
    bb_pct = _percentile(bb_width, 252)
    volume_pct = _percentile(frame.volume.astype(float), 252) if "volume" in frame else None
    efficiency = (abs(float(frame.close.iloc[-1] - frame.close.iloc[max(0, len(frame) - 21)])) /
                  max(float(frame.close.diff().abs().iloc[-20:].sum()), 1e-9)) if len(frame) >= 5 else None
    hurst = _hurst_rs(returns, 100)
    mean_reversion = "mean_reverting" if hurst is not None and hurst < .4 else (
        "persistent" if hurst is not None and hurst > .6 else "random_like")
    cusum = _recent_cusum(returns)
    delivery = "choppy" if trend == "ranging" and volatility == "high" else (
        "clean" if trend == "trending" and range_ratio >= 0.8 else "mixed")
    transition = "high" if trend == "transitional" or range_ratio > 1.8 or cusum["active"] else (
        "medium" if volatility == "high" or range_ratio < 0.65 else "low")
    return {"trend": trend, "direction": direction, "volatility": volatility,
            "delivery": delivery, "transition_risk": transition,
            "code": f"{volatility}_vol_{trend}", "atr": _num(atr_now),
            "atr_percentile": _num(vol_pct, 1), "adx": _num(adx_now, 1),
            "range_ratio": _num(range_ratio, 2), "realized_vol_20": _num(rv20, 1),
            "bb_width_percentile": _num(bb_pct, 1), "volume_percentile": _num(volume_pct, 1),
            "trend_efficiency": _num(efficiency, 2), "hurst": _num(hurst, 2),
            "mean_reversion": mean_reversion, "change_point": cusum}


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

    periods = _period_context(d)
    month_open, quarter_open = periods.get("monthly_open"), periods.get("quarterly_open")
    if month_open is not None and quarter_open is not None and cur.close > month_open and cur.close > quarter_open:
        score += .5; factors.append(_factor("Higher-timeframe opens", "bullish", .5, "above monthly and quarterly opens"))
    elif month_open is not None and quarter_open is not None and cur.close < month_open and cur.close < quarter_open:
        score -= .5; factors.append(_factor("Higher-timeframe opens", "bearish", -.5, "below monthly and quarterly opens"))
    else:
        factors.append(_factor("Higher-timeframe opens", "neutral", 0, "mixed around monthly/quarterly opens"))

    arrays = _pd_arrays(w, 40)
    active_fvg = arrays["active_fvgs"][-1] if arrays["active_fvgs"] else None
    if active_fvg and active_fvg["side"] == "bullish" and cur.close > active_fvg["midpoint"]:
        score += .5; factors.append(_factor("Weekly FVG", "bullish", .5, "active bullish imbalance remains below price"))
    elif active_fvg and active_fvg["side"] == "bearish" and cur.close < active_fvg["midpoint"]:
        score -= .5; factors.append(_factor("Weekly FVG", "bearish", -.5, "active bearish imbalance remains above price"))

    direction = _bias_label(score)
    five = w.iloc[-5:]
    dealing_high, dealing_low = float(five.high.max()), float(five.low.min())
    equilibrium = (dealing_high + dealing_low) / 2.0
    location = "discount" if cur.close < equilibrium else "premium"
    factors.append(_factor("5-week dealing range", "neutral", 0,
                           f"price in {location}; EQ {equilibrium:.2f}"))

    levels = _liquidity_map(d, w, float(cur.close), float(reg["atr"] or 0))
    gaps = _opening_gaps(d)
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
            "factors": factors, "liquidity": levels, "narrative": narrative,
            "ict": {"higher_timeframe": periods, "opening_gaps": gaps,
                    "pd_arrays": arrays, "sessions": _session_context(d)}}


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

    sessions = _session_context(d)
    ov = _overnight(raw, d.index[-1]) or sessions.get("overnight")
    if ov:
        if cur.close > ov["high"]:
            score += .5; factors.append(_factor("Overnight range", "bullish", .5, "price above overnight high"))
        elif cur.close < ov["low"]:
            score -= .5; factors.append(_factor("Overnight range", "bearish", -.5, "price below overnight low"))
        else:
            factors.append(_factor("Overnight range", "neutral", 0, "price inside overnight range"))

    periods = _period_context(d)
    month_open, quarter_open = periods.get("monthly_open"), periods.get("quarterly_open")
    if month_open is not None and quarter_open is not None and cur.close > month_open and cur.close > quarter_open:
        score += .5; factors.append(_factor("HTF opens", "bullish", .5, "above monthly and quarterly opens"))
    elif month_open is not None and quarter_open is not None and cur.close < month_open and cur.close < quarter_open:
        score -= .5; factors.append(_factor("HTF opens", "bearish", -.5, "below monthly and quarterly opens"))

    asia, london = sessions.get("asia"), sessions.get("london")
    if asia and london and cur.close > max(asia["high"], london["high"]):
        score += .5; factors.append(_factor("Session delivery", "bullish", .5, "above Asia and London highs"))
    elif asia and london and cur.close < min(asia["low"], london["low"]):
        score -= .5; factors.append(_factor("Session delivery", "bearish", -.5, "below Asia and London lows"))

    arrays = _pd_arrays(d, 60)
    active_fvg = arrays["active_fvgs"][-1] if arrays["active_fvgs"] else None
    if active_fvg and active_fvg["side"] == "bullish" and cur.close > active_fvg["midpoint"]:
        score += .5; factors.append(_factor("Daily FVG", "bullish", .5, "active bullish imbalance supports price"))
    elif active_fvg and active_fvg["side"] == "bearish" and cur.close < active_fvg["midpoint"]:
        score -= .5; factors.append(_factor("Daily FVG", "bearish", -.5, "active bearish imbalance caps price"))

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
            "close": _num(cur.close), "atr": _num(atr), "weekly_open": _num(week_open),
            "previous_day": {"high": _num(prev.high), "low": _num(prev.low), "close": _num(prev.close)},
            "overnight": ov, "primary_draw": draw, "invalidation": _num(invalidation),
            "weekly_parent": {"bias": wctx.get("bias"), "as_of": wctx.get("as_of")} if wctx else None,
            "factors": factors, "liquidity": levels,
            "narrative": f"Daily context refines, but never overrides silently, the completed weekly narrative. Primary draw: {draw['name']}.",
            "ict": {"higher_timeframe": periods, "opening_gaps": _opening_gaps(d),
                    "pd_arrays": arrays, "sessions": sessions}}


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


def build_report(paths, daily_limit=90, weekly_limit=52, snapshot_file=None,
                 database_path=None, news=None, prediction_database_path=None):
    """Return current context, causal history, statistical models and news risk."""
    try:
        daily, weekly, bars, db_meta = _combined_market_data(paths, database_path)
        if daily.empty:
            raise ValueError("no daily bars")
        wcur = weekly_context(weekly, daily) if len(weekly) >= 3 else {"ok": False, "error": "insufficient weekly history"}
        recent_raw = bars.iloc[-20000:] if bars is not None and not bars.empty else None
        dcur = daily_context(daily, weekly, recent_raw)

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
        prediction_journal = read_prediction_journal(prediction_database_path) if prediction_database_path else {
            "ok": True, "status": "not_configured", "rows": [], "summary": _empty_prediction_summary()}
        model_daily = _completed_daily(daily)
        models = market_models.classify_market(model_daily, dcur.get("bias") if dcur.get("ok") else None)
        raw_last = bars.ts.iloc[-1] if bars is not None and not bars.empty else None
        db_last = pd.Timestamp(db_meta.get("last_bar")) if db_meta.get("last_bar") else None
        latest_ts = max([x for x in (raw_last, db_last) if x is not None])
        first_candidates = []
        if bars is not None and not bars.empty: first_candidates.append(bars.ts.iloc[0])
        if db_meta.get("first_bar"): first_candidates.append(pd.Timestamp(db_meta["first_bar"]))
        first_ts = min(first_candidates)
        historic_bars = int(db_meta.get("raw_bars", 0) or 0)
        new_bars = 0
        if bars is not None and not bars.empty and db_last is not None:
            new_bars = int((bars.ts > db_last).sum())
        elif bars is not None:
            new_bars = int(len(bars))
        total_bars = historic_bars + new_bars if historic_bars else new_bars
        source_names = ([os.path.basename(str(database_path))] if database_path and os.path.exists(database_path) else [])
        source_names += [os.path.basename(str(x)) for x in paths if x and os.path.exists(str(x))]
        age_minutes = (dt.datetime.now(dt.timezone.utc) - latest_ts.to_pydatetime()).total_seconds() / 60.0
        return {"ok": True, "informational_only": True,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "data": {"bars": int(total_bars), "days": int(len(daily)), "weeks": int(len(weekly)),
                         "first_bar": first_ts.isoformat(), "last_bar": latest_ts.isoformat(),
                         "sources": list(dict.fromkeys(source_names)),
                         "history_database": bool(db_meta), "database_meta": db_meta,
                         "roll_events": int(daily.get("roll", pd.Series(dtype=bool)).fillna(False).sum()),
                         "age_minutes": _num(age_minutes, 1), "stale": bool(age_minutes > 30),
                         "model_last_completed_day": _iso_date(model_daily.index[-1]) if len(model_daily) else None,
                         "latest_day_complete": bool(len(model_daily) == len(daily))},
                "weekly": wcur, "daily": dcur,
                "classification": models, "news": news or {"ok": False, "status": "not_supplied", "events": []},
                "history": {"weekly": wh, "daily": dh, "recorded": recorded},
                "prediction_journal": prediction_journal}
    except Exception as exc:
        return {"ok": False, "informational_only": True,
                "error": f"{type(exc).__name__}: {exc}", "history": {"weekly": [], "daily": [], "recorded": []},
                "prediction_journal": {"ok": False, "status": "report_error", "rows": [],
                                       "summary": _empty_prediction_summary()}}


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


def _empty_prediction_summary():
    metric = lambda: {"samples": 0, "correct": 0, "accuracy": None}
    return {"total": 0, "pending": 0, "settled": 0,
            "rule_daily": metric(), "rule_weekly": metric(), "ai_daily": metric()}


def _prediction_connect(path):
    if not path:
        raise ValueError("prediction database path is required")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_version INTEGER NOT NULL DEFAULT 1,
            scope TEXT NOT NULL CHECK(scope IN ('daily','weekly')),
            forecast_key TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            rule_as_of TEXT,
            target_date TEXT NOT NULL,
            capture_price REAL,
            reference_atr REAL,
            rule_bias TEXT,
            rule_confidence REAL,
            rule_score REAL,
            rule_factors_json TEXT,
            primary_draw TEXT,
            invalidation REAL,
            weekly_parent_bias TEXT,
            regime_code TEXT,
            regime_json TEXT,
            hmm_status TEXT,
            hmm_as_of TEXT,
            hmm_code TEXT,
            hmm_label TEXT,
            hmm_confidence REAL,
            hmm_probabilities_json TEXT,
            ai_status TEXT,
            ai_as_of TEXT,
            ai_prediction TEXT,
            ai_confidence REAL,
            ai_probabilities_json TEXT,
            ai_validated INTEGER,
            ai_validation_json TEXT,
            news_risk TEXT,
            news_events_json TEXT,
            actual_date TEXT,
            actual_close REAL,
            actual_move_points REAL,
            actual_move_pct REAL,
            actual_move_atr REAL,
            actual_label TEXT,
            rule_correct INTEGER,
            ai_actual_date TEXT,
            ai_actual_close REAL,
            ai_actual_label TEXT,
            ai_correct INTEGER,
            settled_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(scope, forecast_key)
        );
        CREATE INDEX IF NOT EXISTS idx_market_predictions_status
            ON market_predictions(status, target_date);
        CREATE TABLE IF NOT EXISTS market_prediction_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def _json_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _prediction_keys(database_path):
    if not database_path:
        return set()
    try:
        with _PREDICTION_LOCK:
            conn = _prediction_connect(database_path)
            try:
                return {(row[0], row[1]) for row in conn.execute(
                    "SELECT scope,forecast_key FROM market_predictions")}
            finally:
                conn.close()
    except Exception:
        return set()


def record_prediction(database_path, report, scope, forecast_key, captured_at):
    """Insert one immutable forecast. Repeated scheduler calls are idempotent."""
    if scope not in ("daily", "weekly") or not report or not report.get("ok"):
        return False
    snap = report.get(scope) or {}
    if not snap.get("ok"):
        return False
    # A daily forecast must describe today's live trading date. If the feed is
    # still stuck on Friday, Monday must remain unrecorded rather than backfilled.
    if scope == "daily" and snap.get("as_of") != forecast_key:
        return False
    try:
        key_date = dt.date.fromisoformat(str(forecast_key))
    except Exception:
        return False
    target_date = key_date if scope == "daily" else key_date + dt.timedelta(days=4)
    captured = captured_at.isoformat() if isinstance(captured_at, dt.datetime) else str(captured_at)
    models = report.get("classification") or {}
    hmm, ai = models.get("hmm") or {}, models.get("ai") or {}
    regime = snap.get("regime") or {}
    news = report.get("news") or {}
    reference_atr = snap.get("atr") if scope == "daily" else regime.get("atr")
    parent = snap.get("weekly_parent") or {}
    values = {
        "journal_version": PREDICTION_SCHEMA, "scope": scope,
        "forecast_key": forecast_key, "captured_at": captured,
        "rule_as_of": snap.get("as_of"), "target_date": target_date.isoformat(),
        "capture_price": snap.get("close"), "reference_atr": reference_atr,
        "rule_bias": snap.get("bias"), "rule_confidence": snap.get("confidence"),
        "rule_score": snap.get("score"), "rule_factors_json": _json_value(snap.get("factors") or []),
        "primary_draw": (snap.get("primary_draw") or {}).get("name"),
        "invalidation": snap.get("invalidation"),
        "weekly_parent_bias": parent.get("bias") if scope == "daily" else None,
        "regime_code": regime.get("code"), "regime_json": _json_value(regime),
        "hmm_status": hmm.get("status"), "hmm_as_of": hmm.get("as_of"),
        "hmm_code": hmm.get("code"), "hmm_label": hmm.get("label"),
        "hmm_confidence": hmm.get("confidence"),
        "hmm_probabilities_json": _json_value(hmm.get("probabilities") or []),
        "ai_status": ai.get("status"), "ai_as_of": ai.get("as_of"),
        "ai_prediction": ai.get("prediction"), "ai_confidence": ai.get("confidence"),
        "ai_probabilities_json": _json_value(ai.get("probabilities") or {}),
        "ai_validated": int(bool(ai.get("validated"))) if ai.get("ok") else None,
        "ai_validation_json": _json_value(ai.get("validation") or {}),
        "news_risk": news.get("risk_level"),
        "news_events_json": _json_value((news.get("events") or [])[:20]),
    }
    columns = list(values)
    sql = ("INSERT OR IGNORE INTO market_predictions (" + ",".join(columns) + ") VALUES (" +
           ",".join("?" for _ in columns) + ")")
    try:
        with _PREDICTION_LOCK:
            conn = _prediction_connect(database_path)
            try:
                before = conn.total_changes
                conn.execute(sql, [values[column] for column in columns])
                conn.commit()
                return conn.total_changes > before
            finally:
                conn.close()
    except Exception:
        return False


def _score_summary(rows, scope, field):
    usable = [row for row in rows if row["scope"] == scope and row[field] is not None]
    correct = sum(int(row[field]) for row in usable)
    return {"samples": len(usable), "correct": correct,
            "accuracy": _num(100.0 * correct / len(usable), 1) if usable else None}


def read_prediction_journal(database_path, limit=120):
    """Read live-captured forecasts only; no reconstructed historical predictions."""
    if not database_path:
        return {"ok": True, "status": "not_configured", "rows": [],
                "summary": _empty_prediction_summary()}
    try:
        with _PREDICTION_LOCK:
            conn = _prediction_connect(database_path)
            try:
                compact = conn.execute(
                    "SELECT scope,status,rule_correct,ai_correct,captured_at FROM market_predictions "
                    "ORDER BY captured_at").fetchall()
                rows = conn.execute("SELECT * FROM market_predictions ORDER BY captured_at DESC LIMIT ?",
                                    (max(1, min(int(limit), 500)),)).fetchall()
            finally:
                conn.close()
        compact = [dict(row) for row in compact]
        summary = {"total": len(compact),
                   "pending": sum(row["status"] != "settled" for row in compact),
                   "settled": sum(row["status"] == "settled" for row in compact),
                   "rule_daily": _score_summary(compact, "daily", "rule_correct"),
                   "rule_weekly": _score_summary(compact, "weekly", "rule_correct"),
                   "ai_daily": _score_summary(compact, "daily", "ai_correct")}
        output = []
        json_columns = ("rule_factors_json", "regime_json", "hmm_probabilities_json",
                        "ai_probabilities_json", "ai_validation_json", "news_events_json")
        for raw in rows:
            row = dict(raw)
            for column in json_columns:
                name = column[:-5]
                try: row[name] = json.loads(row.pop(column) or "null")
                except Exception: row[name] = None; row.pop(column, None)
            for column in ("ai_validated", "rule_correct", "ai_correct"):
                if row.get(column) is not None: row[column] = bool(row[column])
            output.append(row)
        return {"ok": True, "status": "ready", "database": os.path.basename(str(database_path)),
                "collection_started_at": compact[0]["captured_at"] if compact else None,
                "rows": output, "summary": summary,
                "method": {"daily": "first live snapshot between 08:45 and 10:00 ET to same trading-day close; ±0.25 daily ATR neutral band",
                           "weekly": "Sunday 18:15 ET plan (Monday premarket fallback) from prior close to the coming week's final close; ±0.25 weekly ATR neutral band",
                           "ai": "exact next-day label used by the classifier; HMM is archived as context and is not scored"}}
    except Exception as exc:
        return {"ok": False, "status": "storage_error", "error": f"{type(exc).__name__}: {exc}",
                "rows": [], "summary": _empty_prediction_summary()}


def _move_label(move_atr):
    if move_atr is None:
        return None
    if move_atr < -0.25:
        return "BEARISH"
    if move_atr > 0.25:
        return "BULLISH"
    return "NEUTRAL"


def settle_prediction_journal(database_path, daily, now=None):
    """Attach later closes/outcomes without changing the original forecast fields."""
    now = now or dt.datetime.now(NY)
    if now.tzinfo is None: now = now.replace(tzinfo=NY)
    now = now.astimezone(NY)
    completed = _completed_daily(daily.sort_index())
    if completed.empty:
        return 0
    features = market_models.build_features(completed)
    with _PREDICTION_LOCK:
        conn = _prediction_connect(database_path)
        try:
            pending = conn.execute("SELECT * FROM market_predictions WHERE status!='settled' ORDER BY captured_at").fetchall()
            newly_settled = 0
            for raw in pending:
                row = dict(raw)
                actual_date = row.get("actual_date")
                actual_close = row.get("actual_close")
                actual_move_points = row.get("actual_move_points")
                actual_move_pct = row.get("actual_move_pct")
                actual_move_atr = row.get("actual_move_atr")
                actual_label = row.get("actual_label")
                rule_correct = row.get("rule_correct")

                if not actual_label:
                    target = pd.Timestamp(row["target_date"])
                    actual_row = None
                    if row["scope"] == "daily" and target in completed.index:
                        actual_row = completed.loc[target]
                        actual_date = target.date().isoformat()
                    elif row["scope"] == "weekly":
                        start = pd.Timestamp(row["forecast_key"])
                        candidates = completed[(completed.index >= start) & (completed.index <= target)]
                        target_week_finished = now.date() > target.date() or target in completed.index
                        if target_week_finished and len(candidates):
                            actual_row = candidates.iloc[-1]
                            actual_date = candidates.index[-1].date().isoformat()
                    if actual_row is not None and row.get("capture_price") is not None:
                        actual_close = float(actual_row["close"])
                        actual_move_points = actual_close - float(row["capture_price"])
                        actual_move_pct = (100.0 * actual_move_points / float(row["capture_price"])
                                           if float(row["capture_price"]) else None)
                        atr = float(row["reference_atr"] or 0)
                        actual_move_atr = actual_move_points / atr if atr > 0 else None
                        actual_label = _move_label(actual_move_atr)
                        rule_correct = (int(actual_label == row.get("rule_bias"))
                                        if actual_label and row.get("rule_bias") else None)

                ai_actual_date = row.get("ai_actual_date")
                ai_actual_close = row.get("ai_actual_close")
                ai_actual_label = row.get("ai_actual_label")
                ai_correct = row.get("ai_correct")
                if row["scope"] == "daily" and row.get("ai_prediction") and not ai_actual_label:
                    try:
                        model_date = pd.Timestamp(row.get("ai_as_of"))
                        target_value = features.loc[model_date, "target"]
                        future = completed[completed.index > model_date]
                        if pd.notna(target_value) and len(future):
                            ai_actual_label = market_models.AI_CLASSES[int(target_value)]
                            ai_actual_date = future.index[0].date().isoformat()
                            ai_actual_close = float(future.iloc[0]["close"])
                            ai_correct = int(ai_actual_label == row.get("ai_prediction"))
                    except Exception:
                        pass

                rule_done = bool(actual_label)
                ai_needed = row["scope"] == "daily" and bool(row.get("ai_prediction"))
                status = "settled" if rule_done and (not ai_needed or bool(ai_actual_label)) else "pending"
                settled_at = now.isoformat() if status == "settled" else row.get("settled_at")
                conn.execute("""
                    UPDATE market_predictions SET
                        actual_date=?,actual_close=?,actual_move_points=?,actual_move_pct=?,
                        actual_move_atr=?,actual_label=?,rule_correct=?,ai_actual_date=?,
                        ai_actual_close=?,ai_actual_label=?,ai_correct=?,settled_at=?,status=?
                    WHERE id=?
                """, (actual_date, _num(actual_close), _num(actual_move_points), _num(actual_move_pct, 3),
                      _num(actual_move_atr, 3), actual_label, rule_correct, ai_actual_date,
                      _num(ai_actual_close), ai_actual_label, ai_correct, settled_at, status, row["id"]))
                if status == "settled":
                    newly_settled += 1
            conn.commit()
            return newly_settled
        finally:
            conn.close()


def settle_prediction_journal_if_due(paths, database_path, market_database_path=None, now=None):
    """Run the heavier settlement pass at most twice per date (morning/close)."""
    now = now or dt.datetime.now(NY)
    if now.tzinfo is None: now = now.replace(tzinfo=NY)
    now = now.astimezone(NY)
    minute = now.hour * 60 + now.minute
    slot = "close" if minute >= 17 * 60 + 5 else "morning" if minute >= 8 * 60 else None
    if not slot:
        return 0
    slot_value = f"{now.date().isoformat()}:{slot}"
    with _PREDICTION_LOCK:
        conn = _prediction_connect(database_path)
        try:
            seen = conn.execute("SELECT value FROM market_prediction_meta WHERE key='settlement_slot'").fetchone()
            if seen and seen[0] == slot_value:
                return 0
            pending = conn.execute("SELECT COUNT(*) FROM market_predictions WHERE status!='settled'").fetchone()[0]
            if not pending:
                conn.execute("INSERT OR REPLACE INTO market_prediction_meta(key,value) VALUES('settlement_slot',?)",
                             (slot_value,)); conn.commit()
                return 0
        finally:
            conn.close()
    daily, _, _, _ = _combined_market_data(paths, market_database_path)
    settled = settle_prediction_journal(database_path, daily, now=now)
    with _PREDICTION_LOCK:
        conn = _prediction_connect(database_path)
        try:
            conn.execute("INSERT OR REPLACE INTO market_prediction_meta(key,value) VALUES('settlement_slot',?)",
                         (slot_value,)); conn.commit()
        finally:
            conn.close()
    return settled


def record_if_due(paths, data_dir, now=None, database_path=None, news=None,
                  prediction_database_path=None):
    """Persist at most one daily (08:45 ET) and one Sunday (18:15 ET) snapshot.

    Missed runs are caught later in the same day/week.  Stale local data is never
    recorded as if it were a current live observation.
    """
    now = (now or dt.datetime.now(NY)).astimezone(NY)
    path = os.path.join(data_dir, HISTORY_FILE)
    existing = read_recorded_history(path)
    keys = {(x.get("kind"), x.get("key")) for x in existing}
    scheduled = []
    if now.weekday() < 5 and (now.hour, now.minute) >= (8, 45):
        scheduled.append(("daily", now.date().isoformat()))
    days_since_sunday = (now.weekday() + 1) % 7
    sunday = now.date() - dt.timedelta(days=days_since_sunday)
    sunday_due = dt.datetime.combine(sunday, dt.time(18, 15), tzinfo=NY)
    if now >= sunday_due:
        scheduled.append(("weekly", (sunday + dt.timedelta(days=1)).isoformat()))
    due = [(kind, key) for kind, key in scheduled if (kind, key) not in keys]
    # Forecasts are only captured while they are still genuinely forward-looking.
    # A redeploy after the close must not manufacture a trivially "correct" row.
    minute = now.hour * 60 + now.minute
    prediction_window = []
    if 8 * 60 + 45 <= minute < 10 * 60:
        prediction_window.extend(item for item in scheduled if item[0] == "daily")
    weekly_capture_open = (now.weekday() == 6 and minute >= 18 * 60 + 15)
    weekly_capture_monday = (now.weekday() == 0 and minute < 9 * 60 + 30)
    if weekly_capture_open or weekly_capture_monday:
        prediction_window.extend(item for item in scheduled if item[0] == "weekly")
    prediction_keys = _prediction_keys(prediction_database_path) if prediction_database_path else set()
    prediction_due = [(kind, key) for kind, key in prediction_window if (kind, key) not in prediction_keys]
    if not due and not prediction_due:
        return []

    report = build_report(paths, daily_limit=1, weekly_limit=1,
                          database_path=database_path, news=news)
    if not report.get("ok"):
        return []
    try:
        last_bar = pd.Timestamp(report["data"]["last_bar"]).tz_convert(NY)
        if abs((now - last_bar.to_pydatetime()).total_seconds()) > 72 * 3600:
            return []
    except Exception:
        return []

    written = []
    for kind, key in scheduled:
        snap = report.get(kind) or {}
        if not snap.get("ok"):
            continue
        if (kind, key) in due:
            row = {"kind": kind, "key": key, "captured_at": now.isoformat(),
                   "informational_only": True, "snapshot": snap}
            _append_snapshot(path, row); written.append(row)
        if prediction_database_path and (kind, key) in prediction_due:
            record_prediction(prediction_database_path, report, kind, key, now)
    return written


def _main():
    parser = argparse.ArgumentParser(description="Build compact Market Context history database")
    parser.add_argument("--build-db", nargs=2, metavar=("SOURCE_CSV", "OUTPUT_DB"))
    args = parser.parse_args()
    if not args.build_db:
        parser.error("use --build-db SOURCE_CSV OUTPUT_DB")
    print(json.dumps(build_history_database(args.build_db[0], args.build_db[1]), indent=2))


if __name__ == "__main__":
    _main()
