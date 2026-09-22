"""Deterministic A Continuation V2 lifecycle.

This module is deliberately separate from the production V1 detector.  V2 has
one causal path and one entry method: catalyst continuation -> completed
displacement -> owned FVG -> later held retracement -> evolving structure BOS
-> 62% Fibonacci limit order.  There is no DIB, orphan, alternate displacement
chain, FVG-edge entry, or entry fallback in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .entries import entry_from_fvg_v10, find_entry_fibo_v10
from .exits import exceeds_risk_cap
from .primitives import fvgs


class V2State(str, Enum):
    CATALYST = "CATALYST"
    CONTINUATION_CLOSE = "CONTINUATION_CLOSE"
    DISPLACEMENT = "DISPLACEMENT"
    PROOF_OF_COMPLETION = "PROOF_OF_COMPLETION"
    FVG_COMPLETE = "FVG_COMPLETE"
    RETRACEMENT = "RETRACEMENT"
    HOLD = "HOLD"
    STRUCTURE = "STRUCTURE"
    BOS = "BOS"
    FIB_ORDER = "62%_FIB_ORDER"
    FILL = "FILL"
    EXPIRE = "EXPIRE"
    INVALID = "INVALID"


@dataclass
class ActiveLifecycleCycle:
    """One same-direction lifecycle from catalyst through its terminal state."""

    setup_id: str
    direction: str
    terminal_ms: int
    terminal_reason: str
    catalyst_ms: int | None = None
    overlapping_catalyst_events: list[dict] = field(default_factory=list)
    first_eligible_post_terminal_event: dict | None = None


class ActiveLifecycleGuard:
    """Allow at most one active A lifecycle in each direction.

    LONG and SHORT state are independent.  A same-direction catalyst observed
    at or before the current lifecycle's terminal timestamp is logged and
    discarded, never queued.  The first genuinely new catalyst event strictly
    after terminal is eligible immediately; no price reset, cooldown, or prior
    level traversal is required.
    """

    def __init__(self):
        self.current: dict[str, ActiveLifecycleCycle | None] = {"LONG": None, "SHORT": None}
        self.cycles: list[ActiveLifecycleCycle] = []
        self.discarded_events: list[dict] = []

    def consider(self, event_id: str, direction: str, event_ms: int,
                 catalysts: Iterable[str] = ()) -> tuple[bool, str]:
        if direction not in self.current:
            raise ValueError(f"unsupported direction: {direction}")
        event = {
            "event_id": str(event_id), "direction": direction,
            "event_ms": int(event_ms), "catalysts": list(catalysts),
        }
        cycle = self.current[direction]
        if cycle is None:
            return True, "direction_idle"
        if int(event_ms) > int(cycle.terminal_ms):
            if cycle.first_eligible_post_terminal_event is None:
                cycle.first_eligible_post_terminal_event = dict(event)
            return True, "prior_lifecycle_terminal"
        event["reason"] = "active_same_direction_lifecycle"
        event["blocked_by_setup_id"] = cycle.setup_id
        cycle.overlapping_catalyst_events.append(dict(event))
        self.discarded_events.append(dict(event))
        return False, "active_same_direction_lifecycle"

    def register_terminal(self, setup_id: str, direction: str, terminal_ms: int,
                          terminal_reason: str, catalyst_ms: int | None = None) -> ActiveLifecycleCycle:
        cycle = ActiveLifecycleCycle(
            setup_id=str(setup_id), direction=direction,
            terminal_ms=int(terminal_ms), terminal_reason=str(terminal_reason),
            catalyst_ms=(int(catalyst_ms) if catalyst_ms is not None else None),
        )
        self.current[direction] = cycle
        self.cycles.append(cycle)
        return cycle

    def audit_rows(self) -> list[dict]:
        return [
            {
                "setup_id": cycle.setup_id,
                "direction": cycle.direction,
                "catalyst_ms": cycle.catalyst_ms,
                "terminal_ms": cycle.terminal_ms,
                "terminal_reason": cycle.terminal_reason,
                "overlapping_catalyst_events": cycle.overlapping_catalyst_events,
                "overlapping_catalyst_event_count": len(cycle.overlapping_catalyst_events),
                "first_eligible_post_terminal_event": cycle.first_eligible_post_terminal_event,
            }
            for cycle in self.cycles
        ]


@dataclass(frozen=True)
class CatalystEvent:
    trigger_bar: int
    direction: str
    catalysts: tuple[str, ...]
    group_ids: tuple[int, ...] = ()
    breaks: tuple[int, ...] = ()


@dataclass
class Lifecycle:
    trigger_bar: int
    direction: str
    catalysts: tuple[str, ...]
    states: list[dict] = field(default_factory=list)
    invalid_reason: str | None = None

    def add(self, state: V2State, bar: int, **details) -> None:
        self.states.append({"state": state.value, "bar": int(bar), **details})

    def invalidate(self, bar: int, reason: str) -> None:
        self.invalid_reason = reason
        self.add(V2State.INVALID, bar, reason=reason)


def _directional(ctx, bar: int, bull: bool) -> bool:
    return bool(ctx.cl[bar] > ctx.o[bar]) if bull else bool(ctx.cl[bar] < ctx.o[bar])


def find_displacement_v2(ctx, trigger: int, direction: str, n_limit: int | None = None):
    """Return the first qualifying *completed* displacement after ``trigger``.

    Completion is proven by the first closed non-directional candle after the
    unbroken run.  The owned FVG's middle candle must belong to that run.  The
    proof candle may complete the FVG, but it cannot later double as the
    retracement candle.
    """
    n = min(int(ctx.n), int(n_limit)) if n_limit is not None else int(ctx.n)
    bull = direction == "LONG"
    cfg = ctx.cfg
    last_start = min(trigger + 1 + int(cfg.dispwin), n)
    s = trigger + 1
    while s < last_start:
        if not _directional(ctx, s, bull):
            s += 1
            continue
        u = s
        while u + 1 < n and u + 1 < s + int(cfg.maxext) and _directional(ctx, u + 1, bull):
            u += 1
        proof = u + 1
        if proof >= n:
            return None  # the run is not yet proven complete in this prefix
        next_s = proof + 1
        if u - s + 1 >= int(cfg.minimp):
            body = sum(
                (float(ctx.cl[x]) - float(ctx.o[x])) if bull
                else (float(ctx.o[x]) - float(ctx.cl[x]))
                for x in range(s, u + 1)
            )
            prior_slice = ctx.hi[max(0, s - int(cfg.lookback)):s] if bull else ctx.lo[max(0, s - int(cfg.lookback)):s]
            if len(prior_slice):
                prior = float(max(prior_slice)) if bull else float(min(prior_slice))
                broke = float(ctx.cl[u]) > prior if bull else float(ctx.cl[u]) < prior
            else:
                broke = False
            atr = float(ctx.ATR[u]) if float(ctx.ATR[u]) > 0 else float("inf")
            prev = range(max(0, s - 10), s)
            maxbody = max((abs(float(ctx.cl[x]) - float(ctx.o[x])) for x in prev), default=0.0)
            if body > 0 and broke and body >= float(cfg.atrmult) * atr and body >= maxbody:
                # k is the third FVG candle.  k-1 (the middle candle) must be
                # inside [s,u], hence k is restricted to [s+1,u+1].
                owned = fvgs(ctx, s + 1, min(proof + 1, n), bull)
                if owned:
                    fl, fh, fb = owned[-1]
                    return {
                        "s": int(s), "u": int(u), "proof_bar": int(proof),
                        "completion_bar": int(max(proof, fb)),
                        "L": int(u - s + 1), "body": round(float(body), 2),
                        "fvg": (float(fl), float(fh)), "fvg_bar": int(fb),
                        "swlo": float(min(ctx.lo[s:u + 1])),
                        "swhi": float(max(ctx.hi[s:u + 1])),
                        "atr5": round(atr, 2),
                    }
        # A failed run is one attempted move, not multiple overlapping chains.
        s = next_s
    return None


def confirm_setup_v2(ctx, disp: dict, direction: str, n_limit: int | None = None,
                     lifecycle: Lifecycle | None = None):
    """Advance the post-displacement V2 state machine to BOS or invalidation."""
    n = min(int(ctx.n), int(n_limit)) if n_limit is not None else int(ctx.n)
    bull = direction == "LONG"
    fl, fh = map(float, disp["fvg"])
    rf = float(ctx.cfg.rej_frac)
    threshold = round(fh - rf * (fh - fl), 2) if bull else round(fl + rf * (fh - fl), 2)
    retrace_start = int(disp["completion_bar"]) + 1
    retrace_end = min(retrace_start + int(ctx.cfg.retwin), n)
    origin = None
    origin_bar = None
    structure = None
    bos_deadline = None
    tests: list[int] = []

    for j in range(retrace_start, n):
        # Until the first held interaction, only the fixed retracement timeout applies.
        if origin is None and j >= retrace_end:
            if lifecycle is not None:
                lifecycle.invalidate(j, "retrace_timeout")
            return None
        if origin is not None and j >= int(bos_deadline):
            if lifecycle is not None:
                lifecycle.invalidate(j, "bos_timeout")
            return None

        body_broke = float(ctx.cl[j]) < threshold if bull else float(ctx.cl[j]) > threshold
        if body_broke:
            if lifecycle is not None:
                lifecycle.invalidate(j, "hold_body_break")
            return None

        # BOS is evaluated against structure completed before this candle.  The
        # first held-retracement candle therefore cannot also be the BOS candle.
        if origin is not None:
            broke = float(ctx.cl[j]) > float(structure) if bull else float(ctx.cl[j]) < float(structure)
            if broke:
                end = float(max(ctx.hi[origin_bar:j + 1])) if bull else float(min(ctx.lo[origin_bar:j + 1]))
                if lifecycle is not None:
                    lifecycle.add(V2State.BOS, j, structure_level=round(float(structure), 2),
                                  impulse_extreme=round(end, 2))
                return {
                    "dr": direction, "origin": round(float(origin), 2),
                    "origin_bar": int(origin_bar), "end": round(end, 2),
                    "bos_bar": int(j), "ce": round((fl + fh) / 2.0, 2),
                    "hold_threshold": threshold, "tests": list(tests),
                    "structure_level": round(float(structure), 2),
                    "fvg": (fl, fh), "fvg_bar": int(disp["fvg_bar"]),
                    "s": int(disp["s"]), "u": int(disp["u"]),
                    "proof_bar": int(disp["proof_bar"]),
                    "completion_bar": int(disp["completion_bar"]),
                }

        wick = float(ctx.lo[j]) <= fh if bull else float(ctx.hi[j]) >= fl
        body_holds = float(ctx.cl[j]) >= threshold if bull else float(ctx.cl[j]) <= threshold
        if wick and body_holds:
            tests.append(j)
            extreme = float(ctx.lo[j]) if bull else float(ctx.hi[j])
            first_hold = origin is None
            deeper = first_hold or (extreme < origin if bull else extreme > origin)
            if deeper:
                origin, origin_bar = extreme, j
                structure = float(max(ctx.hi[int(disp["s"]):j + 1])) if bull else float(min(ctx.lo[int(disp["s"]):j + 1]))
                bos_deadline = j + int(ctx.cfg.boswin) + 1
                if lifecycle is not None:
                    if first_hold:
                        lifecycle.add(V2State.RETRACEMENT, j, fvg=(fl, fh))
                        lifecycle.add(V2State.HOLD, j, threshold=threshold, origin=round(extreme, 2))
                        lifecycle.add(V2State.STRUCTURE, j, level=round(float(structure), 2), origin_updates=[])
                    else:
                        # This is an in-state update, not a backwards lifecycle
                        # transition.  Keep the public state sequence monotonic.
                        for state_row in reversed(lifecycle.states):
                            if state_row["state"] == V2State.STRUCTURE.value:
                                state_row.setdefault("origin_updates", []).append({
                                    "bar": int(j), "origin": round(extreme, 2),
                                    "level": round(float(structure), 2),
                                })
                                break
                continue

        if origin is not None:
            structure = max(float(structure), float(ctx.hi[j])) if bull else min(float(structure), float(ctx.lo[j]))

    # A prefix may simply end before a terminal transition.  This is not a
    # strategy invalidation and is essential for closed-bar prefix parity.
    return None


def evaluate_event_v2(ctx, event: CatalystEvent, n_limit: int | None = None):
    """Evaluate one logical catalyst move.  There is no alternate chain."""
    life = Lifecycle(event.trigger_bar, event.direction, event.catalysts)
    life.add(V2State.CATALYST, event.trigger_bar, catalysts=list(event.catalysts))
    life.add(V2State.CONTINUATION_CLOSE, event.trigger_bar)
    disp = find_displacement_v2(ctx, event.trigger_bar, event.direction, n_limit=n_limit)
    if disp is None:
        if n_limit is None and event.trigger_bar + int(ctx.cfg.dispwin) < int(ctx.n):
            life.invalidate(event.trigger_bar + int(ctx.cfg.dispwin), "displacement_timeout")
        return {"lifecycle": life, "disp": None, "setup": None, "entry": None}
    life.add(V2State.DISPLACEMENT, disp["u"], start=disp["s"], length=disp["L"])
    life.add(V2State.PROOF_OF_COMPLETION, disp["proof_bar"])
    life.add(V2State.FVG_COMPLETE, disp["completion_bar"], fvg=disp["fvg"], fvg_bar=disp["fvg_bar"])
    setup = confirm_setup_v2(ctx, disp, event.direction, n_limit=n_limit, lifecycle=life)
    if setup is None:
        if n_limit is None and life.invalid_reason is None:
            life.invalidate(int(ctx.n) - 1, "data_end")
        return {"lifecycle": life, "disp": disp, "setup": None, "entry": None}
    entry = find_entry_fibo_v10(ctx, setup, ote=0.62)
    if entry is None:
        life.invalidate(setup["bos_bar"], "invalid_fib_geometry")
        return {"lifecycle": life, "disp": disp, "setup": setup, "entry": None}
    entry["kind"] = "FIBO"
    entry["start_bar"] = max(int(entry["hh_bar"]), int(setup["bos_bar"])) + 1
    if exceeds_risk_cap(ctx, entry.get("risk_ce", entry["risk"])):
        life.invalidate(setup["bos_bar"], "ce_risk_cap")
        return {"lifecycle": life, "disp": disp, "setup": setup, "entry": None}
    fib_range = abs(float(setup["end"]) - float(setup["origin"]))
    life.add(V2State.FIB_ORDER, entry["start_bar"], origin=setup["origin"],
             impulse_extreme=setup["end"], fib_range=round(fib_range, 2),
             fib_price=entry["entry"])
    return {"lifecycle": life, "disp": disp, "setup": setup, "entry": entry}


def find_bos_fvg_entry_v2(ctx, setup: dict):
    """Return the registered FVG-edge entry for an FVG owned by the BOS impulse.

    The three-candle FVG must be fully visible by the BOS close.  Its middle
    candle must be at or after the structural-origin candle, so an earlier
    displacement FVG cannot be reused.  The freshest qualifying BOS-impulse
    FVG is selected exactly once and passed to the canonical V1 price/risk
    implementation.
    """
    bull = setup["dr"] == "LONG"
    ob, bb = int(setup["origin_bar"]), int(setup["bos_bar"])
    # k is the third FVG candle; k-1 must lie inside [ob,bb].  Because the
    # entry type freezes at the BOS decision, k itself may not be after bb.
    candidates = fvgs(ctx, ob + 1, bb + 1, bull)
    if not candidates:
        return None
    selected = candidates[-1]
    entry = entry_from_fvg_v10(ctx, setup, selected)
    if entry is None:
        return None
    entry.update({
        "kind": "FVG", "entry_type": "BOS_FVG",
        "start_bar": max(int(selected[2]), bb) + 1,
        "bos_fvg": (float(selected[0]), float(selected[1])),
        "bos_fvg_bar": int(selected[2]),
    })
    return entry


def select_phase2_entry_v2(ctx, setup: dict):
    """Freeze one Phase-2 entry: BOS FVG first, otherwise unchanged Fib 62%."""
    fvg_entry = find_bos_fvg_entry_v2(ctx, setup)
    if fvg_entry is not None:
        return fvg_entry
    fib = find_entry_fibo_v10(ctx, setup, ote=0.62)
    if fib is None:
        return None
    fib.update({
        "kind": "FIBO", "entry_type": "FIB_62",
        "start_bar": max(int(fib["hh_bar"]), int(setup["bos_bar"])) + 1,
        "bos_fvg": None, "bos_fvg_bar": None,
    })
    return fib


def merge_continuation_events(groups: Iterable[dict], ctx, strict_gap_zones: dict | None = None):
    """Merge catalyst sources whose continuation close occurs on the same bar.

    Cached point-level continuation events already require a close through the
    level.  Cached gap events are admitted only when the same trigger candle
    closes beyond the complete registered zone boundary.
    """
    merged: dict[tuple[int, str], dict] = {}
    zones = strict_gap_zones or {}
    for group in groups:
        for raw in group.get("events", ()):
            t, z, model, name, brk = raw
            if model != "Cont":
                continue
            direction = "LONG" if int(z) == 1 else "SHORT"
            if group.get("kind") == "run_gap":
                candidates = zones.get((name, int(group["form"])), ())
                candidates = [pair for pair in candidates
                              if float(ctx.lo[t]) <= pair[1] and float(ctx.hi[t]) >= pair[0]]
                through = [pair for pair in candidates
                           if (float(ctx.cl[t]) > pair[1] if direction == "LONG" else float(ctx.cl[t]) < pair[0])]
                if not through:
                    continue
            key = (int(t), direction)
            slot = merged.setdefault(key, {"cats": [], "groups": [], "breaks": []})
            if name not in slot["cats"]:
                slot["cats"].append(name)
            slot["groups"].append(int(group["id"]))
            slot["breaks"].append(int(brk))
    return [CatalystEvent(t, direction, tuple(v["cats"]), tuple(v["groups"]), tuple(v["breaks"]))
            for (t, direction), v in sorted(merged.items())]


def gap_zone_index(ctx):
    """Index the already-cached NDOG/NWOG/VI catalyst boundaries by form time."""
    out: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for price, form, name, *_ in ctx.gaplev:
        out.setdefault((str(name), int(form)), []).append((float(price), float(price)))
    for lo, hi, bar, _bull, _mag in ctx.vis:
        out.setdefault(("VI", int(ctx.T[int(bar)])), []).append((float(min(lo, hi)), float(max(lo, hi))))
    return out


EXPECTED_PREFIX = (
    V2State.CATALYST.value, V2State.CONTINUATION_CLOSE.value,
    V2State.DISPLACEMENT.value, V2State.PROOF_OF_COMPLETION.value,
    V2State.FVG_COMPLETE.value, V2State.RETRACEMENT.value,
    V2State.HOLD.value, V2State.STRUCTURE.value, V2State.BOS.value,
    V2State.FIB_ORDER.value,
)
