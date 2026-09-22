"""Causal ICT narrative tags for the frozen A Continuation V2 population.

This module is deliberately read-only with respect to V2.  It consumes the
persisted V2 lifecycle timestamps and the existing causal liquidity-level
registry; it never changes setup eligibility, orders, or account chronology.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from typing import Any, Iterable


TICK = 0.25
SOURCE_WINDOW_BARS = 120  # Existing registered ICT event window; not tuned for V3.


@dataclass(frozen=True)
class NarrativeTag:
    evaluated_at_ms: int
    cutoff_bar: int
    source: dict[str, Any] | None
    destination: dict[str, Any] | None
    objective_status: str
    narrative: str
    dealing_range: dict[str, Any] | None
    location_bucket: str | None
    room_r: float | None
    room_bucket: str | None
    session_tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["session_tags"] = list(self.session_tags)
        return row


def _level_class(level) -> str:
    if level.kind in {"day", "week"}:
        return "external liquidity"
    if level.kind == "session":
        return "session liquidity"
    if level.kind == "H1_equal":
        return "equal highs/lows"
    return "internal/local liquidity"


def _class_rank(level) -> int:
    return {
        "external liquidity": 0,
        "session liquidity": 1,
        "equal highs/lows": 2,
        "internal/local liquidity": 3,
    }[_level_class(level)]


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


def _iso(engine, index: int, close_available: bool = False) -> str:
    ms = int(engine.ms[int(index)]) + (60_000 if close_available else 0)
    import pandas as pd
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def _event_type(engine, level) -> tuple[str, float, int | None]:
    i = int(level.touch)
    if i >= engine.n:
        return "UNTOUCHED", 0.0, None
    if level.side == 1:
        depth = float(engine.h[i] - level.price)
        close_through = float(engine.c[i]) >= float(level.price) + TICK
        reclaimed = depth >= TICK and float(engine.c[i]) < float(level.price)
    else:
        depth = float(level.price - engine.l[i])
        close_through = float(engine.c[i]) <= float(level.price) - TICK
        reclaimed = depth >= TICK and float(engine.c[i]) > float(level.price)
    if depth < TICK:
        kind = "TOUCH"
    elif close_through:
        kind = "CLOSE_THROUGH"
    elif reclaimed:
        kind = "RECLAIM"
    else:
        kind = "TRADE_THROUGH_RAID"
    return kind, max(0.0, depth), i


def _source(engine, direction: int, cutoff: int):
    lo = max(0, cutoff - SOURCE_WINDOW_BARS + 1)
    candidates = []
    for level in engine.levels:
        if level.side != -direction or level.born > cutoff:
            continue
        if not (lo <= level.touch <= cutoff and level.touch < level.expires):
            continue
        event_type, depth, _ = _event_type(engine, level)
        if event_type == "TOUCH":
            continue
        candidates.append((level, event_type, depth))
    if not candidates:
        return None, None
    # The initiating take is the most recent causal event.  Ties prefer the
    # broadest supported liquidity family, then a stable level ID.
    level, event_type, depth = min(
        candidates, key=lambda x: (-int(x[0].touch), _class_rank(x[0]), x[0].id))
    row = {
        "liquidity_source_id": level.id,
        "liquidity_type": level.kind,
        "source_class": _level_class(level),
        "price": float(level.price),
        "side": "BSL" if level.side == 1 else "SSL",
        "formed_at": _iso(engine, level.source_start),
        "available_at": _iso(engine, level.born),
        "take_time": _iso(engine, level.touch, close_available=True),
        "take_bar": int(level.touch),
        "event_type": event_type,
        "sweep_depth_points": depth,
        "previously_consumed": False,
        "session": _session_at(engine, level.source_start) if level.kind == "session" else None,
    }
    return level, row


def _objective_candidates(engine, direction: int, at: int, boundary: float):
    return [
        level for level in engine.levels
        if level.side == direction
        and level.born <= at < level.expires
        and level.touch > at
        and direction * float(level.price) > direction * float(boundary)
    ]


def _choose_objective(engine, direction: int, at: int, boundary: float):
    choices = _objective_candidates(engine, direction, at, boundary)
    if not choices:
        return None
    return min(
        choices,
        key=lambda level: (
            direction * (float(level.price) - float(boundary)),
            _class_rank(level),
            level.id,
        ),
    )


def _destination(engine, direction: int, cutoff: int, source_level):
    selected_at = cutoff
    selection_basis = "setup_start"
    objective = None
    if source_level is not None:
        selected_at = int(source_level.touch)
        selection_basis = "source_take"
        objective = _choose_objective(
            engine, direction, selected_at, float(engine.c[selected_at]))
    if objective is None:
        selected_at = cutoff
        selection_basis = "setup_start"
        objective = _choose_objective(
            engine, direction, cutoff, float(engine.c[cutoff]))
    if objective is None:
        return None, "UNCLEAR"

    if int(objective.touch) <= cutoff:
        status = "DELIVERED"
        delivered_at = _iso(engine, objective.touch, close_available=True)
    elif int(objective.expires) <= cutoff:
        status = "UNCLEAR"
        delivered_at = None
    else:
        status = "OPEN"
        delivered_at = None
    row = {
        "target_liquidity_id": objective.id,
        "target_type": objective.kind,
        "target_class": _level_class(objective),
        "target_price": float(objective.price),
        "side": "BSL" if objective.side == 1 else "SSL",
        "formed_at": _iso(engine, objective.source_start),
        "available_at": _iso(engine, objective.born),
        "selected_at": _iso(engine, selected_at, close_available=True),
        "selection_basis": selection_basis,
        "status": status,
        "delivered_at": delivered_at,
    }
    return row, status


def _location(source: dict[str, Any] | None, destination: dict[str, Any] | None,
              current: float):
    if source is None or destination is None:
        return None, None
    low = min(float(source["price"]), float(destination["target_price"]))
    high = max(float(source["price"]), float(destination["target_price"]))
    if high <= low:
        return None, None
    percentile = 100.0 * (float(current) - low) / (high - low)
    if not 0.0 <= percentile <= 100.0:
        bucket = "OUTSIDE"
    elif percentile < 25.0:
        bucket = "0–25%"
    elif percentile < 50.0:
        bucket = "25–50%"
    elif percentile < 75.0:
        bucket = "50–75%"
    else:
        bucket = "75–100%"
    return {
        "low": low,
        "high": high,
        "midpoint": (low + high) / 2.0,
        "current_price": float(current),
        "percentile": percentile,
    }, bucket


def _room(direction: int, destination: dict[str, Any] | None,
          entry: float, stop: float):
    if destination is None:
        return None, None
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return None, None
    room = direction * (float(destination["target_price"]) - float(entry)) / risk
    if room < 1.0:
        bucket = "<1R"
    elif room <= 2.0:
        bucket = "1–2R"
    else:
        bucket = ">2R"
    return room, bucket


def _session_tags(engine, direction: int, cutoff: int) -> tuple[str, ...]:
    lo = max(0, cutoff - SOURCE_WINDOW_BARS + 1)
    tags = set()
    for level in engine.levels:
        if level.kind != "session" or not (lo <= level.touch <= cutoff):
            continue
        event_type, _depth, _ = _event_type(engine, level)
        if event_type == "TOUCH":
            continue
        name = _session_at(engine, level.source_start)
        if name == "ASIA":
            tags.add("ASIA_LIQUIDITY_TAKEN")
        elif name == "LONDON":
            tags.add("LONDON_LIQUIDITY_TAKEN")

    completed = [level for level in engine.levels
                 if level.kind == "session" and level.side == direction
                 and level.born <= cutoff]
    if completed:
        prior = max(completed, key=lambda level: (level.born, level.id))
        if prior.touch <= cutoff:
            tags.add("PRIOR_SESSION_OBJECTIVE_DELIVERED")
        elif cutoff < prior.expires:
            tags.add("PRIOR_SESSION_OBJECTIVE_OPEN")
    return tuple(sorted(tags))


def tag_setup(engine, *, direction: str, evaluated_at_ms: int,
              entry: float, stop: float) -> NarrativeTag:
    """Attach causal narrative state at the completed catalyst bar.

    ``evaluated_at_ms`` is the first instant at which the catalyst bar is fully
    known.  The bar at ``evaluated_at_ms - 60s`` is therefore the latest usable
    market observation.
    """
    z = 1 if direction == "LONG" else -1
    cutoff = bisect.bisect_right(engine.ms, int(evaluated_at_ms) - 60_000) - 1
    if cutoff < 0 or cutoff >= engine.n:
        raise ValueError("Narrative timestamp outside cached market data")
    source_level, source = _source(engine, z, cutoff)
    destination, status = _destination(engine, z, cutoff, source_level)
    if status == "DELIVERED":
        narrative = "Objective Already Delivered"
    elif source is not None and status == "OPEN":
        narrative = "Complete ICT Narrative"
    elif source is not None:
        narrative = "Source Only"
    elif status == "OPEN":
        narrative = "Destination Only"
    else:
        narrative = "No Clear Narrative"
    current = float(engine.c[cutoff])
    dealing_range, location_bucket = _location(source, destination, current)
    room_r, room_bucket = _room(z, destination, entry, stop)
    return NarrativeTag(
        evaluated_at_ms=int(evaluated_at_ms),
        cutoff_bar=cutoff,
        source=source,
        destination=destination,
        objective_status=status,
        narrative=narrative,
        dealing_range=dealing_range,
        location_bucket=location_bucket,
        room_r=room_r,
        room_bucket=room_bucket,
        session_tags=_session_tags(engine, z, cutoff),
    )


def prefix_projection(tag: NarrativeTag) -> dict[str, Any]:
    """Stable scalar projection used by closed-prefix tests and registration."""
    row = tag.to_dict()
    return {
        "source": row["source"],
        "destination": row["destination"],
        "objective_status": row["objective_status"],
        "narrative": row["narrative"],
        "dealing_range": row["dealing_range"],
        "location_bucket": row["location_bucket"],
        "room_r": row["room_r"],
        "room_bucket": row["room_bucket"],
        "session_tags": row["session_tags"],
    }
