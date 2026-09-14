"""Read-only ranked-DOL metadata adapter for canonical live A/B signals.

The ranked representation and source ledger remain owned by
``detcore.a_cont_v3_ict_dol``.  This module only adapts the live buffer and a
canonical A/B record to that frozen API.  It has no execution or broker path.
"""
from __future__ import annotations

import os
from typing import Any


SCHEMA = "AB_RANKED_DOL_V1"
_ENGINE_CACHE: dict[str, Any] = {"key": None, "engine": None}


def _engine_for(buffer_path: str):
    """Build the existing causal policy engine once per live buffer version."""
    stat = os.stat(buffer_path)
    key = (os.path.abspath(buffer_path), int(stat.st_mtime_ns), int(stat.st_size))
    if _ENGINE_CACHE["key"] != key:
        from audit_results.ict_125.policy import Engine, load_bars

        _ENGINE_CACHE["engine"] = Engine(load_bars([buffer_path]))
        _ENGINE_CACHE["key"] = key
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
        "dol_price": None,
        "dol_tier": None,
        "dol_status": "UNAVAILABLE",
        "source_present": None,
        "narrative_class": "NO_CLEAR_NARRATIVE",
        "stacked_constituents": [],
        "successor_dol": None,
        "direction_aligned_with_dol": None,
    }


def attach_metadata(
    signal: dict[str, Any], buffer_path: str, *, engine=None
) -> dict[str, Any]:
    """Attach ranked DOL diagnostics in-place; never alter decision fields."""
    try:
        from detcore.a_cont_v3_ict_dol import tag_setup_dol

        evaluated_at_ms = _evaluation_ms(signal)
        tag = tag_setup_dol(
            engine if engine is not None else _engine_for(buffer_path),
            direction=str(signal["dir"]).upper(),
            evaluated_at_ms=evaluated_at_ms,
        )
        current = _pool_summary(tag.current_dol)
        expected_side = "BSL" if str(signal["dir"]).upper() == "LONG" else "SSL"
        metadata = {
            "schema": SCHEMA,
            "metadata_status": "ATTACHED",
            "evaluated_at_ms": int(tag.evaluated_at_ms),
            "cutoff_bar": int(tag.cutoff_bar),
            "selected_dol": current["id"] if current else None,
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
    except Exception as exc:
        metadata = unavailable_metadata(exc)
    signal["_dol"] = metadata
    return metadata

