"""Catalyst-only amendment for ``A_CONT_FINAL_RESEARCH``.

The canonical V2 lifecycle is intentionally not copied or changed here.  This
module only adds causally confirmed current-session liquidity and folds every
same-bar, same-direction consuming interaction into one catalyst event before
the unchanged V2 evaluator is called.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .a_cont_v2 import CatalystEvent, gap_zone_index, merge_continuation_events


LOCAL_EQUAL_TOLERANCE = 4.0  # existing project equal-high/equal-low tolerance


@dataclass(frozen=True)
class LocalLiquidityLevel:
    level_id: str
    side: int                 # +1 high/BSL, -1 low/SSL
    price: float
    pivot_bar: int
    available_bar: int        # close of the second right-hand bar
    session_start: int
    session_end: int


@dataclass(frozen=True)
class FinalCatalystEvent(CatalystEvent):
    catalyst_type: str = "REGISTERED"
    event_identity: str = ""
    constituent_levels: tuple[dict, ...] = ()


def confirmed_local_levels(ctx, allowed_dates: set[str] | None = None):
    """Return 2-left/2-right local pivots, available only after confirmation."""
    levels: list[LocalLiquidityLevel] = []
    for session_name, start, end, _high, _low in ctx.sessinst:
        start, end = int(start), int(end)
        if end - start < 4:
            continue
        if allowed_dates is not None and str(ctx.dates[start]) not in allowed_dates:
            continue
        session_levels: list[LocalLiquidityLevel] = []
        for bar in range(start + 2, end - 1):
            left_h = max(float(ctx.hi[bar - 2]), float(ctx.hi[bar - 1]))
            right_h = max(float(ctx.hi[bar + 1]), float(ctx.hi[bar + 2]))
            left_l = min(float(ctx.lo[bar - 2]), float(ctx.lo[bar - 1]))
            right_l = min(float(ctx.lo[bar + 1]), float(ctx.lo[bar + 2]))
            available = bar + 2
            if float(ctx.hi[bar]) >= max(left_h, right_h):
                session_levels.append(LocalLiquidityLevel(
                    f"LOCAL_HIGH|{session_name}|{bar}", 1, float(ctx.hi[bar]),
                    bar, available, start, end,
                ))
            if float(ctx.lo[bar]) <= min(left_l, right_l):
                session_levels.append(LocalLiquidityLevel(
                    f"LOCAL_LOW|{session_name}|{bar}", -1, float(ctx.lo[bar]),
                    bar, available, start, end,
                ))
        # Reuse the existing equal-level semantics: two already-confirmed
        # same-side pivots within the project tolerance form one additional
        # pool, available only when the later pivot itself is confirmed.
        for side in (1, -1):
            same_side = [level for level in session_levels if level.side == side]
            for left_index, left in enumerate(same_side):
                for right in same_side[left_index + 1:]:
                    if abs(left.price - right.price) > LOCAL_EQUAL_TOLERANCE:
                        continue
                    levels.append(LocalLiquidityLevel(
                        f"LOCAL_EQUAL_{'HIGH' if side == 1 else 'LOW'}|{session_name}|"
                        f"{left.pivot_bar}|{right.pivot_bar}",
                        side, round((left.price + right.price) / 2.0, 2),
                        right.pivot_bar, right.available_bar, start, end,
                    ))
        levels.extend(session_levels)
    return levels


def local_continuation_events(ctx, allowed_dates: set[str] | None = None):
    """Emit only the first consuming interaction of each confirmed local pool.

    A wick-only first interaction consumes the pool but emits no continuation.
    Same-bar levels are returned separately here and clustered by the final
    merger, which also combines them with registered liquidity on that bar.
    """
    events: list[FinalCatalystEvent] = []
    for level in confirmed_local_levels(ctx, allowed_dates):
        trigger = None
        for bar in range(level.available_bar + 1, level.session_end + 1):
            touched = (float(ctx.hi[bar]) >= level.price if level.side == 1
                       else float(ctx.lo[bar]) <= level.price)
            if not touched:
                continue
            closed_through = (float(ctx.cl[bar]) > level.price if level.side == 1
                              else float(ctx.cl[bar]) < level.price)
            if closed_through:
                trigger = bar
            break  # first consuming interaction closes the pool either way
        if trigger is None:
            continue
        direction = "LONG" if level.side == 1 else "SHORT"
        equal = "LOCAL_EQUAL_" in level.level_id
        constituent = {
            "level_id": level.level_id,
            "kind": (("LOCAL_EQUAL_HIGH" if level.side == 1 else "LOCAL_EQUAL_LOW")
                     if equal else
                     ("LOCAL_SWING_HIGH" if level.side == 1 else "LOCAL_SWING_LOW")),
            "price": level.price,
            "pivot_bar": level.pivot_bar,
            "available_bar": level.available_bar,
        }
        events.append(FinalCatalystEvent(
            trigger, direction, ("LOCAL_BSL" if level.side == 1 else "LOCAL_SSL",),
            (), (), "LOCAL", "", (constituent,),
        ))
    return events


def _cluster_constituents(rows: Iterable[dict]):
    ordered = sorted(rows, key=lambda row: (float(row.get("price", 0.0)), row["level_id"]))
    clusters: list[list[dict]] = []
    for row in ordered:
        if (not clusters or
                abs(float(row.get("price", 0.0)) - float(clusters[-1][-1].get("price", 0.0)))
                > LOCAL_EQUAL_TOLERANCE):
            clusters.append([row])
        else:
            clusters[-1].append(row)
    return clusters


def merge_final_continuation_events(groups, ctx, allowed_dates: set[str] | None = None):
    """Return one event per direction and first consuming interaction bar."""
    registered = merge_continuation_events(groups, ctx, gap_zone_index(ctx))
    if allowed_dates is not None:
        registered = [event for event in registered
                      if str(ctx.dates[int(event.trigger_bar)]) in allowed_dates]
    local = local_continuation_events(ctx, allowed_dates)

    slots: dict[tuple[int, str], dict] = {}
    for event in [*registered, *local]:
        key = (int(event.trigger_bar), event.direction)
        slot = slots.setdefault(key, {
            "cats": [], "groups": [], "breaks": [], "registered": False,
            "local": False, "constituents": [],
        })
        for catalyst in event.catalysts:
            if catalyst not in slot["cats"]:
                slot["cats"].append(catalyst)
        slot["groups"].extend(event.group_ids)
        slot["breaks"].extend(event.breaks)
        if isinstance(event, FinalCatalystEvent):
            slot["local"] = True
            slot["constituents"].extend(event.constituent_levels)
        else:
            slot["registered"] = True

    output = []
    for (bar, direction), slot in sorted(slots.items()):
        local_clusters = _cluster_constituents(slot["constituents"])
        constituents = tuple(row for cluster in local_clusters for row in cluster)
        kind = ("REGISTERED+LOCAL" if slot["registered"] and slot["local"] else
                "LOCAL" if slot["local"] else "REGISTERED")
        identity_parts = [str(group) for group in sorted(set(slot["groups"]))]
        identity_parts.extend(row["level_id"] for row in constituents)
        identity = f"{direction}|{bar}|" + ",".join(identity_parts)
        output.append(FinalCatalystEvent(
            bar, direction, tuple(slot["cats"]),
            tuple(sorted(set(slot["groups"]))), tuple(slot["breaks"]),
            kind, identity, constituents,
        ))
    return output
