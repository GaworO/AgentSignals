"""Evidence-driven decision state machine for Tanya reconstruction.

This module orchestrates explicitly selected setup rules. It does NOT infer
discretionary context, produce broker orders, or claim autonomous 1:1 fidelity.
Every observation has an availability timestamp and provenance. Review intents
never create fills. Unknown entry/exit prices are never replaced by chart prices.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional
import math
from tanya_replay import aggregate, fvg_events


@dataclass(frozen=True)
class Fact:
    value: Optional[bool]
    known_at: int
    source: str
    provenance: str = 'manual_annotation'

    def __post_init__(self):
        if self.value is not None and type(self.value) is not bool:
            raise ValueError('Facts must be true, false, or unknown')
        if not self.source or self.provenance not in ('manual_annotation', 'market_data', 'transcript', 'screenshot', 'ai_inference'):
            raise ValueError('Explicit supported provenance required')


@dataclass(frozen=True)
class Plan:
    id: str
    direction: str
    known_at: int
    requirements: tuple
    requires_smt: Optional[bool]
    source: str

    def __post_init__(self):
        if self.direction not in ('long', 'short') or not self.id or not self.source:
            raise ValueError('Named, sourced directional plan required')
        if not self.requirements or len(set(self.requirements)) != len(self.requirements):
            raise ValueError('Unique, explicit setup requirements required')
        if self.requires_smt is not None and type(self.requires_smt) is not bool:
            raise ValueError('SMT requirement must be true, false or unknown')


@dataclass(frozen=True)
class Review:
    state: str
    intent: str
    plan_id: Optional[str]
    direction: Optional[str]
    reasons: tuple = ()
    missing: tuple = ()
    executable: bool = False
    source_status: str = 'conditional_on_supplied_context'


class DecisionEngine:
    def __init__(self):
        self.plan = None
        self.facts = {}
        self.position = None
        self.invalidated = False
        self.last_exit_at = None
        self.last_exit_kind = None
        self.clock = -1
        self.history = []

    def _check_time(self, at):
        if at < self.clock:
            raise ValueError('Cannot apply an event before already observed information')

    def _log(self, at, kind, payload):
        self.clock = at
        self.history.append(dict(available_at=at, kind=kind, payload=payload))

    def arm(self, plan, at):
        self._check_time(at)
        if plan.known_at > at:
            raise ValueError('Plan selection uses future information')
        if self.position:
            raise ValueError('Cannot replace the plan while a position is open')
        if self.plan and plan.id == self.plan.id:
            raise ValueError('A new plan needs a new ID; cannot reset reentry history')
        self.plan = plan
        self.facts = {}
        self.invalidated = False
        # A new thesis does not erase the need for new evidence after a closure.
        self._log(at, 'plan_selected', asdict(plan))

    def observe(self, name, fact, at, plan_id):
        self._check_time(at)
        if not self.plan or plan_id != self.plan.id:
            raise ValueError('Observation belongs to a different or absent plan')
        if fact.known_at > at:
            raise ValueError('Future observation rejected')
        if name in self.facts and fact.known_at < self.facts[name].known_at:
            raise ValueError('Stale observation cannot overwrite newer information')
        self.facts[name] = fact
        # Invalidation is latched. A later price recovery cannot resurrect an
        # invalidated anchor pair; the caller must establish a new named thesis.
        if name == 'structure_intact' and fact.value is False:
            self.invalidated = True
        if name == 'smt_intact' and self.plan.requires_smt is True and fact.value is False:
            self.invalidated = True
        self._log(at, 'observation', dict(name=name, **asdict(fact)))

    def observe_position(self, direction, quantity, entry, at, source):
        self._check_time(at)
        if not self.plan or direction != self.plan.direction or quantity <= 0 or type(quantity) is not int:
            raise ValueError('Position must match a selected plan and positive whole contracts')
        if entry is not None and (not math.isfinite(entry) or entry <= 0):
            raise ValueError('Invalid average entry')
        if not source:
            raise ValueError('Position source required')
        opened_at=self.position['opened_at'] if self.position else at
        self.position = dict(direction=direction, quantity=quantity, entry=entry,
                             opened_at=opened_at, observed_at=at, source=source, realized_pnl=None)
        # A snapshot quantity change is not a fill and cannot generate realized P&L.
        self._log(at, 'position_snapshot', dict(self.position))

    def close_observed(self, at, source, kind):
        self._check_time(at)
        if not self.position or kind not in ('tactical', 'structural', 'target', 'unknown') or not source:
            raise ValueError('Existing position, source and closure classification required')
        self.position = None
        self.last_exit_at, self.last_exit_kind = at, kind
        if kind == 'structural':
            self.invalidated = True
        self._log(at, 'closure_observed', dict(source=source, exit_kind=kind, realized_pnl=None))

    def _missing_and_false(self, names):
        missing, failed = [], []
        for name in names:
            f = self.facts.get(name)
            if f is None or f.value is None:
                missing.append(name)
            elif f.value is False:
                failed.append(name)
        return missing, failed

    def evaluate(self, at):
        self._check_time(at)
        if not self.plan:
            return Review('NO_PLAN', 'NONE', None, None, missing=('selected_setup',))
        p = self.plan
        if self.invalidated:
            return Review('INVALIDATED', 'REVIEW_EXIT' if self.position else 'NONE', p.id, p.direction,
                          reasons=('selected_thesis_invalidated',))
        if self.position:
            f=self.facts.get('tactical_exit_requested')
            if f and f.value is True and f.known_at >= self.position['opened_at']:
                return Review('POSITION_OPEN', 'REVIEW_EXIT', p.id, p.direction, reasons=('tactical_exit_requested',))
            return Review('POSITION_OPEN', 'NONE', p.id, p.direction, reasons=('no_automatic_management_rule',))
        names = ('structure_intact',) + p.requirements
        if p.requires_smt is True:
            names += ('smt_intact',)
        missing, failed = self._missing_and_false(names)
        if p.requires_smt is None:
            missing.append('whether_this_setup_requires_smt')
        if self.last_exit_at is not None:
            # Both structural survival and fresh confirmation must be observed
            # after exit. A pre-exit trigger is never reused as a reentry trigger.
            for n in ('structure_intact', 'fresh_confirmation', 'reentry_justification_changed'):
                f=self.facts.get(n)
                if f is None or f.value is None or f.known_at <= self.last_exit_at:
                    missing.append('post_exit_'+n)
                elif f.value is False:
                    failed.append(n)
        if failed:
            return Review('WAIT', 'NONE', p.id, p.direction, reasons=tuple(sorted(set(failed))), missing=tuple(sorted(set(missing))))
        if missing:
            return Review('NEEDS_CONTEXT', 'NONE', p.id, p.direction, missing=tuple(sorted(set(missing))))
        return Review('CONDITIONS_MET', 'REVIEW_REENTRY' if self.last_exit_at is not None else 'REVIEW_ENTRY',
                      p.id, p.direction, reasons=('supplied_conditions_satisfied_not_an_execution',))

    def evaluate_news_add(self, at):
        self._check_time(at)
        p=self.plan
        if not p or not self.position or self.invalidated:
            return Review('ADD_UNAVAILABLE','NONE',p.id if p else None,p.direction if p else None)
        names=('headline_supports_direction','news_impulse_intact','retracement_in_selected_band','renewed_continuation')
        missing, failed = self._missing_and_false(names)
        # An add thesis must be formed during the current observed position,
        # not silently borrowed from an earlier trade or news impulse.
        for n in names:
            f=self.facts.get(n)
            if f and f.known_at < self.position['opened_at']:
                missing.append('current_position_'+n)
        if failed or missing:
            return Review('NEEDS_ADD_CONTEXT','NONE',p.id,p.direction,tuple(failed),tuple(sorted(set(missing))))
        return Review('ADD_CONDITIONS_MET','REVIEW_ADD',p.id,p.direction,
                      reasons=('Oct2_news_add_variant_only','quantity_and_risk_not_automated'))

    def review_breakeven_after_target(self, at):
        """July 16 variant, enabled only by explicit management-plan selection.

        Target reached is an observation, not a claim that a partial filled.
        Stop at average entry is price breakeven, not net breakeven after costs.
        """
        self._check_time(at)
        if not self.position or self.invalidated:
            return dict(intent='NONE', reason='no_valid_open_position', executable=False)
        names=('use_BE_after_first_target','first_target_reached')
        missing, failed=self._missing_and_false(names)
        for n in names:
            f=self.facts.get(n)
            if f and f.known_at < self.position['opened_at']:
                missing.append('current_position_'+n)
        if self.position['entry'] is None:
            missing.append('observed_average_entry')
        if missing or failed:
            return dict(intent='NONE', missing=sorted(set(missing)), failed=failed, executable=False)
        return dict(intent='REVIEW_STOP_AT_ENTRY', price=self.position['entry'],
                    quantity=self.position['quantity'], net_breakeven=False,
                    realized_pnl=None, executable=False)


def measure_selected_trigger(bars, timeframe, zone_id, direction, break_level,
                             selection_known_at, as_of):
    """Measure a manually selected IFVG. No threshold fitted to a trade outcome.

    Wick break and close break are returned separately because the source does
    not establish a universal rule choosing between them. Selection is never
    backdated to the market event when it was actually annotated later.
    """
    if selection_known_at > as_of:
        raise ValueError('Future zone / swing selection rejected')
    if direction not in ('long','short'):
        raise ValueError('Invalid direction')
    events=[e for e in fvg_events(bars,timeframe,as_of)
            if e['kind']=='IFVG' and e['zone_id']==zone_id and e['direction']==direction]
    if not events:
        return dict(status='not_yet_inverted', observation_available_at=as_of, executable=False)
    e=events[0]
    candle=next(b for b in aggregate(bars,timeframe,as_of) if b.time+timeframe*60==e['available_at'])
    if break_level is None:
        wick=close=None
    else:
        if not math.isfinite(break_level): raise ValueError('Invalid selected level')
        wick=(candle.high>break_level) if direction=='long' else (candle.low<break_level)
        close=(candle.close>break_level) if direction=='long' else (candle.close<break_level)
    span=candle.high-candle.low
    return dict(status='inverted',market_event_at=e['available_at'],
                observation_available_at=max(selection_known_at,e['available_at']),
                zone_id=zone_id,timeframe=timeframe,close=candle.close,
                wick_broke_selected_level=wick,close_broke_selected_level=close,
                body_fraction=abs(candle.close-candle.open)/span if span else 0,
                strength_threshold=None,selection_mode='externally_supplied',executable=False)
