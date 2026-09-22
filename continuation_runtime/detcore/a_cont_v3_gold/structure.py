"""Retracement-relative hold, structure, BOS, and 62% entry logic."""

from __future__ import annotations

from dataclasses import dataclass

from .types import Bar, Direction, EventArray


def hold_boundary(array: EventArray) -> float:
    width = array.upper - array.lower
    return (
        array.lower + 0.4 * width
        if array.direction is Direction.LONG
        else array.upper - 0.4 * width
    )


def body_holds(array: EventArray, bar: Bar) -> bool:
    boundary = hold_boundary(array)
    return bar.close >= boundary if array.direction is Direction.LONG else bar.close <= boundary


def entry_62(direction: Direction, origin: float, bos_extreme: float) -> float:
    if direction is Direction.LONG:
        if bos_extreme <= origin:
            raise ValueError("long BOS extreme must exceed retracement origin")
        return origin + 0.38 * (bos_extreme - origin)
    if bos_extreme >= origin:
        raise ValueError("short BOS extreme must be below retracement origin")
    return origin - 0.38 * (origin - bos_extreme)


@dataclass(slots=True)
class RetracementStructure:
    direction: Direction
    array: EventArray
    started_at: int
    protected_origin: float
    reference: float | None
    reference_available_at: int | None

    @classmethod
    def begin(cls, direction: Direction, array: EventArray, bar: Bar) -> "RetracementStructure":
        origin = bar.low if direction is Direction.LONG else bar.high
        opposing = bar.bearish if direction is Direction.LONG else bar.bullish
        reference = bar.open if opposing else None
        available_at = bar.index if opposing else None
        return cls(direction, array, bar.index, origin, reference, available_at)

    def update_origin(self, bar: Bar) -> None:
        if self.direction is Direction.LONG:
            self.protected_origin = min(self.protected_origin, bar.low)
        else:
            self.protected_origin = max(self.protected_origin, bar.high)

    def update_reference_from_opposing_sequence(self, bar: Bar) -> None:
        opposing = bar.bearish if self.direction is Direction.LONG else bar.bullish
        if opposing and self.reference is None:
            # Freeze the first causal pullback-sequence open. Later bars cannot
            # ratchet the reference toward price.
            self.reference = bar.open
            self.reference_available_at = bar.index

    def bos(self, bar: Bar) -> bool:
        if (
            self.reference is None
            or self.reference_available_at is None
            or self.reference_available_at >= bar.index
        ):
            return False
        return bar.close > self.reference if self.direction is Direction.LONG else bar.close < self.reference
