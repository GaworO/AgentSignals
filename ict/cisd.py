"""Confirmation 4 — Change in State of Delivery.

Four selectable definitions (config `cisd.definition`), all evaluated on the
1m timeframe AFTER the IFVG confirms. For a LONG setup we are looking for
proof that DOWN-delivery has flipped:

  A  close above the OPEN of the last consecutive down-candle sequence
     (classic CISD line)
  B  micro market-structure shift: close above the last minor swing high
     formed during the decline (k = b_swing_k)
  C  break of recent swing (wider k = c_swing_k, lookback c_lookback_min)
  D  displacement candle: body >= d_atr_mult * ATR closing through the last
     minor swing high

Symmetric for shorts. All detectors are causal: swings need k bars of
right-hand confirmation before they exist.
"""
from __future__ import annotations


class CISDDetector:
    """Instantiated when an IFVG confirms; stepped each bar until it fires,
    times out, or the setup dies."""

    def __init__(self, md, cfg: dict, direction: str, ifvg_idx: int,
                 anchor_idx: int | None = None):
        self.md = md
        self.cfg = cfg["cisd"]
        self.full_cfg = cfg
        self.direction = direction
        self.ifvg_idx = ifvg_idx
        self.anchor_idx = anchor_idx if anchor_idx is not None else ifvg_idx
        self.definition = self.cfg["definition"].upper()
        self.disp_fvg: dict | None = None   # set by definition E when it fires

    # ---------- public ----------
    def check(self, i: int) -> tuple[bool, float | None, str]:
        """Return (fired, reference_level, note) evaluated at bar close i."""
        fn = {"A": self._def_a, "B": self._def_b, "C": self._def_c,
              "D": self._def_d, "E": self._def_e, "F": self._def_f,
              "G": self._def_g}[self.definition]
        return fn(i)

    # ---------- helpers ----------
    def _last_opposing_sequence_open(self, i: int) -> float | None:
        """Open of the most recent maximal run of opposing candles before bar i."""
        md = self.md
        min_seq = self.cfg["a_min_seq_candles"]
        j = i - 1
        lo = max(0, self.ifvg_idx - 30)
        while j > lo:
            opposing = (md.c[j] < md.o[j]) if self.direction == "long" else (md.c[j] > md.o[j])
            if opposing:
                k = j
                while k - 1 > lo:
                    prev_op = (md.c[k - 1] < md.o[k - 1]) if self.direction == "long" \
                        else (md.c[k - 1] > md.o[k - 1])
                    if prev_op:
                        k -= 1
                    else:
                        break
                if j - k + 1 >= min_seq:
                    return float(md.o[k])
                j = k - 1
            else:
                j -= 1
        return None

    def _last_minor_swing(self, i: int, k: int, lookback: int) -> float | None:
        """Most recent confirmed k-fractal against the setup direction."""
        md = self.md
        lo = max(k, i - lookback)
        for p in range(i - k, lo - 1, -1):       # pivot must have k bars both sides, all <= i
            if p + k > i:
                continue
            if self.direction == "long":
                win = md.h[p - k: p + k + 1]
                if md.h[p] == win.max() and (win == md.h[p]).sum() == 1:
                    return float(md.h[p])
            else:
                win = md.l[p - k: p + k + 1]
                if md.l[p] == win.min() and (win == md.l[p]).sum() == 1:
                    return float(md.l[p])
        return None

    # ---------- definitions ----------
    def _def_a(self, i: int):
        ref = self._last_opposing_sequence_open(i)
        if ref is None:
            return False, None, "no opposing sequence"
        md = self.md
        fired = md.c[i] > ref if self.direction == "long" else md.c[i] < ref
        return fired, ref, f"seq-open {ref:.2f}"

    def _def_b(self, i: int):
        ref = self._last_minor_swing(i, self.cfg["b_swing_k"], 45)
        if ref is None:
            return False, None, "no micro swing"
        md = self.md
        fired = md.c[i] > ref if self.direction == "long" else md.c[i] < ref
        return fired, ref, f"micro-MSS {ref:.2f}"

    def _def_c(self, i: int):
        ref = self._last_minor_swing(i, self.cfg["c_swing_k"], self.cfg["c_lookback_min"])
        if ref is None:
            return False, None, "no swing"
        md = self.md
        fired = md.c[i] > ref if self.direction == "long" else md.c[i] < ref
        return fired, ref, f"swing-break {ref:.2f}"

    def _def_d(self, i: int):
        ref = self._last_minor_swing(i, self.cfg["b_swing_k"], 45)
        if ref is None:
            return False, None, "no structure"
        md = self.md
        import numpy as np
        a = md.atr1m[i]
        if np.isnan(a) or a <= 0:
            return False, None, "no atr"
        body = abs(md.c[i] - md.o[i])
        big = body >= self.cfg["d_atr_mult"] * a
        if self.direction == "long":
            fired = big and md.c[i] > ref and md.c[i] > md.o[i]
        else:
            fired = big and md.c[i] < ref and md.c[i] < md.o[i]
        return fired, ref, f"displacement {ref:.2f}"

    def _def_e(self, i: int):
        """Full sequence: CISD-A close AND BOS (def-C swing break) AND the
        displacement since the setup anchor left a 1m FVG in trade direction."""
        ok_a, ref_a, _ = self._def_a(i)
        if not ok_a:
            return False, ref_a, "E: no CISD-A yet"
        ok_c, ref_c, _ = self._def_c(i)
        if not ok_c:
            return False, ref_c, "E: CISD without BOS"
        fvg = self._displacement_fvg(i)
        if fvg is None:
            return False, ref_a, "E: no displacement FVG"
        self.disp_fvg = fvg
        return True, ref_a, (f"E: CISD {ref_a:.2f} + BOS {ref_c:.2f} + "
                             f"dispFVG [{fvg['bottom']:.2f},{fvg['top']:.2f}]")

    def _def_f(self, i: int):
        """CISD-A AND BOS: close through the opposing-sequence open AND through
        the last confirmed swing low (shorts) / high (longs). Entry waits for
        whichever comes last."""
        ok_a, ref_a, _ = self._def_a(i)
        if not ok_a:
            return False, ref_a, "F: no CISD-A yet"
        ok_c, ref_c, _ = self._def_c(i)
        if not ok_c:
            return False, ref_c, "F: CISD without BOS"
        return True, ref_c, f"F: CISD {ref_a:.2f} + BOS {ref_c:.2f}"

    def _def_g(self, i: int):
        """Structural BOS (Aleks 2026-07-06): CISD-A close AND close through
        the LOWEST low (shorts) / HIGHEST high (longs) of the whole structure
        formed since the sweep anchor. Not a minor fractal — the extreme."""
        ok_a, ref_a, _ = self._def_a(i)
        if not ok_a:
            return False, ref_a, "G: no CISD-A yet"
        md = self.md
        a = self.anchor_idx
        if i - a < 3:
            return False, None, "G: structure too young"
        if self.direction == "short":
            ref = float(md.l[a:i].min())
            fired = md.c[i] < ref
        else:
            ref = float(md.h[a:i].max())
            fired = md.c[i] > ref
        return fired, ref, (f"G: CISD {ref_a:.2f} + structural BOS {ref:.2f}"
                            if fired else "G: CISD without structural BOS")

    def _displacement_fvg(self, i: int) -> dict | None:
        """Most recent completed 1m FVG in trade direction formed after the
        setup anchor (the sweep bar in 3-confirm mode). Causal: uses only
        bars <= i."""
        md = self.md
        tick = self.full_cfg["data"]["tick_size"]
        min_size = self.full_cfg["ifvg"]["min_size_ticks"] * tick
        start = max(self.ifvg_idx + 1, 2)
        for j in range(i, start - 1, -1):
            if self.direction == "short":
                if md.h[j] < md.l[j - 2] and (md.l[j - 2] - md.h[j]) >= min_size:
                    return dict(top=float(md.l[j - 2]), bottom=float(md.h[j]),
                                created_idx=j)
            else:
                if md.l[j] > md.h[j - 2] and (md.l[j] - md.h[j - 2]) >= min_size:
                    return dict(top=float(md.l[j]), bottom=float(md.h[j - 2]),
                                created_idx=j)
        return None
