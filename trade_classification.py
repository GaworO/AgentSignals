"""Stable, display-only strategy classification shared by LIVE audit tables.

The returned metadata is informational.  Guard and execution must never read it
to decide whether, or at what size, an order is submitted.
"""
from __future__ import annotations

from typing import Any, Dict


VERSION = "TRADE_CLASSIFICATION_VIEW_V1"


def candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    strategy = str(candidate.get("_strat") or "A/B")
    quality = candidate.get("_ab_quality")
    quality = quality if isinstance(quality, dict) else {}
    tier = str(quality.get("tier") or "N/A")
    score = quality.get("score")
    qmode = str(quality.get("mode") or ("SHADOW_ONLY" if tier != "N/A" else "N/A"))

    if strategy == "DOL_DELIVERY_REVERSAL":
        family = "DOL-REVERSAL"
        setup = "DOL REVERSAL"
        manager = "LIVE"
        label = "DOL REVERSAL · MANAGER LIVE" + ((" · " + tier) if tier != "N/A" else "")
    elif strategy.startswith("A/B Directional"):
        direction = str(candidate.get("dir") or "").upper()
        family = "AB-DIR-L" if direction == "LONG" else "AB-DIR-S"
        setup = "LIQUIDITY CHAIN · FIXED 2R"
        manager = "NONE"
        tier = "N/A"
        qmode = "CAUSAL_FIXED_V1"
        label = family + " · FIXED 2R"
    elif strategy.startswith("Continuation"):
        direction = str(candidate.get("dir") or "").upper()
        family = "CONT-L" if direction == "LONG" else "CONT-S"
        setup = "FROZEN OPEN DOL"
        manager = "N/A"
        tier = "N/A"
        qmode = "N/A_AB_ONLY"
        label = family + " · FROZEN OPEN DOL"
    else:
        family = "AB-SHALLOW" if "shallow" in strategy.lower() else "AB"
        setup = "A/B"
        manager = "SHADOW"
        label = family + (" · " + tier + " SHADOW" if tier != "N/A" else " · QUALITY NOT RECORDED")

    eligibility = candidate.get("_dol_eligibility")
    return {
        "version": VERSION,
        "family": family,
        "setup_class": setup,
        "quality_tier": tier,
        "quality_score": score,
        "quality_mode": qmode,
        "manager_mode": manager,
        "dol_eligibility": eligibility if isinstance(eligibility, dict) else None,
        "label": label,
    }


def continuation_order(row: Dict[str, Any]) -> Dict[str, Any]:
    direction = str(row.get("direction") or "").upper()
    is_abdir = str(row.get("strategy") or "").upper() == "AB_DIRECTIONAL"
    family = (("AB-DIR-L" if direction == "LONG" else "AB-DIR-S") if is_abdir else
              ("CONT-L" if direction == "LONG" else "CONT-S"))
    setup = "LIQUIDITY CHAIN · FIXED 2R" if is_abdir else "FROZEN OPEN DOL"
    return {
        "version": VERSION,
        "family": family,
        "setup_class": setup,
        "quality_tier": "N/A",
        "quality_score": None,
        "quality_mode": "N/A_AB_ONLY",
        "manager_mode": "NONE" if is_abdir else "N/A",
        "dol_id": row.get("dol_id"),
        "label": family + (" · FIXED 2R" if is_abdir else " · FROZEN OPEN DOL"),
    }


def guard_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Infer only what an old Guard row proves; never invent an A/B Q grade."""
    existing = row.get("classification")
    if (isinstance(existing, dict) and existing.get("label") and
            "LEGACY/UNCLASSIFIED" not in str(existing.get("label"))):
        return existing
    strategy = str(row.get("strat") or "A/B")
    payload = {"_strat": strategy, "dir": row.get("dir")}
    quality = row.get("ab_quality")
    if isinstance(quality, dict):
        payload["_ab_quality"] = quality
    return candidate(payload)
