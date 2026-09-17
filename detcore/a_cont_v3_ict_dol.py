"""Outcome-free ranked Draw-on-Liquidity representation for V3 research.

This layer deliberately preserves the set-based source implementation and the
existing ledger.  It replaces only the ledger's single destination choice with
an ordered map of causally available directional liquidity pools.  It neither
creates trades nor reads trade outcomes.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from detcore.a_cont_v3_ict_ledger import (
    CLUSTER_TOLERANCE_POINTS,
    LedgerLevel,
    _cluster,
    _native_levels,
    _objective_state,
)
from detcore.a_cont_v3_ict_set import tag_setup_set


TIER_NAMES = {
    1: "HIGHER_TIMEFRAME_EXTERNAL",
    2: "DAILY_EXTERNAL",
    3: "SESSION_EXTERNAL",
    4: "LOCAL_INTERNAL",
    5: "INTERNAL_PD_ARRAY",
}


@dataclass(frozen=True)
class DOLNarrativeTag:
    evaluated_at_ms: int
    cutoff_bar: int
    direction: str
    current_price: float
    active_source_set: tuple[dict[str, Any], ...]
    source_present: bool
    active_source_count: int
    source_families: tuple[str, ...]
    opposite_side_liquidity_previously_taken: bool
    all_directional_liquidity_pools: tuple[dict[str, Any], ...]
    open_directional_liquidity_pools: tuple[dict[str, Any], ...]
    current_dol: dict[str, Any] | None
    next_dol_after_delivery: dict[str, Any] | None
    dol_status: str
    dol_selection_reason: str | None
    narrative: str

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in (
            "active_source_set",
            "source_families",
            "all_directional_liquidity_pools",
            "open_directional_liquidity_pools",
        ):
            row[key] = list(row[key])
        return row


def _tier(level: dict[str, Any]) -> int:
    """Map only already-supported liquidity types to the registered classes."""
    kind = str(level["kind"])
    if kind in {"week", "H1_equal", "H1_swing"}:
        return 1
    if kind == "day":
        return 2
    if kind == "session":
        return 3
    if kind in {"local_swing", "intraday_swing", "LTF_swing"}:
        return 4
    if kind in {"FVG", "imbalance", "PD_array"}:
        return 5
    # This branch is diagnostic only and cannot manufacture a new concept.
    # Any future supported but unclassified level is conservatively internal.
    return 4


def _pool(group: list[dict[str, Any]], direction: int,
          current: float, cutoff_low: float, cutoff_high: float) -> dict[str, Any]:
    members = sorted(group, key=lambda row: (float(row["price"]), row["id"]))
    open_members = [row for row in members if row["status"] == "OPEN"]
    eligible_members = [
        row for row in open_members
        if direction * (float(row["price"]) - current) > 0
    ]
    prices = [float(row["price"]) for row in members]
    tier = min(_tier(row) for row in members)
    pool_low, pool_high = min(prices), max(prices)
    if eligible_members:
        representative = min(
            eligible_members,
            key=lambda row: (
                direction * (float(row["price"]) - current),
                _tier(row), row["id"],
            ),
        )
        distance = direction * (float(representative["price"]) - current)
    else:
        representative = min(
            members,
            key=lambda row: (abs(float(row["price"]) - current),
                             _tier(row), row["id"]),
        )
        distance = direction * (float(representative["price"]) - current)
    status = "OPEN" if eligible_members else "DELIVERED"
    interacting = bool(
        eligible_members and (
            distance <= CLUSTER_TOLERANCE_POINTS
            or pool_low <= cutoff_high and pool_high >= cutoff_low
        )
    )
    external_internal = (
        "EXTERNAL" if tier <= 3 else
        "LOCAL_INTERNAL" if tier == 4 else "INTERNAL_PD_ARRAY"
    )
    constituents = [
        {
            "id": row["id"], "kind": row["kind"],
            "liquidity_family": row["liquidity_family"],
            "price": float(row["price"]), "status": row["status"],
            "priority_tier": _tier(row),
        }
        for row in members
    ]
    return {
        "pool_id": "POOL|" + "|".join(row["id"] for row in members),
        "side": representative["side"],
        "status": status,
        "priority_tier": tier,
        "priority_class": TIER_NAMES[tier],
        "highest_timeframe_represented": TIER_NAMES[tier],
        "external_internal": external_internal,
        "pool_low": pool_low,
        "pool_high": pool_high,
        "pool_price": float(representative["price"]),
        "distance_points": float(distance),
        "stacked_confluence": len(members),
        "is_stacked": len(members) > 1,
        "constituent_levels": constituents,
        "currently_interacting": interacting,
        "selection_eligible": bool(eligible_members and not interacting),
    }


def _rank_open_pools(pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the causal intraday delivery ladder without a weekly-always rule.

    Primary DOL is external when any meaningful external pool is available.
    Within that trade-horizon class, proximity is primary; tier and stacking
    resolve otherwise equivalent choices.  Internal pools are retained in the
    map and become eligible only when no external objective remains.
    """
    eligible = [row for row in pools if row["selection_eligible"]]
    external = [row for row in eligible if row["external_internal"] == "EXTERNAL"]
    primary = external if external else eligible
    primary_ids = {row["pool_id"] for row in primary}
    return sorted(
        eligible,
        key=lambda row: (
            0 if row["pool_id"] in primary_ids else 1,
            float(row["distance_points"]),
            int(row["priority_tier"]),
            -int(row["stacked_confluence"]),
            row["pool_id"],
        ),
    )


def tag_setup_dol(
    engine,
    *,
    direction: str,
    evaluated_at_ms: int,
    extra_levels: Iterable[LedgerLevel] = (),
    include_native_levels: bool = True,
    replacement_cycles: Iterable[dict[str, Any]] = (),
) -> DOLNarrativeTag:
    """Return the ranked source-set -> active delivery -> DOL state."""
    z = 1 if direction == "LONG" else -1
    cutoff = bisect.bisect_right(engine.ms, int(evaluated_at_ms) - 60_000) - 1
    if cutoff < 0 or cutoff >= engine.n:
        raise ValueError("DOL timestamp outside cached market data")
    current = float(engine.c[cutoff])

    # Source logic is intentionally delegated unchanged to the already-audited
    # set model.  Its legacy destination fields are ignored by this layer.
    set_tag = tag_setup_set(
        engine,
        direction=direction,
        evaluated_at_ms=evaluated_at_ms,
        extra_levels=extra_levels,
        include_native_levels=include_native_levels,
        replacement_cycles=replacement_cycles,
    )

    unique = ({level.id: level for level in _native_levels(engine)}
              if include_native_levels else {})
    unique.update({level.id: level for level in extra_levels})
    levels = [level for level in unique.values() if level.born <= cutoff]
    objective_rows = [
        _objective_state(engine, level, cutoff)
        for level in levels
        if level.side == z and cutoff < level.expires
    ]
    directional = [
        row for row in objective_rows
        if z * (float(row["price"]) - current) > 0
    ]
    pools = [
        _pool(group, z, current, float(engine.l[cutoff]), float(engine.h[cutoff]))
        for group in _cluster(directional)
    ]
    pools.sort(key=lambda row: (
        row["status"] != "OPEN", float(row["distance_points"]),
        int(row["priority_tier"]), -int(row["stacked_confluence"]),
        row["pool_id"],
    ))
    open_pools = [row for row in pools if row["status"] == "OPEN"]
    ranked = _rank_open_pools(pools)

    if ranked:
        current_dol = ranked[0]
        next_dol = ranked[1] if len(ranked) > 1 else None
        status = "OPEN"
        reason = (
            "nearest meaningful OPEN external pool for the intraday delivery "
            "horizon; internal pools defer while external liquidity remains; "
            "tier and stacked confluence break equivalent choices"
        )
    else:
        delivered = [row for row in pools if row["status"] == "DELIVERED"]
        current_dol = max(
            delivered,
            key=lambda row: max(
                (int(member.get("delivered_bar") or -1)
                 for member in objective_rows
                 if member["id"] in {
                     item["id"] for item in row["constituent_levels"]
                 }),
                default=-1,
            ),
            default=None,
        )
        next_dol = None
        status = "DELIVERED" if current_dol is not None else "UNRESOLVED"
        reason = (
            "previous meaningful directional pool delivered; no next eligible "
            "OPEN pool remains"
            if current_dol is not None else None
        )

    source_present = bool(set_tag.source_present)
    if source_present and status == "OPEN":
        narrative = "COMPLETE_DOL_NARRATIVE"
    elif status == "OPEN":
        narrative = "DOL_ONLY"
    elif source_present and status == "DELIVERED":
        narrative = "OBJECTIVE_DELIVERED"
    elif source_present:
        narrative = "SOURCE_ONLY"
    elif status == "DELIVERED":
        narrative = "OBJECTIVE_DELIVERED"
    else:
        narrative = "NO_CLEAR_NARRATIVE"

    return DOLNarrativeTag(
        evaluated_at_ms=int(evaluated_at_ms), cutoff_bar=cutoff,
        direction=direction, current_price=current,
        active_source_set=set_tag.active_source_set,
        source_present=source_present,
        active_source_count=set_tag.active_source_count,
        source_families=set_tag.source_families,
        opposite_side_liquidity_previously_taken=source_present,
        all_directional_liquidity_pools=tuple(pools),
        open_directional_liquidity_pools=tuple(open_pools),
        current_dol=current_dol,
        next_dol_after_delivery=next_dol,
        dol_status=status, dol_selection_reason=reason,
        narrative=narrative,
    )
