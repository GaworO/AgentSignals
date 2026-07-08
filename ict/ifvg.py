"""Confirmation 3 — 1-minute Inversion Fair Value Gaps.

For a LONG setup we want a bearish 1m FVG that price closes back through:
the failed bearish imbalance "inverts" into support. Every IFVG is an object
with full lifecycle: created -> (inverted | expired) -> (mitigated | invalidated).

Inversion methods (config `ifvg.inversion_method`):
  close_through  candle CLOSE beyond the far edge of the gap
  full_body      candle body (open & close) entirely beyond the gap
  wick_through   any trade beyond the far edge

Strength score in [0,1]: gap size relative to ATR, capped.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IFVG:
    direction: str            # direction of the SETUP it supports: "long" | "short"
    top: float
    bottom: float
    created_idx: int          # 1m bar index of the 3rd candle (gap complete)
    strength: float
    inverted_idx: int | None = None
    mitigated_idx: int | None = None    # first retrace into the inverted gap
    invalidated_idx: int | None = None  # price reclaimed the gap -> dead
    state: str = "fresh"      # fresh | inverted | mitigated | invalidated | expired

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def ce(self) -> float:
        return (self.top + self.bottom) / 2.0


class IFVGTracker:
    """Runs only inside an active setup window (post-sweep) — keeps the hot
    loop cheap. Feed every 1m bar; query `inverted()` for usable IFVGs."""

    def __init__(self, md, cfg: dict, direction: str, start_idx: int):
        self.md = md
        self.cfg = cfg["ifvg"]
        self.tick = cfg["data"]["tick_size"]
        self.min_size = self.cfg["min_size_ticks"] * self.tick
        self.direction = direction
        self.start_idx = start_idx
        self.gaps: list[IFVG] = []

    def step(self, i: int) -> IFVG | None:
        """Process bar i. Returns a newly-inverted IFVG if one confirmed here."""
        md = self.md
        newly = None

        # -- detect new 1m FVGs (need bars i-2, i-1, i, all >= start-2)
        if i >= max(2, self.start_idx):
            if self.direction == "long":
                # bearish gap: low[i-2] - high[i] > 0
                gap = md.l[i - 2] - md.h[i]
                if gap >= self.min_size:
                    self.gaps.append(IFVG("long", float(md.l[i - 2]), float(md.h[i]),
                                          i, self._strength(gap, i)))
            else:
                gap = md.l[i] - md.h[i - 2]
                if gap >= self.min_size:
                    self.gaps.append(IFVG("short", float(md.l[i]), float(md.h[i - 2]),
                                          i, self._strength(gap, i)))

        # -- lifecycle updates
        max_age = self.cfg["max_age_min"]
        for g in self.gaps:
            if g.state in ("invalidated", "expired"):
                continue
            if i - g.created_idx > max_age:
                g.state = "expired"
                continue
            if g.state == "fresh":
                if g.created_idx < i and self._inverted_now(g, i):
                    g.inverted_idx = i
                    g.state = "inverted"
                    if g.strength >= self.cfg["min_strength"] and newly is None:
                        newly = g
            elif g.state in ("inverted", "mitigated"):
                if self._invalidated_now(g, i):
                    g.invalidated_idx = i
                    g.state = "invalidated"
                elif g.state == "inverted" and self._mitigated_now(g, i):
                    g.mitigated_idx = i
                    g.state = "mitigated"
        return newly

    # ---- rules -------------------------------------------------------
    def _strength(self, gap: float, i: int) -> float:
        import numpy as np
        a = self.md.atr1m[i]
        if a is None or np.isnan(a) or a <= 0:
            return 0.5
        return float(min(1.0, gap / (2.0 * a)))

    def _inverted_now(self, g: IFVG, i: int) -> bool:
        md, m = self.md, self.cfg["inversion_method"]
        if g.direction == "long":       # bearish gap; need close ABOVE top
            if m == "close_through":
                ok = md.c[i] > g.top
            elif m == "full_body":
                ok = min(md.o[i], md.c[i]) > g.top
            else:                        # wick_through
                ok = md.h[i] > g.top
        else:                            # bullish gap; need close BELOW bottom
            if m == "close_through":
                ok = md.c[i] < g.bottom
            elif m == "full_body":
                ok = max(md.o[i], md.c[i]) < g.bottom
            else:
                ok = md.l[i] < g.bottom
        if not ok:
            return False
        need = self.cfg["confirm_closes"]
        if need <= 1:
            return True
        # count consecutive qualifying closes ending at i
        cnt = 0
        j = i
        while j > g.created_idx and cnt < need:
            c = md.c[j]
            good = c > g.top if g.direction == "long" else c < g.bottom
            if not good:
                break
            cnt += 1
            j -= 1
        return cnt >= need

    def _mitigated_now(self, g: IFVG, i: int) -> bool:
        if g.direction == "long":
            return self.md.l[i] <= g.top
        return self.md.h[i] >= g.bottom

    def _invalidated_now(self, g: IFVG, i: int) -> bool:
        rule = self.cfg["invalidation"]
        if g.direction == "long":
            lvl = g.bottom if rule == "full_reclaim" else g.ce
            return self.md.c[i] < lvl
        lvl = g.top if rule == "full_reclaim" else g.ce
        return self.md.c[i] > lvl

    def usable(self) -> list[IFVG]:
        return [g for g in self.gaps if g.state in ("inverted", "mitigated")]
