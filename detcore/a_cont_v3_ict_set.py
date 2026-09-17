"""Set-based causal ICT narrative representation for V3 research.

No unique source owner is selected.  This module records all resolved sources
that remain compatible with the current delivery and combines source presence
with the frozen V3 objective ledger.  It never reads or changes trade outcomes.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from detcore.a_cont_v3_ict_ledger import (
    LedgerLevel, _events, _native_levels, _objective_state, _choose_objective,
)


@dataclass(frozen=True)
class SetNarrativeTag:
    evaluated_at_ms: int
    cutoff_bar: int
    direction: str
    active_source_set: tuple[dict[str, Any], ...]
    inactive_source_set: tuple[dict[str, Any], ...]
    source_present: bool
    active_source_count: int
    source_families: tuple[str, ...]
    nearest_source: dict[str, Any] | None
    oldest_source: dict[str, Any] | None
    objective_candidates: tuple[dict[str, Any], ...]
    destination: dict[str, Any] | None
    objective_status: str
    narrative: str
    replacement_cycle: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("active_source_set", "inactive_source_set",
                    "source_families", "objective_candidates"):
            row[key] = list(row[key])
        return row


def tag_setup_set(
    engine,
    *,
    direction: str,
    evaluated_at_ms: int,
    extra_levels: Iterable[LedgerLevel] = (),
    include_native_levels: bool = True,
    replacement_cycles: Iterable[dict[str, Any]] = (),
) -> SetNarrativeTag:
    """Return outcome-free set-based narrative state at a closed-bar cutoff.

    A source is *not* deleted by its old ledger ``invalidated_at`` field.  It is
    inactive only when it never directionally resolved, is currently
    incompatible with delivery, or a later fully qualified opposing delivery
    cycle has superseded it.  The replacement-cycle producer must enforce the
    registered five-part liquidity-resolution/displacement/BOS/objective rule.
    """
    z = 1 if direction == "LONG" else -1
    cutoff = bisect.bisect_right(engine.ms, int(evaluated_at_ms) - 60_000) - 1
    if cutoff < 0 or cutoff >= engine.n:
        raise ValueError("Set narrative timestamp outside cached market data")
    current = float(engine.c[cutoff])

    unique = ({level.id: level for level in _native_levels(engine)}
              if include_native_levels else {})
    unique.update({level.id: level for level in extra_levels})
    levels = [level for level in unique.values() if level.born <= cutoff]

    opposing_cycles = [cycle for cycle in replacement_cycles
                       if int(cycle["bar"]) <= cutoff
                       and int(cycle["direction_int"]) == -z
                       and bool(cycle.get("qualified", False))]
    last_replacement = max(opposing_cycles, key=lambda cycle: int(cycle["bar"]),
                           default=None)
    replacement_bar = int(last_replacement["bar"]) if last_replacement else -1

    active, inactive = [], []
    for level in levels:
        if level.side != -z or level.touch_hint > cutoff:
            continue
        event = _events(engine, level, cutoff, z)
        resolution = event.get("resolution_bar")
        if resolution is None:
            event["current_active"] = False
            event["invalidation_reason"] = "no_directional_resolution"
            inactive.append(event)
            continue
        if int(resolution) <= replacement_bar:
            event["current_active"] = False
            event["invalidation_reason"] = "qualified_opposite_replacement_cycle"
            event["replacement_cycle_id"] = last_replacement.get("id")
            inactive.append(event)
            continue
        if z * (current - float(level.price)) <= 0:
            # Dormant is not permanent deletion.  It can re-enter the active
            # set if delivery returns to the compatible side before a reset.
            event["current_active"] = False
            event["invalidation_reason"] = "currently_incompatible_with_delivery"
            inactive.append(event)
            continue
        event["current_active"] = True
        event["invalidation_reason"] = None
        active.append(event)

    active.sort(key=lambda row: (float(row["price"]), row["id"]))
    inactive.sort(key=lambda row: (float(row["price"]), row["id"]))
    nearest = min(active, key=lambda row: abs(current - float(row["price"])),
                  default=None)
    oldest = min(active, key=lambda row: (int(row["resolution_bar"]), row["id"]),
                 default=None)

    objective_rows = [
        _objective_state(engine, level, cutoff)
        for level in levels
        if level.side == z and cutoff < level.expires
    ]
    destination, _nearest_objective, status, _reason = _choose_objective(
        engine, objective_rows, z, cutoff)

    source_present = bool(active)
    if source_present and status == "OPEN":
        narrative = "COMPLETE_SET_NARRATIVE"
    elif source_present and status == "DELIVERED":
        narrative = "OBJECTIVE_DELIVERED"
    elif source_present:
        narrative = "SOURCE_ONLY"
    elif status == "OPEN":
        narrative = "DESTINATION_ONLY"
    else:
        narrative = "NO_CLEAR_NARRATIVE"

    return SetNarrativeTag(
        evaluated_at_ms=int(evaluated_at_ms), cutoff_bar=cutoff,
        direction=direction, active_source_set=tuple(active),
        inactive_source_set=tuple(inactive), source_present=source_present,
        active_source_count=len(active),
        source_families=tuple(sorted({row["liquidity_family"] for row in active})),
        nearest_source=nearest, oldest_source=oldest,
        objective_candidates=tuple(objective_rows), destination=destination,
        objective_status=status, narrative=narrative,
        replacement_cycle=last_replacement,
    )
