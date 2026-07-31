"""Data loading and the multi-timeframe engine.

All timestamps are converted to the project fixed UTC-04:00 strategy clock. A "trading day" follows the
futures convention and rolls at 18:00 ET.

Look-ahead policy
-----------------
* HTF candles are stamped by their OPEN time but only become *usable* at their
  CLOSE time. `resample_htf` returns a `close_ts` column and every consumer
  gates on it.
* Session high/low, PDH/PDL etc. are computed strictly from past bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import atr as _atr

HTF_MINUTES = {"5min": 5, "15min": 15, "1h": 60, "4h": 240}


@dataclass
class MarketData:
    """1m base data plus derived arrays used by the engine (all causal)."""
    df: pd.DataFrame              # 1m bars, ET index
    ts: np.ndarray                # int64 ns
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    atr1m: np.ndarray
    vol_avg: np.ndarray           # 20-bar rolling mean volume (causal)
    minute_of_day: np.ndarray     # ET minute-of-day per bar
    tday: np.ndarray              # trading-day ordinal per bar
    session: np.ndarray           # session label per bar (object array)
    pdh: np.ndarray               # previous trading day high, per bar
    pdl: np.ndarray
    htf: dict                     # tf -> DataFrame with open/high/low/close/close_ts/atr
    accum_ok: object = None       # per-bar bool: this trading-day AM was a compressed "accumulation"


def load_1m(csv_path: str | Path, tz: str = "Etc/GMT+4",
            start: str | None = None, end: str | None = None) -> pd.DataFrame:
    csv_path = Path(csv_path)
    cache = csv_path.with_suffix(".feather")
    if cache.exists():
        df = pd.read_feather(cache).set_index("ts_event")
        df.index = df.index.tz_convert(tz)
    else:
        df = pd.read_csv(csv_path, usecols=["ts_event", "open", "high", "low", "close", "volume"])
        df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert(tz)
        df = df.set_index("ts_event").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        try:
            df.reset_index().to_feather(cache)
        except Exception:
            pass  # cache is best-effort
    if start:
        df = df[df.index >= pd.Timestamp(start, tz=tz)]
    if end:
        df = df[df.index < pd.Timestamp(end, tz=tz)]
    return df


def trading_day_ids(index: pd.DatetimeIndex, roll_hour: int = 18) -> np.ndarray:
    """Ordinal trading-day id; day rolls at roll_hour ET."""
    shifted = index - pd.Timedelta(hours=roll_hour)
    return shifted.normalize().map(pd.Timestamp.toordinal).to_numpy()


def session_labels(minute_of_day: np.ndarray, sessions_cfg: dict) -> np.ndarray:
    from .utils import hhmm_to_min
    out = np.full(len(minute_of_day), "other", dtype=object)
    for name in ("asia", "london", "ny_am", "lunch", "ny_pm"):
        if name not in sessions_cfg:
            continue
        a, b = (hhmm_to_min(x) for x in sessions_cfg[name])
        if a <= b:
            mask = (minute_of_day >= a) & (minute_of_day < b)
        else:
            mask = (minute_of_day >= a) | (minute_of_day < b)
        out[mask] = name
    return out


def prev_day_levels(h: np.ndarray, l: np.ndarray, tday: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar previous-completed-trading-day high/low (NaN for first day)."""
    pdh = np.full(len(h), np.nan)
    pdl = np.full(len(h), np.nan)
    days, starts = np.unique(tday, return_index=True)
    order = np.argsort(starts)
    days, starts = days[order], starts[order]
    bounds = list(starts) + [len(h)]
    prev_hi, prev_lo = np.nan, np.nan
    for i in range(len(days)):
        s, e = bounds[i], bounds[i + 1]
        pdh[s:e] = prev_hi
        pdl[s:e] = prev_lo
        prev_hi = np.max(h[s:e])
        prev_lo = np.min(l[s:e])
    return pdh, pdl


def resample_htf(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample 1m -> HTF. Adds close_ts = timestamp when the candle is complete."""
    rule = {"5min": "5min", "15min": "15min", "1h": "1h", "4h": "4h"}[tf]
    agg = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])
    agg["close_ts"] = agg.index + pd.Timedelta(minutes=HTF_MINUTES[tf])
    agg["atr"] = _atr(agg["high"].to_numpy(), agg["low"].to_numpy(),
                      agg["close"].to_numpy(), 14)
    return agg


def accumulation_flags(df: pd.DataFrame, tday: np.ndarray, cfg: dict) -> np.ndarray:
    """AMD 'Accumulation' gate (causal).

    For each trading day, measure the MORNING window (default 08:00-12:00 ET)
    range or realized vol, normalize by the trailing-N-day mean (shifted 1 day
    so only prior days are used), and flag the day as an accumulation day if
    that ratio is <= max_ratio. Returns a per-1m-bar boolean. When the gate is
    disabled every bar is True (no-op).
    """
    n = len(df)
    acfg = cfg.get("accumulation", {})
    if not acfg.get("enabled", False):
        return np.ones(n, bool)
    from .utils import hhmm_to_min
    a, b = hhmm_to_min(acfg.get("window", ["08:00", "12:00"])[0]), \
        hhmm_to_min(acfg.get("window", ["08:00", "12:00"])[1])
    metric = acfg.get("metric", "range_ratio")     # range_ratio | vol_ratio
    look = int(acfg.get("lookback_days", 20))
    maxr = float(acfg.get("max_ratio", 1.0))
    min_bars = int(acfg.get("min_am_bars", 120))

    mod = (df.index.hour * 60 + df.index.minute).to_numpy()
    am = (mod >= a) & (mod < b)
    sub = pd.DataFrame({"tday": tday[am], "high": df["high"].to_numpy()[am],
                        "low": df["low"].to_numpy()[am],
                        "lc": np.log(df["close"].to_numpy()[am])})
    g = sub.groupby("tday")
    rng = g["high"].max() - g["low"].min()
    vol = g["lc"].apply(lambda x: x.diff().std())
    bars = g.size()
    base = (rng if metric == "range_ratio" else vol).copy()
    base[bars < min_bars] = np.nan                 # not a real AM session
    ratio = base / base.rolling(look).mean().shift(1)
    ok_day = (ratio <= maxr) & ratio.notna()
    okmap = ok_day.to_dict()
    return np.array([bool(okmap.get(d, False)) for d in tday])


def build_market_data(cfg: dict, start: str | None = None, end: str | None = None,
                      base_dir: str | Path = ".") -> MarketData:
    dcfg = cfg["data"]
    path = Path(base_dir) / dcfg["csv_path"]
    df = load_1m(path, dcfg["tz"], start, end)

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    mod = (df.index.hour * 60 + df.index.minute).to_numpy()
    tday = trading_day_ids(df.index, dcfg["trading_day_roll_hour"])
    pdh, pdl = prev_day_levels(h, l, tday)
    vol_avg = pd.Series(v).rolling(20, min_periods=5).mean().to_numpy()

    htf = {tf: resample_htf(df, tf) for tf in cfg["htf_fvg"]["timeframes"]}
    accum_ok = accumulation_flags(df, tday, cfg)

    return MarketData(
        df=df, ts=df.index.asi8, o=o, h=h, l=l, c=c, v=v,
        atr1m=_atr(h, l, c, 20), vol_avg=vol_avg, minute_of_day=mod,
        tday=tday, session=session_labels(mod, cfg["sessions"]),
        pdh=pdh, pdl=pdl, htf=htf, accum_ok=accum_ok,
    )
