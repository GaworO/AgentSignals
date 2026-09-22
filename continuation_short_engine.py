"""Exploratory SHORT mirror of the frozen LONG Continuation detector.

This module does not modify or impersonate the immutable LONG freeze.  It uses
the same hashed canonical detector, Jade daily thesis and causal DOL catalog,
with directionally mirrored liquidity, entry and order geometry.  It contains
no profitability calculation or broker integration.
"""
from __future__ import annotations

import collections
from dataclasses import asdict
from unittest.mock import patch

import numpy as np
import pandas as pd

from MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE.source import freeze_baseline as base
from detcore.config import Config
from detcore.data import load
from detcore.catalysts import build_levels
from detcore.emit import emit_orphan, try_chain
from detcore.scaffolding import _armed_hit_positions, _cap_a1
from detcore.a_cont_v3_ict_dol import tag_setup_dol
from detcore.a_cont_v3_ict_ledger import LedgerLevel


IDENTITY = "MNQ_CONTINUATION_HTF_CANONICAL_SHORT_RESEARCH_V1"
SIDE = "SHORT"


def level_specs(ctx) -> list[dict]:
    """Exact lower-side counterparts of the baseline's registered highs."""
    specs = []
    session = {"ASIA": "AL", "LO": "LL", "NYAM": "NYAML", "NYL": "NYLL", "NYPM": "NYPML"}
    for name, _start, end, _high, low in ctx.sessinst:
        if name in session:
            specs.append(dict(name=session[name], price=float(low), form_t=int(ctx.T[end]), intraday=True))
    for di in range(1, len(ctx.days)):
        day = ctx.days[di]
        if day in ctx.day_first_idx:
            specs.append(dict(name="PDL", price=float(ctx.day_hl[ctx.days[di - 1]][1]),
                              form_t=int(ctx.T[ctx.day_first_idx[day]]) - 1, intraday=False))
    iso = ctx.df.dt.dt.isocalendar()
    weeks: collections.OrderedDict[tuple, list] = collections.OrderedDict()
    for i, key in enumerate(zip(iso.year.values, iso.week.values)):
        if key not in weeks:
            weeks[key] = [i, i, float(ctx.lo[i])]
        else:
            weeks[key][1] = i
            weeks[key][2] = min(weeks[key][2], float(ctx.lo[i]))
    keys = list(weeks)
    for wi in range(1, len(keys)):
        specs.append(dict(name="PWL", price=float(weeks[keys[wi - 1]][2]),
                          form_t=int(ctx.T[weeks[keys[wi]][0]]) - 1, intraday=False))
    for price, form_t in ctx.eqL:
        specs.append(dict(name="SSL H1", price=float(price), form_t=int(form_t), intraday=False))
    unique = {(x["name"], round(x["price"], 8), x["form_t"]): x for x in specs}
    return sorted(unique.values(), key=lambda x: (x["form_t"], x["name"], x["price"]))


def run_level(ctx, spec: dict, epoch: int, triggers: list[dict]) -> None:
    a0, a1 = _cap_a1(ctx, spec["form_t"])
    if a1 <= a0:
        return
    indices = np.arange(a0, a1)
    window = indices[ctx.T[a0:a1] > spec["form_t"]]
    if not len(window):
        return
    hit = np.flatnonzero(ctx.cl[window] < spec["price"])
    rearm = np.flatnonzero(ctx.cl[window] > spec["price"] + ctx.cfg.buf)
    events = _armed_hit_positions(hit, rearm)
    if ctx.cfg.eod_intraday and spec["intraday"] and events:
        first = int(window[events[0]])
        di0 = ctx.dayi[ctx.dates[first]]
        extra = 1 if ctx.H[first] >= 18 else 0
        expiry = ctx.day_last_idx[ctx.days[min(di0 + extra, len(ctx.days) - 1)]]
        events = [q for q in events if int(window[q]) <= expiry]
    for number, q in enumerate(events, 1):
        i = int(window[q])
        ctx.cur_break = number
        trigger = {
            "epoch": epoch, "trigger_bar": i, "trigger_ms": int(ctx.T[i]) * 1000,
            "ssl_name": spec["name"], "ssl_price": spec["price"],
            "level_formed_ms": int(spec["form_t"]) * 1000,
            "close": float(ctx.cl[i]), "close_through": bool(ctx.cl[i] < spec["price"]),
            "direction": SIDE,
        }
        triggers.append(trigger)
        before, orphan_before = len(ctx.out), len(ctx.orphans)
        try_chain(ctx, i, SIDE, "Cont", spec["name"])
        for row in ctx.out[before:]:
            row["_source"] = trigger
        for orphan in ctx.orphans[orphan_before:]:
            orphan["_source"] = trigger
        if len(ctx.out) > before:
            return


def generate_detector(raw: pd.DataFrame) -> tuple[list[dict], list[dict], dict]:
    outputs, triggers, epoch_meta = [], [], []
    with base.detector_environment():
        cfg = Config.from_env(base.DETECTOR_ENV)
        for epoch, global_a, global_b, frame in base.epoch_frames(raw):
            with patch("detcore.data.pd.read_csv", return_value=frame.copy()):
                ctx = load(cfg)
            build_levels(ctx)
            specs = level_specs(ctx)
            for spec in specs:
                run_level(ctx, spec, epoch, triggers)
            if ctx.orphans:
                seen = set()
                for orphan in ctx.orphans:
                    key = (orphan["dr"], round(orphan["disp"]["fvg"][0], 2),
                           round(orphan["disp"]["fvg"][1], 2), int(orphan["disp"]["fvg_bar"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    before = len(ctx.out)
                    emit_orphan(ctx, orphan)
                    for row in ctx.out[before:]:
                        row["_source"] = orphan.get("_source", {})
            seen, deduped = set(), []
            for row in sorted(ctx.out, key=lambda z: z["emit_bar"]):
                key = (row["model"], row["cat"], row["dir"], row["emit_bar"] // 30)
                if key not in seen:
                    seen.add(key)
                    deduped.append(row)
            for row in deduped:
                source = row.pop("_source", {})
                for key in ("s", "u", "fvg_bar", "origin_bar", "bos_bar", "emit_bar",
                            "entry_bar", "sfvg_bar", "hh_bar"):
                    if row.get(key) is not None:
                        row[key] = int(row[key]) + global_a
                row.update(epoch=epoch, instrument_id=int(frame.instrument_id.iloc[0]),
                           source_event=source, research_identity=IDENTITY)
                outputs.append(row)
            epoch_meta.append({"epoch": epoch, "instrument_id": int(frame.instrument_id.iloc[0]),
                               "registered_ssl_objects": len(specs),
                               "raw_close_through_events": sum(x["epoch"] == epoch for x in triggers),
                               "canonical_outputs": len(deduped)})
    outputs.sort(key=lambda x: (x["bos_ms"], x["cat"], x["entry"]))
    return outputs, triggers, {"epochs": epoch_meta, "effective_config": asdict(cfg)}


def ssl_ledger(engine, epochs: list[dict]) -> dict[int, list[LedgerLevel]]:
    """Physical-contract lower-side catalog, with causally confirmed H1 swings."""
    result: dict[int, list[LedgerLevel]] = {}
    for epoch in epochs:
        a, b = epoch["start"], epoch["end"]
        levels = []
        for level in engine.levels:
            if level.side != -1 or level.kind not in {"day", "week", "session", "H1_equal"}:
                continue
            if not (a <= int(level.source_start) < int(level.born) < b):
                continue
            levels.append(LedgerLevel(str(level.id), str(level.kind), -1, float(level.price),
                                      int(level.source_start), int(level.born),
                                      min(int(level.expires), b), min(int(level.touch), b)))
        frame = engine.f.iloc[a:b].copy()
        frame["global_i"] = np.arange(a, b)
        h1 = frame.set_index("ts").resample("1h").agg(
            start=("global_i", "min"), end=("global_i", "max"),
            low=("low", "min"), count=("low", "size"),
        ).dropna()
        complete = ((engine.ms[h1.end.to_numpy(int)] % 3_600_000 == 3_540_000)
                    & (engine.ms[h1.start.to_numpy(int)] % 3_600_000 == 0))
        h1 = h1[complete]
        lows = h1.low.to_numpy(float)
        for k in range(2, len(h1) - 2):
            if lows[k] > min(lows[k - 2:k + 3]):
                continue
            born = int(h1.iloc[k + 2].end) + 1
            if born >= b:
                continue
            price = float(lows[k])
            hits = np.flatnonzero(engine.l[born:b] <= price)
            touch = born + int(hits[0]) if len(hits) else b
            source = int(h1.iloc[k].start)
            levels.append(LedgerLevel(f"H1_swing:{int(engine.ms[source])}:-1", "H1_swing", -1,
                                      price, source, born, b, touch))
        result[epoch["epoch"]] = sorted({x.id: x for x in levels}.values(), key=lambda x: (x.born, x.id))
    return result


def build_manifests(raw: pd.DataFrame, outputs: list[dict], theses: dict) -> tuple[list[dict], list[dict], dict]:
    engine, epochs, _ = base.dol_resources(raw)
    ledger = ssl_ledger(engine, epochs)
    epoch_by_id = {x["epoch"]: x for x in epochs}
    ms = raw.ts_event.astype("int64").to_numpy() // 1_000_000
    iid = raw.instrument_id.to_numpy(np.int64)
    high = raw.high.to_numpy(float)
    candidates, orders = [], []
    reasons = collections.Counter()
    for n, output in enumerate(outputs, 1):
        day = base.trading_day_at(output["bos_ms"])
        thesis = theses.get(day, {"thesis": "NONE", "reason": "missing_day"})
        row = dict(output)
        row.update(candidate_id=base.stable_id("CONT_SHORT", output["instrument_id"], output["bos_ms"],
                                                output["cat"], output["fvg_bar"], output["entry"]),
                   chronological_index=n, trading_day=day, jade_thesis=thesis.get("thesis", "NONE"),
                   jade_thesis_reason=thesis.get("reason"), eligible=False, rejection_reason=None)
        row["jade_thesis_fixed_at"] = (day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)).tz_localize(
            "America/New_York").tz_convert("UTC")
        if row["jade_thesis"] != SIDE:
            row["rejection_reason"] = "HTF_THESIS_NOT_SHORT"
        else:
            decision_ms = int(row["entry_ms"])
            tag = tag_setup_dol(engine, direction=SIDE, evaluated_at_ms=decision_ms,
                                extra_levels=ledger[row["epoch"]], include_native_levels=False).to_dict()
            dol = tag.get("current_dol")
            if tag.get("dol_status") != "OPEN" or dol is None:
                row["rejection_reason"] = "NO_CAUSAL_OPEN_BEARISH_DOL"
            else:
                entry = base.tick(float(row["entry"]) - float(base.DETECTOR_ENV["ENTRY_OFFSET_PTS"]))
                stop = base.tick(float(row["SL"]))
                risk = round(stop - entry, 8)
                target = base.tick(float(dol["pool_price"]))
                if risk <= 0:
                    row["rejection_reason"] = "INVALID_FINAL_ENTRY_SL_GEOMETRY"
                elif target >= entry:
                    row["rejection_reason"] = "OPEN_DOL_NOT_BELOW_FINAL_ENTRY"
                else:
                    ep = epoch_by_id[row["epoch"]]
                    activation, expiry = decision_ms, decision_ms + 10 * 60_000
                    left = max(int(np.searchsorted(ms, activation, side="left")), int(ep["start"]))
                    right = min(int(np.searchsorted(ms, expiry, side="left")), int(ep["end"]))
                    hits = np.flatnonzero((iid[left:right] == row["instrument_id"])
                                         & (high[left:right] >= entry + base.TICK))
                    fill_i = None if not len(hits) else left + int(hits[0])
                    row.update(eligible=True, dol_status="OPEN", dol_id=dol["pool_id"],
                               dol_price=target, dol_priority_class=dol["priority_class"],
                               dol_selected_at=pd.Timestamp(decision_ms, unit="ms", tz="UTC"),
                               final_entry=entry, final_structural_sl=stop,
                               final_initial_risk_points=risk,
                               policy_B_target=target,
                               entry_activation_timestamp=pd.Timestamp(activation, unit="ms", tz="UTC"),
                               order_expiry=pd.Timestamp(expiry, unit="ms", tz="UTC"),
                               estimated_fill=fill_i is not None,
                               estimated_fill_timestamp=None if fill_i is None else raw.ts_event.iloc[fill_i])
                    if fill_i is None:
                        reasons["UNFILLED_WITHIN_10_MINUTES"] += 1
                    orders.append({
                        "order_id": base.stable_id("ORDER_SHORT", row["candidate_id"], activation, entry, stop),
                        "candidate_id": row["candidate_id"], "direction": SIDE,
                        "chronological_index": n, "trading_day": day,
                        "instrument_id": row["instrument_id"], "epoch": row["epoch"],
                        "activation_timestamp": row["entry_activation_timestamp"],
                        "expiry_timestamp": row["order_expiry"], "entry_price": entry,
                        "structural_sl_price": stop, "initial_risk_points": risk,
                        "policy_B_target": target, "dol_id": row["dol_id"],
                        "estimated_fill": row["estimated_fill"],
                        "estimated_fill_timestamp": row["estimated_fill_timestamp"],
                        "fill_rule": "first same-instrument minute high >= entry + 0.25; activation inclusive; expiry exclusive",
                    })
        if row["rejection_reason"]:
            reasons[row["rejection_reason"]] += 1
        candidates.append(row)
    funnel = {
        "canonical_outputs": len(outputs),
        "jade_short_eligible": sum(x["jade_thesis"] == SIDE for x in candidates),
        "causal_open_dol_eligible": sum(x["eligible"] for x in candidates),
        "resting_orders": len(orders), "estimated_fills": sum(x["estimated_fill"] for x in orders),
        "principal_rejection_reasons": dict(reasons),
    }
    return candidates, orders, funnel
