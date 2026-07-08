"""Confirmation 1 — liquidity pools and sweep detection.

Pool types (all configurable via `liquidity.pool_types`):
  significant_swing  wide-fractal swing highs/lows (k = significant_swing_k)
  equal              clusters of >=N swing extremes within a tick tolerance
  session            high/low of each COMPLETED session block
  prev_day           previous trading day high/low

Look-ahead policy: a swing with fractal strength k only becomes a pool k bars
after its pivot; session pools activate on the first bar after the session
ends; PD levels come from the previous completed trading day.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# ------------------------------------------------------------------ swings
def fractal_swings(h: np.ndarray, l: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (swing_high_idx, swing_low_idx): pivot indices of k-fractals.

    Pivot i is a swing high if h[i] is the strict max of h[i-k .. i+k]
    (ties broken to the earliest bar). Usable only from bar i+k onward.
    """
    n = len(h)
    if n < 2 * k + 1:
        return np.array([], int), np.array([], int)
    sh, sl = [], []
    for i in range(k, n - k):
        win_h = h[i - k: i + k + 1]
        if h[i] == win_h.max() and (win_h == h[i]).sum() == 1:
            sh.append(i)
        win_l = l[i - k: i + k + 1]
        if l[i] == win_l.min() and (win_l == l[i]).sum() == 1:
            sl.append(i)
    return np.array(sh, int), np.array(sl, int)


def fractal_swings_fast(h: np.ndarray, l: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized fractal detection (same semantics as fractal_swings)."""
    n = len(h)
    if n < 2 * k + 1:
        return np.array([], int), np.array([], int)
    hi_ok = np.ones(n - 2 * k, bool)
    lo_ok = np.ones(n - 2 * k, bool)
    center_h = h[k: n - k]
    center_l = l[k: n - k]
    for off in range(-k, k + 1):
        if off == 0:
            continue
        seg_h = h[k + off: n - k + off]
        seg_l = l[k + off: n - k + off]
        if off < 0:  # earlier bars must be strictly lower/higher (tie -> earlier wins)
            hi_ok &= seg_h < center_h
            lo_ok &= seg_l > center_l
        else:        # later bars may not exceed or equal
            hi_ok &= seg_h < center_h
            lo_ok &= seg_l > center_l
    return np.nonzero(hi_ok)[0] + k, np.nonzero(lo_ok)[0] + k


# ------------------------------------------------------------------ pools
@dataclass
class Level:
    kind: str          # significant_swing | equal | session | prev_day
    side: str          # "above" (buy-side liquidity) | "below" (sell-side)
    price: float
    pivot_idx: int     # bar where the extreme printed
    active_from: int   # first bar index at which the level is known
    meta: str = ""
    swept_idx: int | None = None

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.side}:{self.price:.2f}@{self.pivot_idx}"


@dataclass
class SweepEvent:
    direction: str     # "long" (swept lows) | "short" (swept highs)
    level: Level
    sweep_idx: int     # bar that penetrated the level
    confirm_idx: int   # bar where sweep confirmed (close-back), == sweep_idx if not required
    penetration_ticks: float
    extreme: float     # the sweep wick extreme (stop anchor)


def build_levels(md, cfg: dict) -> list[Level]:
    """Precompute every pool with its activation bar. Sorted by active_from."""
    lcfg = cfg["liquidity"]
    tick = cfg["data"]["tick_size"]
    pool_types = set(lcfg["pool_types"])
    levels: list[Level] = []

    k_sig = lcfg["significant_swing_k"]
    sh, sl = fractal_swings_fast(md.h, md.l, k_sig)
    if "significant_swing" in pool_types:
        for i in sh:
            levels.append(Level("significant_swing", "above", md.h[i], int(i), int(i + k_sig)))
        for i in sl:
            levels.append(Level("significant_swing", "below", md.l[i], int(i), int(i + k_sig)))

    if "equal" in pool_types:
        tol = lcfg["equal_level_tol_ticks"] * tick
        min_t = lcfg["equal_level_min_touches"]
        k_eq = lcfg["swing_k"]
        eh, el = fractal_swings_fast(md.h, md.l, k_eq)
        levels += _equal_clusters(md.h, eh, k_eq, tol, min_t, "above")
        levels += _equal_clusters(md.l, el, k_eq, tol, min_t, "below")

    if "session" in pool_types:
        levels += _session_levels(md)

    # prev_day handled from md.pdh / md.pdl at runtime (see PoolManager)
    levels.sort(key=lambda x: x.active_from)
    return levels


def _equal_clusters(px: np.ndarray, pivots: np.ndarray, k: int, tol: float,
                    min_touches: int, side: str) -> list[Level]:
    """Pair up swing extremes within tolerance -> equal-highs/lows pools.

    The pool activates when the LAST member confirms; its price is the extreme
    of the cluster (liquidity rests beyond the farthest touch).
    """
    out = []
    recent: list[tuple[int, float]] = []  # (pivot, price) not yet clustered
    for p in pivots:
        price = px[p]
        matched = None
        for j, (pp, ppx) in enumerate(recent):
            if abs(price - ppx) <= tol and (p - pp) <= 720:  # within 12h
                matched = j
                break
        if matched is not None:
            pp, ppx = recent.pop(matched)
            ext = max(price, ppx) if side == "above" else min(price, ppx)
            out.append(Level("equal", side, ext, int(p), int(p + k), meta=f"pair@{pp}"))
        else:
            recent.append((int(p), float(price)))
            recent = recent[-30:]
    return out


def _session_levels(md) -> list[Level]:
    """High/low of each completed session block, active from the next bar."""
    out = []
    sess = md.session
    n = len(sess)
    start = 0
    for i in range(1, n + 1):
        if i == n or sess[i] != sess[start]:
            name = sess[start]
            if name != "other" and i - start >= 15:
                hi = md.h[start:i].max()
                lo = md.l[start:i].min()
                hi_at = start + int(np.argmax(md.h[start:i]))
                lo_at = start + int(np.argmin(md.l[start:i]))
                out.append(Level("session", "above", float(hi), hi_at, i, meta=name))
                out.append(Level("session", "below", float(lo), lo_at, i, meta=name))
            start = i
    return out


# ------------------------------------------------------------------ runtime
class PoolManager:
    """Feeds levels into the live set and detects sweeps bar by bar."""

    def __init__(self, md, cfg: dict, levels: list[Level]):
        self.md = md
        self.cfg = cfg
        self.lcfg = cfg["liquidity"]
        self.scfg = self.lcfg["sweep"]
        self.tick = cfg["data"]["tick_size"]
        self.levels = levels
        self._next = 0
        self.active_above: list[Level] = []
        self.active_below: list[Level] = []
        self._pending: list[dict] = []  # sweeps awaiting close-back confirmation
        self.max_age = self.lcfg["level_max_age_min"]
        self._pd_day: int | None = None
        self._pd_levels: list[Level] = []

    def _activate(self, i: int) -> None:
        while self._next < len(self.levels) and self.levels[self._next].active_from <= i:
            lv = self.levels[self._next]
            (self.active_above if lv.side == "above" else self.active_below).append(lv)
            self._next += 1
        if i % 240 == 0:  # prune stale
            lo = i - self.max_age
            self.active_above = [x for x in self.active_above if x.pivot_idx >= lo and x.swept_idx is None]
            self.active_below = [x for x in self.active_below if x.pivot_idx >= lo and x.swept_idx is None]

    def _prev_day_levels(self, i: int) -> list[Level]:
        """PDH/PDL as persistent levels, refreshed once per trading day."""
        if "prev_day" not in self.lcfg["pool_types"] or np.isnan(self.md.pdh[i]):
            return []
        day = int(self.md.tday[i])
        if day != self._pd_day:
            self._pd_day = day
            self._pd_levels = [
                Level("prev_day", "above", float(self.md.pdh[i]), i, i),
                Level("prev_day", "below", float(self.md.pdl[i]), i, i),
            ]
        return self._pd_levels

    def step(self, i: int) -> list[SweepEvent]:
        """Advance one bar; return confirmed sweep events at bar i."""
        self._activate(i)
        md, s = self.md, self.scfg
        pen_min = s["min_penetration_ticks"] * self.tick
        pen_max = s["max_penetration_ticks"] * self.tick
        events: list[SweepEvent] = []

        # -- resolve pending close-back confirmations
        still = []
        for p in self._pending:
            lv, d = p["level"], p["dir"]
            closed_back = (md.c[i] < lv.price) if d == "short" else (md.c[i] > lv.price)
            p["extreme"] = max(p["extreme"], md.h[i]) if d == "short" else min(p["extreme"], md.l[i])
            if closed_back:
                if self._displacement_ok(i, d):
                    events.append(SweepEvent(d, lv, p["sweep_idx"], i, p["pen"], p["extreme"]))
            elif i - p["sweep_idx"] < s["close_back_window_bars"]:
                still.append(p)
            # else: no close-back in window -> breakout, drop it
        self._pending = still

        # -- new penetrations this bar
        for lv in self.active_above + self._prev_day_levels(i):
            if lv.side != "above" or lv.swept_idx is not None or lv.active_from > i:
                continue
            pen = md.h[i] - lv.price
            if pen >= pen_min and pen <= pen_max and self._filters_ok(i, "short"):
                lv.swept_idx = i
                if s["require_close_back"]:
                    if md.c[i] < lv.price:
                        if self._displacement_ok(i, "short"):
                            events.append(SweepEvent("short", lv, i, i, pen / self.tick, md.h[i]))
                    else:
                        self._pending.append({"level": lv, "dir": "short", "sweep_idx": i,
                                              "pen": pen / self.tick, "extreme": md.h[i]})
                else:
                    events.append(SweepEvent("short", lv, i, i, pen / self.tick, md.h[i]))
        for lv in self.active_below + self._prev_day_levels(i):
            if lv.side != "below" or lv.swept_idx is not None or lv.active_from > i:
                continue
            pen = lv.price - md.l[i]
            if pen >= pen_min and pen <= pen_max and self._filters_ok(i, "long"):
                lv.swept_idx = i
                if s["require_close_back"]:
                    if md.c[i] > lv.price:
                        if self._displacement_ok(i, "long"):
                            events.append(SweepEvent("long", lv, i, i, pen / self.tick, md.l[i]))
                    else:
                        self._pending.append({"level": lv, "dir": "long", "sweep_idx": i,
                                              "pen": pen / self.tick, "extreme": md.l[i]})
                else:
                    events.append(SweepEvent("long", lv, i, i, pen / self.tick, md.l[i]))
        return events

    def _displacement_ok(self, i: int, direction: str) -> bool:
        """Optional: the sweep-confirmation bar must itself be a displacement
        candle in the reversal direction (body >= mult * ATR)."""
        s, md = self.scfg, self.md
        if not s["displacement_after"]:
            return True
        a = md.atr1m[i]
        if np.isnan(a) or a <= 0:
            return False
        body = md.c[i] - md.o[i] if direction == "long" else md.o[i] - md.c[i]
        return body >= s["displacement_atr_mult"] * a

    def _filters_ok(self, i: int, direction: str) -> bool:
        md, s = self.md, self.scfg
        if s["atr_filter"]:
            j0 = max(0, i - 2000)
            thresh = np.nanpercentile(md.atr1m[j0:i + 1], s["atr_min_percentile"])
            if np.isnan(md.atr1m[i]) or md.atr1m[i] < thresh:
                return False
        if s["min_wick_ticks"]:
            if direction == "short":
                wick = md.h[i] - max(md.o[i], md.c[i])
            else:
                wick = min(md.o[i], md.c[i]) - md.l[i]
            if wick < s["min_wick_ticks"] * self.tick:
                return False
        if s["volume_filter"] and not np.isnan(md.vol_avg[i]):
            if md.v[i] < s["volume_mult"] * md.vol_avg[i]:
                return False
        if s["time_filter"]:
            from .utils import in_window
            if not any(in_window(md.minute_of_day[i], a, b) for a, b in s["time_filter"]):
                return False
        return True
