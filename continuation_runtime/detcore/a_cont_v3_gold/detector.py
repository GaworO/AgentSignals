"""A_CONT_V3_GOLD streaming state machine.

The detector consumes completed bars only. It is isolated from production V2,
contains no outcome/P&L code, and emits correspondence candidates at BOS close.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalyst_adapter import CatalystAdapter, ConfirmedLocalLiquidity
from .event_envelope import EventEnvelope, basic_fvg, causal_envelope_start
from .structure import RetracementStructure, body_holds, entry_62
from .types import Bar, Candidate, Catalyst, Direction, LiquidityPool, State, StateTransition


class CandidateRegistry:
    """Run-local physical identity registry; it contains no time cooldown."""

    def __init__(self) -> None:
        self._keys: set[tuple[object, ...]] = set()

    def claim(self, key: tuple[object, ...]) -> bool:
        if key in self._keys:
            return False
        self._keys.add(key)
        return True


@dataclass(slots=True)
class _RuntimeEvent:
    envelope: EventEnvelope
    structure: RetracementStructure | None = None


class AContinuationV3GoldDetector:
    def __init__(
        self,
        catalyst_adapter: CatalystAdapter | list[LiquidityPool] | tuple[LiquidityPool, ...],
        *,
        enable_cllc: bool = True,
        local_pivot_strength: int = 2,
        minimum_fvg_size: float = 0.0,
    ) -> None:
        self.catalysts = (
            catalyst_adapter
            if isinstance(catalyst_adapter, CatalystAdapter)
            else CatalystAdapter(catalyst_adapter)
        )
        self.local = ConfirmedLocalLiquidity(local_pivot_strength) if enable_cllc else None
        self.minimum_fvg_size = minimum_fvg_size
        self.history: list[Bar] = []
        self.active: dict[str, _RuntimeEvent] = {}
        self._events: list[EventEnvelope] = []
        self.candidates: list[Candidate] = []
        self.transitions: list[StateTransition] = []
        self._candidate_registry = CandidateRegistry()

    @property
    def events(self) -> list[EventEnvelope]:
        return list(self._events)

    def state_signature(self) -> tuple[object, ...]:
        """Stable prefix signature used by truncation/repeat-run tests."""
        events = tuple(
            (
                e.event_id,
                e.state.value,
                e.start_bar,
                e.end_bar,
                tuple(a.array_id for a in e.arrays),
                e.selected_array.array_id if e.selected_array else None,
                e.retracement_bar,
                e.terminal_reason,
            )
            for e in self.events
        )
        candidates = tuple(c.physical_key for c in self.candidates)
        transitions = tuple((t.event_id, t.state.value, t.bar_index, t.reason) for t in self.transitions)
        return events, candidates, transitions

    def _transition(self, event: EventEnvelope, state: State, bar: int, reason: str) -> None:
        event.state = state
        self.transitions.append(StateTransition(event.event_id, state, bar, reason))

    def _new_event(self, catalyst: Catalyst) -> _RuntimeEvent:
        start = causal_envelope_start(self.history, catalyst)
        event_id = f"EVT|{catalyst.direction.value}|{catalyst.bar_index}|{'|'.join(catalyst.pool_ids)}"
        env = EventEnvelope(event_id, catalyst, start)
        runtime = _RuntimeEvent(env)
        self.active[event_id] = runtime
        self._events.append(env)
        self._transition(env, State.BUILD_EVENT, catalyst.bar_index, "closed-candle catalyst")
        # Retrospective grouping reads only bars already closed at catalyst time.
        relevant = [bar for bar in self.history if start <= bar.index <= catalyst.bar_index]
        for end in range(3, len(relevant) + 1):
            array = basic_fvg(relevant[:end], event_id, self.minimum_fvg_size)
            if array is not None and array.direction is catalyst.direction:
                env.add_array(array)
        if env.arrays:
            self._transition(env, State.WAIT_RETRACEMENT, catalyst.bar_index, "owned array set available")
        return runtime

    def _handle_catalyst(self, catalyst: Catalyst) -> None:
        # An opposite directional delivery replaces unfinished events.
        for event_id, runtime in list(self.active.items()):
            env = runtime.envelope
            if env.direction is not catalyst.direction and env.state not in {State.ORDER_READY, State.TERMINAL}:
                env.terminal_reason = "opposite_direction_reset"
                self._transition(env, State.TERMINAL, catalyst.bar_index, env.terminal_reason)
                del self.active[event_id]

        # A later source crossed inside the same un-retraced physical envelope
        # extends that event. Once an array/retracement identity is locked, a
        # later source can form an independent M24-style event.
        start = causal_envelope_start(self.history, catalyst)
        for runtime in self.active.values():
            env = runtime.envelope
            if (
                env.direction is catalyst.direction
                and env.selected_array is None
                and env.start_bar == start
            ):
                env.merge_catalyst(catalyst)
                return
        self._new_event(catalyst)

    def _advance_event(self, runtime: _RuntimeEvent, bar: Bar) -> None:
        env = runtime.envelope
        if env.state in {State.ORDER_READY, State.TERMINAL}:
            return

        # Every aligned array born while the event remains unselected belongs
        # to the immutable event-owned set.
        if env.selected_array is None:
            array = basic_fvg(self.history, env.event_id, self.minimum_fvg_size)
            if array is not None and array.direction is env.direction and array.available_at >= env.start_bar:
                before = env.state
                env.add_array(array)
                if before is State.BUILD_EVENT and env.state is State.WAIT_RETRACEMENT:
                    self._transition(env, State.WAIT_RETRACEMENT, bar.index, "first owned array available")

        if env.state is State.WAIT_RETRACEMENT:
            selected = env.first_touched(bar)
            if selected is not None:
                if not body_holds(selected, bar):
                    env.terminal_reason = "selected_array_body_close_failed_0.6"
                    self._transition(env, State.TERMINAL, bar.index, env.terminal_reason)
                    return
                runtime.structure = RetracementStructure.begin(env.direction, selected, bar)
                self._transition(env, State.TRACK_RETRACEMENT, bar.index, "first touched owned array locked; 0.6 held")
                return  # array birth/touch/hold can never also produce BOS

        if env.state is not State.TRACK_RETRACEMENT or runtime.structure is None:
            return
        structure = runtime.structure
        if not body_holds(structure.array, bar):
            env.terminal_reason = "selected_array_body_close_failed_0.6"
            self._transition(env, State.TERMINAL, bar.index, env.terminal_reason)
            return
        structure.update_origin(bar)
        structure.update_reference_from_opposing_sequence(bar)
        if structure.bos(bar):
            bos_extreme = bar.high if env.direction is Direction.LONG else bar.low
            candidate = Candidate(
                candidate_id=f"V3|{env.direction.value}|{env.event_id}|{bar.index}",
                subtype=env.catalyst.subtype,
                direction=env.direction,
                event_id=env.event_id,
                catalyst_id=env.catalyst.catalyst_id,
                catalyst_pool_ids=env.catalyst.pool_ids,
                catalyst_bar=env.catalyst.bar_index,
                envelope_start=env.start_bar,
                envelope_end=env.end_bar if env.end_bar is not None else bar.index - 1,
                selected_array_id=structure.array.array_id,
                array_bounds=(structure.array.lower, structure.array.upper),
                retracement_bar=structure.started_at,
                protected_origin=structure.protected_origin,
                bos_reference=structure.reference,
                bos_reference_available_at=structure.reference_available_at,
                bos_bar=bar.index,
                bos_extreme=bos_extreme,
                entry_62=entry_62(env.direction, structure.protected_origin, bos_extreme),
                dol=env.catalyst.dol,
            )
            if self._candidate_registry.claim(candidate.physical_key):
                self.candidates.append(candidate)
                self._transition(env, State.ORDER_READY, bar.index, "closed BOS through pre-existing retracement reference")
            else:
                env.terminal_reason = "physical_duplicate_merged"
                self._transition(env, State.TERMINAL, bar.index, env.terminal_reason)

    def step(self, bar: Bar) -> tuple[Candidate, ...]:
        if self.history and bar.index <= self.history[-1].index:
            raise ValueError("bars must arrive once, in strictly increasing index order")
        previous = self.history[-1] if self.history else None
        self.history.append(bar)

        if self.local is not None:
            for pool in self.local.on_close(self.history):
                self.catalysts.register(pool)
        for catalyst in self.catalysts.on_close(previous, bar):
            self._handle_catalyst(catalyst)

        before = len(self.candidates)
        for runtime in list(self.active.values()):
            self._advance_event(runtime, bar)
        return tuple(self.candidates[before:])

    def run(self, bars: list[Bar]) -> tuple[Candidate, ...]:
        for bar in bars:
            self.step(bar)
        return tuple(self.candidates)
