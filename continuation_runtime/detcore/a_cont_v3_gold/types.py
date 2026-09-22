"""Immutable public records for A_CONT_V3_GOLD.

Bar indices denote completed bars. ``available_at`` is the first completed-bar
index at which an object may be consulted; callers must still require it to be
strictly earlier than a candle which breaks that object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Subtype(str, Enum):
    RLC = "RLC"
    CLLC = "CLLC"


class State(str, Enum):
    SEEK_CATALYST = "SEEK_CATALYST"
    BUILD_EVENT = "BUILD_EVENT"
    WAIT_RETRACEMENT = "WAIT_RETRACEMENT"
    TRACK_RETRACEMENT = "TRACK_RETRACEMENT"
    ORDER_READY = "ORDER_READY"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class Bar:
    index: int
    timestamp: Any
    open: float
    high: float
    low: float
    close: float

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    pool_id: str
    side: str  # BSL or SSL
    lower: float
    upper: float
    available_at: int
    kind: str = "registered"
    priority_tier: int | None = None
    status: str = "OPEN"
    constituent_ids: tuple[str, ...] = ()
    formed_at: int | None = None

    def __post_init__(self) -> None:
        if self.side not in {"BSL", "SSL"}:
            raise ValueError("liquidity side must be BSL or SSL")
        if self.lower > self.upper:
            raise ValueError("pool lower bound exceeds upper bound")


@dataclass(frozen=True, slots=True)
class DOLSnapshot:
    captured_at: int
    current_dol: str | None = None
    current_tier: int | None = None
    current_status: str | None = None
    successor_dol: str | None = None
    narrative: str = "UNAVAILABLE"
    source_pool_ids: tuple[str, ...] = ()
    ranked_pool_ids: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    @classmethod
    def unavailable(cls, captured_at: int, reason: str) -> "DOLSnapshot":
        return cls(captured_at=captured_at, unavailable_reason=reason)


@dataclass(frozen=True, slots=True)
class Catalyst:
    catalyst_id: str
    subtype: Subtype
    direction: Direction
    pool_ids: tuple[str, ...]
    bar_index: int
    lower: float
    upper: float
    dol: DOLSnapshot


@dataclass(frozen=True, slots=True)
class EventArray:
    array_id: str
    formation_bars: tuple[int, int, int]
    available_at: int
    owner_event_id: str
    lower: float
    upper: float
    direction: Direction


@dataclass(frozen=True, slots=True)
class StateTransition:
    event_id: str
    state: State
    bar_index: int
    reason: str


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    subtype: Subtype
    direction: Direction
    event_id: str
    catalyst_id: str
    catalyst_pool_ids: tuple[str, ...]
    catalyst_bar: int
    envelope_start: int
    envelope_end: int
    selected_array_id: str
    array_bounds: tuple[float, float]
    retracement_bar: int
    protected_origin: float
    bos_reference: float
    bos_reference_available_at: int
    bos_bar: int
    bos_extreme: float
    entry_62: float
    dol: DOLSnapshot
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def physical_key(self) -> tuple[Any, ...]:
        """Identity used to merge physical duplicate/re-entry emissions."""
        return (
            self.direction,
            self.selected_array_id,
            self.retracement_bar,
            round(self.protected_origin, 8),
            round(self.bos_reference, 8),
            self.bos_bar,
        )
