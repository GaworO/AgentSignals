"""Causal, shadow-only quality label for canonical A/B setups.

The thresholds are frozen from the exposed 2022-2025 exploratory replay.  This
module never blocks an order and never changes quantity; it only supplies a
label that can be audited forward before any sizing policy is enabled.
"""
from __future__ import annotations

from typing import Any, Dict


VERSION = "AB_QUALITY_SHADOW_V1"

# Frozen, causal thresholds.  All inputs exist at the close of the BOS bar.
MIN_DISP_LEN = 6
MIN_BOS_DELAY = 4
MAX_BOS_DELAY = 5
MAX_ENTRY_GAP_R = 0.364342758
MIN_FVG_ATR = 0.42228739
MAX_FVG_ATR = 0.579269878


def _number(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deterministic Q0-Q3 shadow label without mutating ``signal``."""
    s = _number(signal.get("s"))
    u = _number(signal.get("u"))
    bos_bar = _number(signal.get("bos_bar"))
    entry = _number(signal.get("entry"))
    stop = _number(signal.get("SL"))
    close = _number(signal.get("signal_close", signal.get("_signal_close")))
    fvg_lo = _number(signal.get("fvg_lo"))
    fvg_hi = _number(signal.get("fvg_hi"))
    atr5 = _number(signal.get("atr5"))

    disp_len = int(u - s + 1) if s is not None and u is not None else None
    bos_delay = int(bos_bar - u) if bos_bar is not None and u is not None else None
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    entry_gap_r = (abs(close - entry) / risk
                   if close is not None and entry is not None and risk and risk > 0 else None)
    fvg_atr = (abs(fvg_hi - fvg_lo) / atr5
               if fvg_lo is not None and fvg_hi is not None and atr5 and atr5 > 0 else None)

    checks = {
        "disp_len_ge_6": disp_len is not None and disp_len >= MIN_DISP_LEN,
        "bos_delay_4_5": bos_delay is not None and MIN_BOS_DELAY <= bos_delay <= MAX_BOS_DELAY,
        "entry_gap_le_0_364r": entry_gap_r is not None and entry_gap_r <= MAX_ENTRY_GAP_R,
        "fvg_atr_balanced": fvg_atr is not None and MIN_FVG_ATR <= fvg_atr < MAX_FVG_ATR,
    }
    available = {
        "disp_len_ge_6": disp_len is not None,
        "bos_delay_4_5": bos_delay is not None,
        "entry_gap_le_0_364r": entry_gap_r is not None,
        "fvg_atr_balanced": fvg_atr is not None,
    }
    score = sum(1 for name, passed in checks.items() if available[name] and passed)
    tier = "Q%d" % min(score, 3)
    suggested = {"Q3": 1.0, "Q2": 1.0, "Q1": 0.25, "Q0": 0.0}[tier]
    missing = [name for name, present in available.items() if not present]
    return {
        "version": VERSION,
        "mode": "SHADOW_ONLY",
        "tier": tier,
        "score": score,
        "max_score": 4,
        "complete": not missing,
        "suggested_risk_mult": suggested,
        "suggestion_only": True,
        "checks": checks,
        "missing": missing,
        "features": {
            "disp_len": disp_len,
            "bos_delay_bars": bos_delay,
            "entry_gap_r": round(entry_gap_r, 6) if entry_gap_r is not None else None,
            "fvg_atr": round(fvg_atr, 6) if fvg_atr is not None else None,
        },
    }


def attach(signal: Dict[str, Any]) -> Dict[str, Any]:
    quality = classify(signal)
    signal["_ab_quality"] = quality
    return quality


def tagline(signal: Dict[str, Any]) -> str:
    quality = signal.get("_ab_quality") or classify(signal)
    tier = quality["tier"]
    score = quality["score"]
    mult = quality["suggested_risk_mult"]
    completeness = "" if quality.get("complete") else " · niepełne dane"
    return "🧪 AB quality SHADOW: %s (%s/4) · sugestia %.2gx%s" % (
        tier, score, mult, completeness)
