"""Causal ICT session-cycle source/objective ledger for V3 research.

This module is deliberately separate from ``a_cont_v2`` and from the original
V3 tagger.  It represents liquidity state only; it neither creates trades nor
reads outcomes.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd


TICK = 0.25
CLUSTER_TOLERANCE_POINTS = 4.0  # existing equal-high/low tolerance


@dataclass(frozen=True)
class LedgerLevel:
    id: str
    kind: str
    side: int
    price: float
    source_start: int
    born: int
    expires: int
    touch_hint: int


@dataclass(frozen=True)
class LedgerTag:
    evaluated_at_ms: int
    cutoff_bar: int
    direction: str
    source_candidates: tuple[dict[str, Any], ...]
    selected_source: dict[str, Any] | None
    superseded_sources: tuple[dict[str, Any], ...]
    objective_candidates: tuple[dict[str, Any], ...]
    selected_objective: dict[str, Any] | None
    nearest_objective: dict[str, Any] | None
    objective_status: str
    narrative: str
    dealing_range: dict[str, Any] | None
    source_selection_reason: str | None
    objective_selection_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_candidates"] = list(value["source_candidates"])
        value["superseded_sources"] = list(value["superseded_sources"])
        value["objective_candidates"] = list(value["objective_candidates"])
        return value


def _iso(engine, index: int, *, close_available: bool = False) -> str:
    ms = int(engine.ms[int(index)]) + (60_000 if close_available else 0)
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def _bar_index(engine, value: str | int | None, default: int) -> int:
    if value is None:
        return int(default)
    if isinstance(value, int):
        return int(value)
    ms = int(pd.Timestamp(value).timestamp() * 1000)
    return int(np.searchsorted(engine.ms, ms, side="left"))


def _session_at(engine, index: int) -> str:
    stamp = engine.f.ts.iloc[int(index)].tz_convert("Etc/GMT+4")
    minute = stamp.hour * 60 + stamp.minute
    if minute >= 1200:
        return "ASIA"
    if 120 <= minute < 300:
        return "LONDON"
    if 570 <= minute < 720:
        return "NYAM"
    if 720 <= minute < 780:
        return "NY_LUNCH"
    if 810 <= minute < 960:
        return "NYPM"
    if 960 <= minute < 1200:
        return "PM_AH"
    return "PREM"


def _family(level: LedgerLevel, engine) -> tuple[str, int, str | None]:
    session = _session_at(engine, level.source_start) if level.kind == "session" else None
    if level.kind in {"day", "week"}:
        return "EXTERNAL_DAY_WEEK", 0, session
    if level.kind == "H1_equal":
        return "H1_EQUAL", 1, session
    if level.kind == "session" and session in {"ASIA", "LONDON", "NYAM", "NY_LUNCH", "NYPM"}:
        return "MAJOR_SESSION", 2, session
    if level.kind == "H1_swing":
        return "H1_SWING", 3, session
    if level.kind == "session":
        return "INTERNAL_SESSION", 4, session
    return "OTHER_SUPPORTED", 5, session


def _native_levels(engine) -> list[LedgerLevel]:
    return [
        LedgerLevel(
            id=str(level.id), kind=str(level.kind), side=int(level.side),
            price=float(level.price), source_start=int(level.source_start),
            born=int(level.born), expires=int(level.expires),
            touch_hint=int(level.touch),
        )
        for level in engine.levels
        if level.kind in {"day", "week", "session", "H1_equal"}
    ]


def levels_from_review_packet(engine, packet: dict[str, Any]) -> list[LedgerLevel]:
    """Convert the cached causal review universe into ledger levels.

    The original blind packets stored the native and closed-H1 objects visible
    to the reviewer.  Reusing that exact finite catalog makes the retest compare
    representation logic rather than candidate-generation changes.  This
    adapter reads no trade result.
    """
    merged: dict[str, dict[str, Any]] = {}
    for key in ("manual_source_candidates", "manual_destination_candidates"):
        for row in packet.get(key, []):
            merged[str(row["id"])] = row
    old_source = packet.get("auto_source")
    if old_source:
        merged.setdefault(str(old_source["liquidity_source_id"]), {
            "id": old_source["liquidity_source_id"],
            "kind": old_source["liquidity_type"],
            "side": old_source["side"], "price": old_source["price"],
            "formed_at": old_source["formed_at"],
            "available_at": old_source["available_at"],
            "take_time": old_source["take_time"],
        })
    old_destination = packet.get("auto_destination")
    if old_destination:
        merged.setdefault(str(old_destination["target_liquidity_id"]), {
            "id": old_destination["target_liquidity_id"],
            "kind": old_destination["target_type"],
            "side": old_destination["side"],
            "price": old_destination["target_price"],
            "formed_at": old_destination["formed_at"],
            "available_at": old_destination["available_at"],
            "delivered_at": old_destination.get("delivered_at"),
        })
    native = {str(level.id): level for level in engine.levels}
    out = []
    for row in merged.values():
        if str(row["id"]) in native:
            level = native[str(row["id"])]
            out.append(LedgerLevel(
                id=str(level.id), kind=str(level.kind), side=int(level.side),
                price=float(level.price), source_start=int(level.source_start),
                born=int(level.born), expires=int(level.expires),
                touch_hint=int(level.touch),
            ))
            continue
        born = _bar_index(engine, row.get("available_at"), 0)
        source_start = _bar_index(engine, row.get("formed_at"), born)
        take_value = row.get("take_time") or row.get("delivered_at")
        touch = _bar_index(engine, take_value, engine.n)
        # Availability/touch strings refer to the first instant after a closed
        # bar.  The causal market event therefore occurred one bar earlier.
        if take_value is not None:
            touch = max(born, touch - 1)
        out.append(LedgerLevel(
            id=str(row["id"]), kind="H1_swing",
            side=1 if row["side"] == "BSL" else -1,
            price=float(row["price"]), source_start=source_start, born=born,
            expires=engine.n, touch_hint=touch,
        ))
    return out


def _first(mask: np.ndarray, start: int) -> int | None:
    found = np.flatnonzero(mask)
    return int(start + found[0]) if len(found) else None


def _events(engine, level: LedgerLevel, cutoff: int, direction: int) -> dict[str, Any]:
    start = max(0, int(level.born))
    end = min(int(cutoff), engine.n - 1)
    family, rank, session = _family(level, engine)
    base = {
        "id": level.id,
        "liquidity_family": family,
        "structural_priority_class": rank,
        "kind": level.kind,
        "session": session,
        "side": "BSL" if level.side == 1 else "SSL",
        "price": float(level.price),
        "formed_at": _iso(engine, level.source_start),
        "available_at": _iso(engine, level.born),
    }
    if start > end:
        return {**base, "first_touch": None, "first_penetration": None,
                "first_close_through": None, "resolution_time": None,
                "consumed": False, "narrative_active": False,
                "invalidated_at": None}

    if level.side == 1:
        touch_mask = engine.h[start:end + 1] >= level.price
        penetration_mask = engine.h[start:end + 1] >= level.price + TICK
        close_mask = engine.c[start:end + 1] >= level.price + TICK
    else:
        touch_mask = engine.l[start:end + 1] <= level.price
        penetration_mask = engine.l[start:end + 1] <= level.price - TICK
        close_mask = engine.c[start:end + 1] <= level.price - TICK
    touch = _first(touch_mask, start)
    penetration = _first(penetration_mask, start)
    close_through = _first(close_mask, start)

    resolution = None
    invalidated = None
    if penetration is not None:
        if direction == 1:  # bullish resolution after taking SSL
            resolution = _first(engine.c[penetration:end + 1] >= level.price + TICK, penetration)
            if resolution is not None and resolution < end:
                invalidated = _first(
                    engine.c[resolution + 1:end + 1] <= level.price - TICK,
                    resolution + 1,
                )
        else:  # bearish resolution after taking BSL
            resolution = _first(engine.c[penetration:end + 1] <= level.price - TICK, penetration)
            if resolution is not None and resolution < end:
                invalidated = _first(
                    engine.c[resolution + 1:end + 1] >= level.price + TICK,
                    resolution + 1,
                )
    active = resolution is not None and invalidated is None
    return {
        **base,
        "first_touch": _iso(engine, touch, close_available=True) if touch is not None else None,
        "first_touch_bar": touch,
        "first_penetration": _iso(engine, penetration, close_available=True) if penetration is not None else None,
        "first_penetration_bar": penetration,
        "first_close_through": _iso(engine, close_through, close_available=True) if close_through is not None else None,
        "first_close_through_bar": close_through,
        "resolution_time": _iso(engine, resolution, close_available=True) if resolution is not None else None,
        "resolution_bar": resolution,
        "consumed": penetration is not None,
        "narrative_active": active,
        "invalidated_at": _iso(engine, invalidated, close_available=True) if invalidated is not None else None,
        "invalidated_bar": invalidated,
    }


def _cluster(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: (float(row["price"]), row["id"]))
    clusters: list[list[dict[str, Any]]] = []
    for row in ordered:
        if not clusters or abs(float(row["price"]) - float(clusters[-1][-1]["price"])) > CLUSTER_TOLERANCE_POINTS:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    return clusters


def _select_source(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    active = [row for row in rows if row["narrative_active"]]
    if not active:
        return None, [], None
    clusters = _cluster(active)
    winning = max(
        clusters,
        key=lambda group: (
            max(int(row["resolution_bar"]) for row in group),
            -min(int(row["structural_priority_class"]) for row in group),
            max(float(row["price"]) for row in group),
        ),
    )
    selected = min(
        winning,
        key=lambda row: (int(row["structural_priority_class"]),
                         -int(row["resolution_bar"]), row["id"]),
    )
    superseded = [row for row in active if row["id"] not in {x["id"] for x in winning}]
    reason = (
        "latest directionally resolved, non-invalidated source cluster; "
        "confluent representative chosen by external/equal/major-session/"
        "H1-swing/internal-session precedence"
    )
    return selected, superseded, reason


def _objective_state(engine, level: LedgerLevel, cutoff: int) -> dict[str, Any]:
    family, rank, session = _family(level, engine)
    touch = int(level.touch_hint)
    delivered = touch <= cutoff
    return {
        "id": level.id,
        "liquidity_family": family,
        "structural_priority_class": rank,
        "kind": level.kind,
        "session": session,
        "side": "BSL" if level.side == 1 else "SSL",
        "price": float(level.price),
        "formed_at": _iso(engine, level.source_start),
        "available_at": _iso(engine, level.born),
        "status": "DELIVERED" if delivered else "OPEN",
        "delivered_at": _iso(engine, touch, close_available=True) if delivered else None,
        "delivered_bar": touch if delivered else None,
    }


def _choose_objective(engine, rows: list[dict[str, Any]], direction: int,
                      cutoff: int) -> tuple[dict[str, Any] | None,
                                           dict[str, Any] | None, str, str | None]:
    current = float(engine.c[cutoff])
    directional = [row for row in rows
                   if direction * (float(row["price"]) - current) > 0]
    open_rows = [row for row in directional if row["status"] == "OPEN"]
    nearest = min(
        open_rows,
        key=lambda row: (direction * (float(row["price"]) - current),
                         int(row["structural_priority_class"]), row["id"]),
        default=None,
    )
    if open_rows:
        clusters = _cluster(open_rows)
        clusters.sort(key=lambda group: min(
            direction * (float(row["price"]) - current) for row in group))
        chosen_cluster = None
        for group in clusters:
            price = min((float(row["price"]) for row in group),
                        key=lambda p: direction * (p - current))
            # An allegedly open level inside the fully known bar is an
            # in-progress interaction, not the next clean draw.
            if float(engine.l[cutoff]) <= price <= float(engine.h[cutoff]):
                continue
            chosen_cluster = group
            break
        if chosen_cluster is not None:
            selected = min(
                chosen_cluster,
                key=lambda row: (int(row["structural_priority_class"]),
                                 direction * (float(row["price"]) - current), row["id"]),
            )
            reason = (
                "next open price cluster in the causal delivery ladder; "
                "representative chosen by semantic precedence; delivered and "
                "current-bar-interaction clusters skipped"
            )
            return selected, nearest, "OPEN", reason

    delivered = [row for row in directional if row["status"] == "DELIVERED"]
    if delivered:
        # No successor remains open.  Preserve the most recently delivered
        # meaningful directional objective instead of reporting a false void.
        selected = max(
            delivered,
            key=lambda row: (int(row["delivered_bar"]),
                             -int(row["structural_priority_class"]), row["id"]),
        )
        return selected, nearest, "DELIVERED", (
            "no open successor; retain the most recently delivered meaningful "
            "directional objective"
        )
    return None, nearest, "UNRESOLVED", None


def _dealing_range(source: dict[str, Any] | None,
                   destination: dict[str, Any] | None, current: float):
    if source is None or destination is None:
        return None
    low = min(float(source["price"]), float(destination["price"]))
    high = max(float(source["price"]), float(destination["price"]))
    if high <= low:
        return None
    return {
        "low": low, "high": high, "midpoint": (low + high) / 2.0,
        "current_price": float(current),
        "current_price_percentile": 100.0 * (float(current) - low) / (high - low),
    }


def tag_setup_ledger(engine, *, direction: str, evaluated_at_ms: int,
                     extra_levels: Iterable[LedgerLevel] = (),
                     include_native_levels: bool = True) -> LedgerTag:
    """Return the causal source/objective ledger state at a completed bar."""
    z = 1 if direction == "LONG" else -1
    cutoff = bisect.bisect_right(engine.ms, int(evaluated_at_ms) - 60_000) - 1
    if cutoff < 0 or cutoff >= engine.n:
        raise ValueError("Ledger timestamp outside cached market data")

    unique: dict[str, LedgerLevel] = (
        {level.id: level for level in _native_levels(engine)}
        if include_native_levels else {}
    )
    unique.update({level.id: level for level in extra_levels})
    levels = [level for level in unique.values() if level.born <= cutoff]

    # Walk backward through source events until the newest active source
    # cluster is established.  Anything older is semantically superseded and
    # cannot win selection, so rescanning years of already superseded events is
    # both unnecessary and misleading.  This is event-driven, not age-driven:
    # if recent events are unresolved/invalidated, the walk continues as far
    # back as needed.
    source_rows = []
    source_levels = sorted(
        (level for level in levels
         if level.side == -z and level.touch_hint <= cutoff),
        key=lambda level: (-int(level.touch_hint), level.id),
    )
    active_touch = None
    active_price = None
    for level in source_levels:
        if (active_touch is not None and int(level.touch_hint) < active_touch
                and abs(float(level.price) - float(active_price)) > CLUSTER_TOLERANCE_POINTS):
            break
        row = _events(engine, level, cutoff, z)
        source_rows.append(row)
        if row["narrative_active"] and active_touch is None:
            active_touch = int(level.touch_hint)
            active_price = float(level.price)
    source_rows.sort(key=lambda row: (
        -(row["resolution_bar"] if row["resolution_bar"] is not None else -1),
        int(row["structural_priority_class"]), row["id"],
    ))
    source, superseded, source_reason = _select_source(source_rows)

    objective_rows = [
        _objective_state(engine, level, cutoff)
        for level in levels
        if level.side == z and cutoff < level.expires
    ]
    objective_rows.sort(key=lambda row: (
        row["status"] != "OPEN",
        z * (float(row["price"]) - float(engine.c[cutoff])),
        int(row["structural_priority_class"]), row["id"],
    ))
    destination, nearest, status, objective_reason = _choose_objective(
        engine, objective_rows, z, cutoff)

    if source is not None and status == "OPEN":
        narrative = "COMPLETE"
    elif source is not None and status == "DELIVERED":
        narrative = "OBJECTIVE DELIVERED"
    elif source is not None:
        narrative = "SOURCE ONLY"
    elif status == "OPEN":
        narrative = "DESTINATION ONLY"
    elif status == "DELIVERED":
        narrative = "OBJECTIVE DELIVERED"
    else:
        narrative = "NO CLEAR NARRATIVE"

    return LedgerTag(
        evaluated_at_ms=int(evaluated_at_ms), cutoff_bar=cutoff,
        direction=direction, source_candidates=tuple(source_rows),
        selected_source=source, superseded_sources=tuple(superseded),
        objective_candidates=tuple(objective_rows),
        selected_objective=destination, nearest_objective=nearest,
        objective_status=status, narrative=narrative,
        dealing_range=_dealing_range(source, destination, float(engine.c[cutoff])),
        source_selection_reason=source_reason,
        objective_selection_reason=objective_reason,
    )


def prefix_projection(tag: LedgerTag) -> dict[str, Any]:
    """Stable representation-only projection for closed-prefix tests."""
    row = tag.to_dict()
    return {
        key: row[key] for key in (
            "direction", "source_candidates", "selected_source",
            "superseded_sources", "objective_candidates", "selected_objective",
            "nearest_objective", "objective_status", "narrative",
            "dealing_range", "source_selection_reason",
            "objective_selection_reason",
        )
    }
