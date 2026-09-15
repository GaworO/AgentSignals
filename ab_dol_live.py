"""Read-only ranked-DOL metadata adapter for canonical live A/B signals.

The ranked representation and source ledger remain owned by
``detcore.a_cont_v3_ict_dol``.  This module only adapts the live buffer and a
canonical A/B record to that frozen API.  It has no execution or broker path.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np


SCHEMA = "AB_MULTI_HORIZON_DOL_V1"
_ENGINE_CACHE: dict[str, Any] = {
    "key": None, "engine": None, "native_key": None, "native_levels": None,
}


def _engine_for(buffer_path: str):
    """Build the existing causal policy engine once per live buffer version."""
    stat = os.stat(buffer_path)
    key = (os.path.abspath(buffer_path), int(stat.st_mtime_ns), int(stat.st_size))
    if _ENGINE_CACHE["key"] != key:
        from audit_results.ict_125.policy import Engine, load_bars

        _ENGINE_CACHE["engine"] = Engine(load_bars([buffer_path]))
        _ENGINE_CACHE["key"] = key
        _ENGINE_CACHE["native_key"] = None
        _ENGINE_CACHE["native_levels"] = None
    return _ENGINE_CACHE["engine"]


def _evaluation_ms(signal: dict[str, Any]) -> int:
    """First instant at which the complete signal/order state is available."""
    bos_close = int(signal["bos_ms"]) + 60_000
    return max(bos_close, int(signal.get("entry_ms") or bos_close))


def _pool_summary(pool: dict[str, Any] | None) -> dict[str, Any] | None:
    if pool is None:
        return None
    return {
        "id": pool.get("pool_id"),
        "price": pool.get("pool_price"),
        "tier": pool.get("priority_tier"),
        "tier_class": pool.get("priority_class"),
        "status": pool.get("status"),
        "side": pool.get("side"),
        "stacked_confluence": pool.get("stacked_confluence"),
        "constituents": list(pool.get("constituent_levels") or ()),
    }


def unavailable_metadata(reason: str) -> dict[str, Any]:
    """Keep the diagnostic schema present without ever failing a trade path."""
    return {
        "schema": SCHEMA,
        "metadata_status": "UNAVAILABLE",
        "error": str(reason)[:240],
        "selected_dol": None,
        "dol_direction": None,
        "dol_price": None,
        "dol_tier": None,
        "dol_status": "UNAVAILABLE",
        "source_present": None,
        "narrative_class": "NO_CLEAR_NARRATIVE",
        "stacked_constituents": [],
        "successor_dol": None,
        "direction_aligned_with_dol": None,
        "execution_dol": None,
        "execution_dol_price": None,
        "execution_dol_direction": None,
        "execution_dol_source": None,
        "execution_dol_timeframe": None,
        "execution_dol_open_beyond_entry": False,
        "execution_dol_resolution": None,
        "alignment_classification": "AMBIGUOUS",
        "alignment_reason": "DOL metadata unavailable",
        "multi_horizon_alignment": "AMBIGUOUS",
    }


def _dol_direction(pool: dict[str, Any] | None) -> str | None:
    if not pool:
        return None
    if pool.get("side") == "BSL":
        return "LONG"
    if pool.get("side") == "SSL":
        return "SHORT"
    return None


def _execution_snapshot(engine, evaluated_at_ms: int) -> dict[str, Any]:
    """Apply the frozen execution-DOL resolver to bars closed at decision time."""
    from audit_results.ab_dol_qualitative_random10_20260915 import (
        build_two_horizon_preoutcome as frozen_execution,
    )

    cutoff = int(np.searchsorted(engine.ms, evaluated_at_ms - 60_000, side="right") - 1)
    if cutoff < 0:
        raise ValueError("no closed bar available at the causal decision timestamp")
    cache_key = (id(engine), int(getattr(engine, "n", len(engine.ms))))
    if _ENGINE_CACHE.get("native_key") != cache_key:
        _ENGINE_CACHE["native_levels"] = frozen_execution._native_levels(engine)
        _ENGINE_CACHE["native_key"] = cache_key
    return frozen_execution.execution_dol(
        engine, cutoff, evaluated_at_ms, _ENGINE_CACHE.get("native_levels")
    )


def _alignment_snapshot(signal: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, str]:
    """Classify with the already-frozen five-state A/B audit definition."""
    from audit_results.ab_execution_dol_management_audit_20260915.alignment_audit import (
        classify,
    )

    row = {
        "direction": str(signal["dir"]).upper(),
        "entry": float(signal["entry"]),
        "dol_price": metadata.get("dol_price"),
        "htf_dol_direction": metadata.get("dol_direction"),
        "execution_dol_price": metadata.get("execution_dol_price"),
        "execution_dol_direction": metadata.get("execution_dol_direction"),
        "execution_dol_open_beyond_entry": metadata.get("execution_dol_open_beyond_entry"),
        "dol_status": metadata.get("dol_status"),
    }
    return classify(row)


def _market_alignment(metadata: dict[str, Any]) -> str:
    """Describe agreement between the two market layers, independent of a trade."""
    if metadata.get("alignment_classification") == "AMBIGUOUS":
        return "AMBIGUOUS"
    htf = metadata.get("dol_direction")
    execution = metadata.get("execution_dol_direction")
    if not htf or not execution:
        return "AMBIGUOUS"
    if htf != execution:
        return "CONFLICTING"
    return "BULLISH" if htf == "LONG" else "BEARISH"


def attach_metadata(
    signal: dict[str, Any], buffer_path: str, *, engine=None
) -> dict[str, Any]:
    """Attach ranked DOL diagnostics in-place; never alter decision fields."""
    try:
        from detcore.a_cont_v3_ict_dol import tag_setup_dol

        evaluated_at_ms = _evaluation_ms(signal)
        policy_engine = engine if engine is not None else _engine_for(buffer_path)
        tag = tag_setup_dol(
            policy_engine,
            direction=str(signal["dir"]).upper(),
            evaluated_at_ms=evaluated_at_ms,
        )
        current = _pool_summary(tag.current_dol)
        htf_direction = _dol_direction(current)
        expected_side = "BSL" if str(signal["dir"]).upper() == "LONG" else "SSL"
        metadata = {
            "schema": SCHEMA,
            "metadata_status": "ATTACHED",
            "evaluated_at_ms": int(tag.evaluated_at_ms),
            "cutoff_bar": int(tag.cutoff_bar),
            "selected_dol": current["id"] if current else None,
            "dol_direction": htf_direction,
            "dol_price": current["price"] if current else None,
            "dol_tier": current["tier"] if current else None,
            "dol_tier_class": current["tier_class"] if current else None,
            "dol_status": tag.dol_status,
            "source_present": bool(tag.source_present),
            "narrative_class": tag.narrative,
            "stacked_constituents": current["constituents"] if current else [],
            "successor_dol": _pool_summary(tag.next_dol_after_delivery),
            "direction_aligned_with_dol": (
                bool(current and current["side"] == expected_side)
            ),
        }
        # Execution-DOL failure degrades this layer to AMBIGUOUS.  The already
        # attached HTF DOL remains available and no exception can enter the
        # canonical signal/execution path.
        try:
            execution = _execution_snapshot(policy_engine, evaluated_at_ms)
            ep = execution.get("price")
            ed = execution.get("direction")
            entry = float(signal["entry"])
            open_beyond = bool(
                ep is not None
                and ((ed == "LONG" and float(ep) > entry)
                     or (ed == "SHORT" and float(ep) < entry))
            )
            metadata.update({
                "execution_dol": execution.get("id") or execution.get("type"),
                "execution_dol_price": ep,
                "execution_dol_direction": ed,
                "execution_dol_source": execution.get("type"),
                "execution_dol_timeframe": execution.get("tf"),
                "execution_dol_open_beyond_entry": open_beyond,
                "execution_dol_resolution": execution.get("resolution"),
                "execution_dol_constituents": list(execution.get("constituents") or ()),
                "execution_dol_selection_reason": execution.get("selection_reason"),
            })
            state, reason = _alignment_snapshot(signal, metadata)
            metadata["alignment_classification"] = state
            metadata["alignment_reason"] = reason
        except Exception as exc:
            metadata.update({
                "execution_dol": None,
                "execution_dol_price": None,
                "execution_dol_direction": None,
                "execution_dol_source": None,
                "execution_dol_timeframe": None,
                "execution_dol_open_beyond_entry": False,
                "execution_dol_resolution": None,
                "execution_dol_constituents": [],
                "execution_dol_selection_reason": None,
                "alignment_classification": "AMBIGUOUS",
                "alignment_reason": "execution DOL unavailable: " + str(exc)[:180],
            })
        metadata["multi_horizon_alignment"] = _market_alignment(metadata)
    except Exception as exc:
        metadata = unavailable_metadata(exc)
    signal["_dol"] = metadata
    return metadata
