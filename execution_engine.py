"""Pure bar-by-bar execution semantics shared by tests and live observers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from execution_plan import ExecutionPlan, Side


@dataclass(slots=True)
class ExecutionState:
    status: str = "pending"  # pending | open | closed | no_fill
    filled_ms: int | None = None
    closed_ms: int | None = None
    remaining_leg_indexes: list[int] = field(default_factory=list)
    realized_gross_r: float = 0.0
    outcome: str | None = None

    @classmethod
    def for_plan(cls, plan: ExecutionPlan) -> "ExecutionState":
        return cls(remaining_leg_indexes=list(range(len(plan.legs))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "filled_ms": self.filled_ms,
            "closed_ms": self.closed_ms,
            "remaining_leg_indexes": list(self.remaining_leg_indexes),
            "realized_gross_r": self.realized_gross_r,
            "outcome": self.outcome,
        }


def entry_filled(plan: ExecutionPlan, high: float, low: float) -> bool:
    through = plan.fill_through_ticks * plan.tick_size
    if through == 0:
        return float(low) <= plan.entry <= float(high)
    if plan.side is Side.LONG:
        return float(low) <= plan.entry - through
    return float(high) >= plan.entry + through


def stop_hit(plan: ExecutionPlan, high: float, low: float) -> bool:
    if plan.side is Side.LONG:
        return float(low) <= plan.stop_loss
    return float(high) >= plan.stop_loss


def target_hit(plan: ExecutionPlan, target: float, high: float, low: float) -> bool:
    if plan.side is Side.LONG:
        return float(high) >= target
    return float(low) <= target


def step(plan: ExecutionPlan, state: ExecutionState, *, bar_ms: int, high: float, low: float) -> ExecutionState:
    """Advance one plan by one completed OHLC bar using conservative ordering."""
    bar_ms = int(bar_ms)
    if state.status in ("closed", "no_fill"):
        return state
    if state.status == "pending":
        if bar_ms < plan.active_from_ms:
            return state
        if bar_ms >= plan.valid_until_ms:
            state.status = "no_fill"
            state.outcome = "no_fill"
            state.closed_ms = bar_ms
            return state
        if not entry_filled(plan, high, low):
            return state
        state.status = "open"
        state.filled_ms = bar_ms
        # A stop on the fill bar is observable adverse risk and therefore counts.
        if plan.adverse_first and stop_hit(plan, high, low):
            state.realized_gross_r = -1.0
            state.remaining_leg_indexes.clear()
            state.status = "closed"
            state.outcome = "loss"
            state.closed_ms = bar_ms
            return state
        # A favourable high/low may have happened before the limit fill.  Ignore it by default.
        if not plan.allow_fill_bar_take_profit:
            return state

    if state.status != "open":
        return state
    if stop_hit(plan, high, low):
        remaining_fraction = sum(plan.legs[i].quantity for i in state.remaining_leg_indexes) / plan.total_quantity
        state.realized_gross_r -= remaining_fraction
        state.remaining_leg_indexes.clear()
        state.status = "closed"
        state.outcome = "loss" if state.realized_gross_r < 0 else "partial"
        state.closed_ms = bar_ms
        return state

    hit: list[int] = []
    for i in state.remaining_leg_indexes:
        leg = plan.legs[i]
        if target_hit(plan, leg.take_profit, high, low):
            fraction = leg.quantity / plan.total_quantity
            state.realized_gross_r += fraction * plan.reward_r(leg.take_profit)
            hit.append(i)
    if hit:
        state.remaining_leg_indexes = [i for i in state.remaining_leg_indexes if i not in hit]
        if not state.remaining_leg_indexes:
            state.status = "closed"
            state.outcome = "win"
            state.closed_ms = bar_ms
    return state


def resolve_arrays(
    plan: ExecutionPlan,
    ms: Sequence[int],
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    hold_bars: int = 2880,
) -> dict[str, Any]:
    """Resolve a plan against chronological bar arrays without look-ahead in the rules."""
    import numpy as np

    if not len(ms):
        return {"outcome": "open"}
    start = int(np.searchsorted(ms, plan.active_from_ms, side="left"))
    state = ExecutionState.for_plan(plan)
    filled_index: int | None = None
    for i in range(start, len(ms)):
        if state.status == "open" and filled_index is not None and i - filled_index >= hold_bars:
            state.status = "closed"
            state.outcome = "timeout"
            state.closed_ms = int(ms[i])
            break
        before = state.status
        step(plan, state, bar_ms=int(ms[i]), high=float(highs[i]), low=float(lows[i]))
        if before == "pending" and state.status in ("open", "closed") and state.filled_ms is not None:
            filled_index = i
        if state.status in ("closed", "no_fill"):
            break
    if state.status == "pending":
        if int(ms[-1]) >= plan.valid_until_ms:
            return {"outcome": "no_fill"}
        return {"outcome": "open"}
    if state.status == "open":
        return {"outcome": "open", "fill_ms": state.filled_ms}
    return {
        "outcome": state.outcome,
        "gross_R": round(state.realized_gross_r, 6),
        "fill_ms": state.filled_ms,
        "closed_ms": state.closed_ms,
    }
