"""Causal event envelopes and immutable event-owned FVG arrays."""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Bar, Catalyst, Direction, EventArray, State


def basic_fvg(bars: list[Bar], owner_event_id: str, minimum_size: float = 0.0) -> EventArray | None:
    """Return an aligned three-candle FVG born at the latest completed bar."""
    if len(bars) < 3:
        return None
    a, _, c = bars[-3:]
    direction: Direction | None = None
    lower = upper = 0.0
    if c.low > a.high and c.low - a.high >= minimum_size:
        direction, lower, upper = Direction.LONG, a.high, c.low
    elif c.high < a.low and a.low - c.high >= minimum_size:
        direction, lower, upper = Direction.SHORT, c.high, a.low
    if direction is None:
        return None
    return EventArray(
        array_id=f"FVG|{direction.value}|{a.index}|{bars[-2].index}|{c.index}|{lower:.8f}|{upper:.8f}",
        formation_bars=(a.index, bars[-2].index, c.index),
        available_at=c.index,
        owner_event_id=owner_event_id,
        lower=float(lower),
        upper=float(upper),
        direction=direction,
    )


def causal_envelope_start(history: list[Bar], catalyst: Catalyst, strength: int = 1) -> int:
    """Latest causally confirmed counter-directional pivot before the catalyst.

    This is a structural boundary, not an ATR/body threshold. It permits mixed
    colours and allows the catalyst at the start, middle, or end of the event.
    """
    if not history:
        return catalyst.bar_index
    end = len(history) - 1
    for p in range(end - strength, strength - 1, -1):
        if p + strength > end:
            continue
        win = history[p - strength : p + strength + 1]
        if catalyst.direction is Direction.LONG:
            ok = history[p].low == min(x.low for x in win) and sum(
                x.low == history[p].low for x in win
            ) == 1
        else:
            ok = history[p].high == max(x.high for x in win) and sum(
                x.high == history[p].high for x in win
            ) == 1
        if ok:
            return history[p].index
    return history[0].index


@dataclass(slots=True)
class EventEnvelope:
    event_id: str
    catalyst: Catalyst
    start_bar: int
    state: State = State.BUILD_EVENT
    end_bar: int | None = None
    arrays: list[EventArray] = field(default_factory=list)
    selected_array: EventArray | None = None
    retracement_bar: int | None = None
    terminal_reason: str | None = None

    @property
    def direction(self) -> Direction:
        return self.catalyst.direction

    def add_array(self, array: EventArray) -> None:
        if array.owner_event_id != self.event_id:
            raise ValueError("cannot rewrite array ownership")
        if array.direction is not self.direction:
            return
        if array.array_id not in {x.array_id for x in self.arrays}:
            self.arrays.append(array)
            self.arrays.sort(key=lambda x: (x.available_at, x.array_id))
        if self.state is State.BUILD_EVENT:
            self.state = State.WAIT_RETRACEMENT

    def first_touched(self, bar: Bar) -> EventArray | None:
        if self.selected_array is not None:
            return self.selected_array
        eligible = [
            array
            for array in self.arrays
            if array.available_at < bar.index
            and bar.low <= array.upper
            and bar.high >= array.lower
        ]
        if not eligible:
            return None
        # Retracing from above reaches the highest bullish array first; retracing
        # from below reaches the lowest bearish array first.
        if self.direction is Direction.LONG:
            chosen = max(eligible, key=lambda x: (x.upper, -x.available_at, x.array_id))
        else:
            chosen = min(eligible, key=lambda x: (x.lower, x.available_at, x.array_id))
        self.selected_array = chosen
        self.retracement_bar = bar.index
        self.end_bar = bar.index - 1
        self.state = State.TRACK_RETRACEMENT
        return chosen

    def merge_catalyst(self, catalyst: Catalyst) -> None:
        """Merge a same physical un-retraced directional close-through."""
        if catalyst.direction is not self.direction or self.selected_array is not None:
            raise ValueError("only an unselected same-direction event can merge")
        ids = tuple(sorted(set(self.catalyst.pool_ids + catalyst.pool_ids)))
        self.catalyst = Catalyst(
            catalyst_id=f"CATSET|{self.direction.value}|{self.catalyst.bar_index}|{'|'.join(ids)}",
            subtype=self.catalyst.subtype,
            direction=self.direction,
            pool_ids=ids,
            bar_index=self.catalyst.bar_index,
            lower=min(self.catalyst.lower, catalyst.lower),
            upper=max(self.catalyst.upper, catalyst.upper),
            dol=self.catalyst.dol,
        )
