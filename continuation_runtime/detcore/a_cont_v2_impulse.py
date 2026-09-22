"""A Continuation V2 research variant with causal impulse-at-catalyst ownership.

Only displacement segmentation differs from :mod:`detcore.a_cont_v2`.  The
catalyst set, hold, evolving structure, BOS, Fib entry, stop, target, and
lifecycle guard remain unchanged.
"""
from __future__ import annotations

from .a_cont_v2 import (
    CatalystEvent,
    Lifecycle,
    V2State,
    confirm_setup_v2,
)
from .entries import find_entry_fibo_v10
from .exits import exceeds_risk_cap
from .primitives import fvgs


def _directional(ctx, bar: int, bull: bool) -> bool:
    return bool(ctx.cl[bar] > ctx.o[bar]) if bull else bool(ctx.cl[bar] < ctx.o[bar])


def _completion_cross(ctx, bar: int, last_directional: int, bull: bool) -> bool:
    """Return True when a counter candle closes through the last impulse body.

    Counter-colour candles whose closes remain within the latest directional
    body are internal pauses.  A counter-colour close through that body's
    origin causally proves that the directional repricing has completed.
    """
    if _directional(ctx, bar, bull):
        return False
    return (
        float(ctx.cl[bar]) < float(ctx.o[last_directional])
        if bull else float(ctx.cl[bar]) > float(ctx.o[last_directional])
    )


def find_impulse_at_catalyst_v2(ctx, trigger: int, direction: str,
                                n_limit: int | None = None):
    """Find the first completed coherent impulse that contains ``trigger``.

    The immediately preceding ``lookback`` closed bars may be recognized as
    part of the impulse after the catalyst close.  Minor counter-colour bars
    remain inside the impulse while their closes hold the latest directional
    candle's body origin.  Existing V2 strength, structure-break, and owned-FVG
    requirements are then applied to the complete mixed-colour envelope.

    Candidate selection is deterministic: earliest causal proof first, then
    the most recent qualifying start.  No bar after ``n_limit`` is inspected.
    """
    n = min(int(ctx.n), int(n_limit)) if n_limit is not None else int(ctx.n)
    if trigger >= n:
        return None
    bull = direction == "LONG"
    cfg = ctx.cfg
    earliest = max(0, int(trigger) - int(cfg.lookback))
    proof_limit = min(n, int(trigger) + int(cfg.dispwin) + 1)
    candidates = []

    # Most-recent starts are preferred only after earliest proof is fixed.
    for s in range(int(trigger), earliest - 1, -1):
        last_directional = None
        proof = None
        u = None
        for bar in range(s, proof_limit):
            if _directional(ctx, bar, bull):
                last_directional = bar
                continue
            if (last_directional is not None and bar > int(trigger)
                    and _completion_cross(ctx, bar, last_directional, bull)):
                proof = bar
                u = last_directional
                break
        if proof is None or u is None or u < int(trigger):
            continue
        length = int(u - s + 1)
        if length < int(cfg.minimp) or length > int(cfg.maxext):
            continue

        signed_bodies = [
            (float(ctx.cl[x]) - float(ctx.o[x])) if bull
            else (float(ctx.o[x]) - float(ctx.cl[x]))
            for x in range(s, u + 1)
        ]
        body = float(sum(signed_bodies))
        if body <= 0:
            continue

        prior_slice = (
            ctx.hi[max(0, s - int(cfg.lookback)):s]
            if bull else ctx.lo[max(0, s - int(cfg.lookback)):s]
        )
        if not len(prior_slice):
            continue
        prior = float(max(prior_slice)) if bull else float(min(prior_slice))
        broke = float(ctx.cl[u]) > prior if bull else float(ctx.cl[u]) < prior
        if not broke:
            continue

        atr = float(ctx.ATR[u]) if float(ctx.ATR[u]) > 0 else float("inf")
        if body < float(cfg.atrmult) * atr:
            continue
        maxbody = max(
            (abs(float(ctx.cl[x]) - float(ctx.o[x]))
             for x in range(max(0, s - 10), s)),
            default=0.0,
        )
        if body < maxbody:
            continue

        # k is the third FVG candle. Its middle candle k-1 must be owned by
        # this impulse; the third candle may be the causal proof candle.
        owned = [
            gap for gap in fvgs(ctx, s + 1, min(proof + 1, n), bull)
            if s <= int(gap[2]) - 1 <= u
        ]
        if not owned:
            continue
        fl, fh, fb = owned[-1]
        candidates.append({
            "s": int(s), "u": int(u), "proof_bar": int(proof),
            "completion_bar": int(max(proof, fb)), "L": length,
            "body": round(body, 2), "fvg": (float(fl), float(fh)),
            "fvg_bar": int(fb),
            "swlo": float(min(ctx.lo[s:u + 1])),
            "swhi": float(max(ctx.hi[s:u + 1])),
            "atr5": round(atr, 2),
            "opposite_bar_count": int(sum(value < 0 for value in signed_bodies)),
            "total_range": round(float(max(ctx.hi[s:u + 1]) - min(ctx.lo[s:u + 1])), 2),
            "contains_catalyst": bool(s <= trigger <= u),
            "pre_catalyst_bars": int(trigger - s),
            "post_catalyst_bars": int(u - trigger),
            "segmentation": "IMPULSE_AT_CATALYST",
        })

    if not candidates:
        return None
    return min(candidates, key=lambda row: (row["proof_bar"], -row["s"]))


def evaluate_event_impulse_v2(ctx, event: CatalystEvent,
                              n_limit: int | None = None):
    """Evaluate V2 with only the impulse-at-catalyst segmentation amended."""
    life = Lifecycle(event.trigger_bar, event.direction, event.catalysts)
    life.add(V2State.CATALYST, event.trigger_bar, catalysts=list(event.catalysts))
    life.add(V2State.CONTINUATION_CLOSE, event.trigger_bar)
    disp = find_impulse_at_catalyst_v2(
        ctx, event.trigger_bar, event.direction, n_limit=n_limit)
    if disp is None:
        if n_limit is None and event.trigger_bar + int(ctx.cfg.dispwin) < int(ctx.n):
            life.invalidate(event.trigger_bar + int(ctx.cfg.dispwin), "displacement_timeout")
        return {"lifecycle": life, "disp": None, "setup": None, "entry": None}

    life.add(
        V2State.DISPLACEMENT, disp["u"], start=disp["s"],
        length=disp["L"], opposite_bar_count=disp["opposite_bar_count"],
        pre_catalyst_bars=disp["pre_catalyst_bars"],
        post_catalyst_bars=disp["post_catalyst_bars"],
    )
    life.add(V2State.PROOF_OF_COMPLETION, disp["proof_bar"])
    life.add(
        V2State.FVG_COMPLETE, disp["completion_bar"],
        fvg=disp["fvg"], fvg_bar=disp["fvg_bar"])
    setup = confirm_setup_v2(
        ctx, disp, event.direction, n_limit=n_limit, lifecycle=life)
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
    life.add(
        V2State.FIB_ORDER, entry["start_bar"], origin=setup["origin"],
        impulse_extreme=setup["end"], fib_range=round(fib_range, 2),
        fib_price=entry["entry"])
    return {"lifecycle": life, "disp": disp, "setup": setup, "entry": entry}
