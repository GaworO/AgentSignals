"""Confirmation 2 — higher-timeframe Fair Value Gaps.

A bullish FVG exists when low[i] > high[i-2] (gap = [high[i-2], low[i]]);
bearish when high[i] < low[i-2]. The gap becomes USABLE only at the close
of candle i (close_ts) — never earlier.

Lifecycle tracked per gap: creation, fill percentage, consequent
encroachment (CE = midpoint), mitigation (first touch), full fill (death).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class FVG:
    tf: str
    direction: str        # "bullish" | "bearish"
    top: float
    bottom: float
    created_ts: int       # ns epoch of the candle CLOSE that completes the gap
    created_bar: int      # HTF bar position
    atr_at_creation: float
    filled_pct: float = 0.0
    mitigated_ts: int | None = None
    dead: bool = False

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def ce(self) -> float:
        return (self.top + self.bottom) / 2.0

    def update_fill(self, bar_low: float, bar_high: float, ts: int) -> None:
        """Update fill % / mitigation from a 1m bar that occurred AFTER creation."""
        if self.dead:
            return
        if self.direction == "bullish":
            if bar_low < self.top:
                if self.mitigated_ts is None:
                    self.mitigated_ts = ts
                depth = min(self.top - bar_low, self.size)
                self.filled_pct = max(self.filled_pct, 100.0 * depth / self.size)
        else:
            if bar_high > self.bottom:
                if self.mitigated_ts is None:
                    self.mitigated_ts = ts
                depth = min(bar_high - self.bottom, self.size)
                self.filled_pct = max(self.filled_pct, 100.0 * depth / self.size)
        if self.filled_pct >= 100.0:
            self.dead = True

    def zone(self, entry_zone: str, ce_tol: float) -> tuple[float, float]:
        if entry_zone == "ce":
            return self.ce - ce_tol, self.ce + ce_tol
        if entry_zone == "mitigation":
            # proximal half of the gap
            if self.direction == "bullish":
                return self.ce, self.top
            return self.bottom, self.ce
        return self.bottom, self.top


def detect_htf_fvgs(htf_df: pd.DataFrame, tf: str, min_size: float,
                    min_atr_frac: float) -> list[FVG]:
    """Vectorized detection over an HTF frame. Returns gaps sorted by creation."""
    h = htf_df["high"].to_numpy()
    l = htf_df["low"].to_numpy()
    a = htf_df["atr"].to_numpy()
    close_ts = htf_df["close_ts"].values.astype("datetime64[ns]").astype(np.int64)
    out: list[FVG] = []
    for i in range(2, len(h)):
        floor = min_size
        if min_atr_frac and not np.isnan(a[i]):
            floor = max(floor, min_atr_frac * a[i])
        if l[i] - h[i - 2] >= floor:  # bullish gap
            out.append(FVG(tf, "bullish", float(l[i]), float(h[i - 2]),
                           int(close_ts[i]), i, float(a[i]) if not np.isnan(a[i]) else 0.0))
        if l[i - 2] - h[i] >= floor:  # bearish gap
            out.append(FVG(tf, "bearish", float(l[i - 2]), float(h[i]),
                           int(close_ts[i]), i, float(a[i]) if not np.isnan(a[i]) else 0.0))
    return out


class HTFFVGManager:
    """Streams 1m bars; answers 'is price delivering from a valid HTF FVG?'"""

    def __init__(self, md, cfg: dict):
        self.cfg = cfg["htf_fvg"]
        self.tick = cfg["data"]["tick_size"]
        min_size = self.cfg["min_size_ticks"] * self.tick
        self.all_gaps: list[FVG] = []
        self.bar_minutes = {}
        wanted = set(self.cfg["timeframes"])
        for tf, frame in md.htf.items():
            if tf not in wanted:
                continue
            self.all_gaps += detect_htf_fvgs(frame, tf, min_size, self.cfg["min_size_atr_frac"])
            from .data import HTF_MINUTES
            self.bar_minutes[tf] = HTF_MINUTES[tf]
        self.all_gaps.sort(key=lambda g: g.created_ts)
        self._next = 0
        self.active: list[FVG] = []
        self.md = md

    def step(self, i: int) -> None:
        """Activate newly-completed gaps and update fill state with bar i."""
        ts = self.md.ts[i]
        while self._next < len(self.all_gaps) and self.all_gaps[self._next].created_ts <= ts:
            self.active.append(self.all_gaps[self._next])
            self._next += 1
        max_filled = self.cfg["max_filled_pct"]
        keep = []
        for g in self.active:
            g.update_fill(self.md.l[i], self.md.h[i], ts)
            age_bars = (ts - g.created_ts) / 1e9 / 60 / self.bar_minutes[g.tf]
            if not g.dead and g.filled_pct < max_filled and age_bars <= self.cfg["max_age_bars"]:
                keep.append(g)
        self.active = keep

    def gap_in_play(self, i: int, direction: str) -> FVG | None:
        """Return a valid aligned HTF FVG whose zone contains the current price
        (or from which price is reacting within react_window_min)."""
        want = "bullish" if direction == "long" else "bearish"
        px = self.md.c[i]
        ts = self.md.ts[i]
        window_ns = self.cfg["react_window_min"] * 60e9
        best: FVG | None = None
        for g in self.active:
            if self.cfg["require_alignment"] and g.direction != want:
                continue
            lo, hi = g.zone(self.cfg["entry_zone"], self.cfg["ce_tolerance_ticks"] * self.tick)
            inside = lo <= px <= hi
            reacting = (g.mitigated_ts is not None and (ts - g.mitigated_ts) <= window_ns)
            if inside or reacting:
                if best is None or g.size > best.size:
                    best = g
        return best
