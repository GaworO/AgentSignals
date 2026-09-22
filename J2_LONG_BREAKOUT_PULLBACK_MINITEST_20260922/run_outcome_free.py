#!/usr/bin/env python3
"""Outcome-free J2 LONG breakout-pullback feasibility mini-test.

This runner generates a new population from frozen causal primitives.  It does
not read old A_CONT candidate membership and never computes post-fill outcomes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = Path("/Users/aleksandra/Desktop/NQ Signals/MNQ_databento_2022_1m.csv")
POLICY_ROOT = ROOT / "audit_results/ict_125"
YEAR = POLICY_ROOT / "year_20260910"
JADE_CANDIDATES = ROOT / "MNQ_JADECAP_HTF_PULLBACK_V1_OUTCOME_FREE_FREEZE/candidate_manifest.jsonl"
START_DAY = pd.Timestamp("2022-06-01")
END_DAY = pd.Timestamp("2022-12-01")
CLOCK = "Etc/GMT+4"
TICK = 0.25

sys.path[:0] = [str(POLICY_ROOT), str(YEAR), str(ROOT)]
from policy import Engine  # noqa: E402
from detcore.a_cont_v2 import Lifecycle, confirm_setup_v2, find_displacement_v2  # noqa: E402
from detcore.a_cont_v3_gold.structure import entry_62  # noqa: E402
from detcore.a_cont_v3_ict_dol import tag_setup_dol  # noqa: E402
from detcore.a_cont_v3_ict_ledger import LedgerLevel, _session_at  # noqa: E402


@dataclass(frozen=True)
class RegisteredBSL:
    id: str
    kind: str
    family: str
    price: float
    source_start: int
    born: int
    expires: int
    touch: int
    epoch: int


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def iso_ms(ms: int) -> str:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").isoformat()


def pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 6) if d else None


def round_tick(value: float) -> float:
    return round(round(float(value) / TICK) * TICK, 2)


def read_early_data() -> pd.DataFrame:
    use = ["ts_event", "instrument_id", "open", "high", "low", "close", "volume", "symbol"]
    parts = []
    for chunk in pd.read_csv(DATA, usecols=use, parse_dates=["ts_event"], chunksize=100_000):
        chunk["ts_event"] = pd.to_datetime(chunk.ts_event, utc=True)
        parts.append(chunk[chunk.ts_event < pd.Timestamp("2022-12-02T06:00:00Z")])
        if chunk.ts_event.max() >= pd.Timestamp("2022-12-02T06:00:00Z"):
            break
    raw = pd.concat(parts, ignore_index=True).sort_values("ts_event", kind="stable")
    raw = raw.drop_duplicates("ts_event", keep="last").reset_index(drop=True)
    local = raw.ts_event.dt.tz_convert(CLOCK)
    raw["local_day"] = local.dt.tz_localize(None).dt.normalize()
    raw["trading_day"] = raw.local_day + pd.to_timedelta((local.dt.hour >= 18).astype(int), unit="D")
    # Keep one observed day beyond the study boundary only so aggregates ending
    # before END_DAY are causally publishable. No later Development data enters.
    raw = raw[raw.trading_day < END_DAY + pd.Timedelta(days=1)].copy().reset_index(drop=True)
    raw["ts"] = raw.ts_event
    return raw


def build_engine(raw: pd.DataFrame) -> Engine:
    return Engine(raw[["ts_event", "ts", "open", "high", "low", "close"]].copy())


def epoch_ranges(raw: pd.DataFrame, engine: Engine) -> list[dict[str, int]]:
    changes = raw.instrument_id.ne(raw.instrument_id.shift())
    rows = raw.loc[changes, ["ts_event", "instrument_id"]]
    starts = np.searchsorted(engine.ms, rows.ts_event.astype("int64").to_numpy() // 1_000_000, side="left")
    ends = np.r_[starts[1:], engine.n]
    return [
        {"epoch": i, "start": int(a), "end": int(b), "instrument_id": int(iid)}
        for i, (a, b, iid) in enumerate(zip(starts, ends, rows.instrument_id.to_numpy()))
    ]


def atr_array(engine: Engine, start: int, end: int) -> np.ndarray:
    frame = engine.f.iloc[start:end].copy()
    bins = engine.ms[start:end] // 300_000
    five = frame.assign(bin=bins).groupby("bin").agg(h=("high", "max"), l=("low", "min"))
    five["atr"] = (five.h - five.l).rolling(20).mean().shift(1)
    values = pd.DataFrame({"bin": bins}).merge(five[["atr"]], left_on="bin", right_index=True, how="left").atr
    return values.fillna(0.0).to_numpy(float)


def h1_swings(engine: Engine, epoch: dict[str, int]) -> list[RegisteredBSL]:
    a, b = epoch["start"], epoch["end"]
    f = engine.f.iloc[a:b].copy()
    f["global_i"] = np.arange(a, b)
    h1 = f.set_index("ts").resample("1h").agg(
        start=("global_i", "min"), end=("global_i", "max"),
        high=("high", "max"), count=("high", "size"),
    ).dropna()
    complete = (engine.ms[h1.end.to_numpy(int)] % 3_600_000 == 3_540_000) & (engine.ms[h1.start.to_numpy(int)] % 3_600_000 == 0)
    h1 = h1[complete]
    out: list[RegisteredBSL] = []
    values = h1.high.to_numpy(float)
    for k in range(2, len(h1) - 2):
        if values[k] < max(values[k - 2:k + 3]):
            continue
        born = int(h1.iloc[k + 2].end) + 1
        if born >= b:
            continue
        price = float(values[k])
        hits = np.flatnonzero(engine.h[born:b] >= price)
        touch = born + int(hits[0]) if len(hits) else b
        source = int(h1.iloc[k].start)
        out.append(RegisteredBSL(
            id=f"H1_swing:{int(engine.ms[source])}:1", kind="H1_swing", family="H1_SWING",
            price=price, source_start=source, born=born, expires=b, touch=touch, epoch=epoch["epoch"],
        ))
    return out


def family_for(engine: Engine, level) -> str:
    if level.kind in {"day", "week"}:
        return "EXTERNAL_DAY_WEEK"
    if level.kind == "H1_equal":
        return "H1_EQUAL"
    if level.kind == "session":
        return f"SESSION_{_session_at(engine, int(level.source_start))}"
    return str(level.kind)


def registered_catalog(engine: Engine, epochs: list[dict[str, int]]) -> list[RegisteredBSL]:
    out: list[RegisteredBSL] = []
    for epoch in epochs:
        a, b = epoch["start"], epoch["end"]
        for level in engine.levels:
            if level.side != 1 or level.kind not in {"day", "week", "session", "H1_equal"}:
                continue
            if not (a <= int(level.source_start) < int(level.born) < b):
                continue
            out.append(RegisteredBSL(
                id=str(level.id), kind=str(level.kind), family=family_for(engine, level),
                price=float(level.price), source_start=int(level.source_start), born=int(level.born),
                expires=min(int(level.expires), b), touch=min(int(level.touch), b), epoch=epoch["epoch"],
            ))
        out.extend(h1_swings(engine, epoch))
    unique = {(x.epoch, x.id): x for x in out}
    return sorted(unique.values(), key=lambda x: (x.epoch, x.born, x.id))


def ledger_catalog(catalog: list[RegisteredBSL]) -> list[LedgerLevel]:
    return [LedgerLevel(x.id, x.kind, 1, x.price, x.source_start, x.born, x.expires, x.touch) for x in catalog]


def breakout_events(engine: Engine, catalog: list[RegisteredBSL], epochs: list[dict[str, int]],
                    study_start: int, study_end: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[RegisteredBSL]] = {}
    epoch_by_id = {x["epoch"]: x for x in epochs}
    for level in catalog:
        epoch = epoch_by_id[level.epoch]
        # The level must exist before, not merely at, the breaking candle.
        lo = max(level.born + 1, epoch["start"] + 1, study_start)
        hi = min(level.expires, epoch["end"], study_end)
        if lo >= hi:
            continue
        hit = np.flatnonzero((engine.c[lo - 1:hi - 1] <= level.price) & (engine.c[lo:hi] > level.price))
        if len(hit):
            bar = lo + int(hit[0])
            grouped.setdefault((level.epoch, bar), []).append(level)
    events = []
    for (epoch, bar), levels in sorted(grouped.items()):
        levels = sorted(levels, key=lambda x: (x.price, x.id))
        events.append({
            "event_id": f"J2|E{epoch}|LONG|{bar}|" + "|".join(x.id for x in levels),
            "epoch": epoch, "bar": bar, "level_ids": [x.id for x in levels],
            "families": sorted({x.family for x in levels}), "prices": [x.price for x in levels],
        })
    return events


def jade_metadata(jade_rows: list[dict[str, Any]], trading_day: str, decision_ms: int) -> dict[str, Any]:
    rows = [x for x in jade_rows if str(x.get("trading_day", ""))[:10] == trading_day]
    states = sorted({str(x.get("htf_thesis_state", "NONE")) for x in rows if x.get("htf_thesis_state")})
    thesis = states[0] if len(states) == 1 else "NONE"
    prior = any(
        x.get("direction") == "LONG" and x.get("reclaim_known_ts")
        and int(pd.Timestamp(x["reclaim_known_ts"]).timestamp() * 1000) <= decision_ms
        for x in rows
    )
    return {
        "bullish_htf_thesis": "YES" if thesis == "LONG" else "NO" if thesis == "SHORT" else "NONE",
        "prior_countertrend_sellside_raid_reclaim": "YES" if prior else "NO",
        "alignment_with_j2_long": "ALIGNED" if thesis == "LONG" else "OPPOSED" if thesis == "SHORT" else "NONE",
        "membership_effect": false_value(),
    }


def false_value() -> bool:
    return False


def stage_trace(ctx, disp: dict[str, Any], life: Lifecycle) -> dict[str, bool]:
    states = {row["state"] for row in life.states}
    stop_bar = ctx.n
    invalid = [int(row["bar"]) for row in life.states if row["state"] == "INVALID"]
    if invalid:
        stop_bar = min(invalid) + 1
    fl, fh = map(float, disp["fvg"])
    start = int(disp["completion_bar"]) + 1
    end = min(stop_bar, start + int(ctx.cfg.retwin), ctx.n)
    physical = any(float(ctx.lo[j]) <= fh and float(ctx.hi[j]) >= fl for j in range(start, end))
    return {
        "later_pullback": physical,
        "hold": "RETRACEMENT" in states and "HOLD" in states,
        "structure": "STRUCTURE" in states,
        "bos": "BOS" in states,
    }


def geometry(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"N": 0, "median": None, "P75": None, "P90": None,
                "pct_ge_2R": None, "pct_ge_3R": None, "pct_ge_5R": None}
    a = np.asarray(values, dtype=float)
    return {
        "N": len(a), "median": round(float(np.median(a)), 6),
        "P75": round(float(np.percentile(a, 75)), 6),
        "P90": round(float(np.percentile(a, 90)), 6),
        "pct_ge_2R": pct(int(np.sum(a >= 2)), len(a)),
        "pct_ge_3R": pct(int(np.sum(a >= 3)), len(a)),
        "pct_ge_5R": pct(int(np.sum(a >= 5)), len(a)),
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def analyze() -> dict[str, Any]:
    raw = read_early_data()
    engine = build_engine(raw)
    epochs = epoch_ranges(raw, engine)
    start_ms = int((START_DAY.tz_localize(CLOCK) - pd.Timedelta(hours=6)).tz_convert("UTC").timestamp() * 1000)
    end_ms = int((END_DAY.tz_localize(CLOCK) - pd.Timedelta(hours=6)).tz_convert("UTC").timestamp() * 1000)
    study_start = int(np.searchsorted(engine.ms, start_ms, side="left"))
    study_end = int(np.searchsorted(engine.ms, end_ms, side="left"))
    catalog = registered_catalog(engine, epochs)
    study_catalog = [x for x in catalog if x.born < study_end and x.expires > study_start]
    ledger = ledger_catalog(catalog)
    ledgers_by_epoch = {e["epoch"]: [x for x in ledger if e["start"] <= x.born < e["end"]] for e in epochs}
    events = breakout_events(engine, study_catalog, epochs, study_start, study_end)
    epoch_by_id = {x["epoch"]: x for x in epochs}
    jade_rows = load_jsonl(JADE_CANDIDATES)
    cfg = SimpleNamespace(dispwin=30, maxext=40, minimp=3, lookback=15, atrmult=1.0, tol=3.0,
                          retwin=20, boswin=30, rej_frac=0.6)
    rows: list[dict[str, Any]] = []
    prefix_failures = 0

    for event in events:
        epoch = epoch_by_id[event["epoch"]]
        a = epoch["start"]
        b = min(epoch["end"], study_end)
        if event["bar"] >= b:
            continue
        local_n = b - a
        ctx = SimpleNamespace(
            o=engine.o[a:b], hi=engine.h[a:b], lo=engine.l[a:b], cl=engine.c[a:b],
            ATR=atr_array(engine, a, b), n=local_n, cfg=cfg,
        )
        trigger = event["bar"] - a
        row: dict[str, Any] = {**event, "trigger_local": trigger, "stage": "BREAKOUT_CONFIRMED"}
        disp = find_displacement_v2(ctx, trigger, "LONG", n_limit=local_n)
        row["valid_displacement"] = disp is not None
        row["owned_fvg"] = disp is not None
        row.update(later_pullback=False, hold=False, structure=False, bos=False,
                   valid_entry_sl=False, open_dol=False, estimated_fill=False)
        if disp is None:
            rows.append(row)
            continue
        row["stage"] = "OWNED_FVG"
        row["displacement"] = disp
        life = Lifecycle(trigger, "LONG", tuple(event["level_ids"]))
        setup = confirm_setup_v2(ctx, disp, "LONG", n_limit=local_n, lifecycle=life)
        row.update(stage_trace(ctx, disp, life))
        if row["later_pullback"]:
            row["stage"] = "LATER_PULLBACK"
        if row["hold"]:
            row["stage"] = "HOLD"
        if row["structure"]:
            row["stage"] = "STRUCTURE"
        if setup is None:
            row["terminal_reason"] = life.invalid_reason or "NO_BOS_BEFORE_WINDOW_OR_EPOCH_END"
            rows.append(row)
            continue
        row["stage"] = "BOS"
        row["setup"] = setup
        bos_global = a + int(setup["bos_bar"])
        decision_ms = int(engine.ms[bos_global]) + 60_000
        entry = round_tick(entry_62(
            direction=__import__("detcore.a_cont_v3_gold.types", fromlist=["Direction"]).Direction.LONG,
            origin=float(setup["origin"]), bos_extreme=float(setup["end"]),
        ))
        sl = round_tick(float(setup["origin"]) - TICK)
        risk = round(entry - sl, 8)
        if risk <= 0:
            row["terminal_reason"] = "INVALID_STRUCTURAL_RISK"
            rows.append(row)
            continue
        row.update(stage="ENTRY_SL", valid_entry_sl=True, bos_global=bos_global,
                   decision_ms=decision_ms, entry=entry, sl=sl, risk=risk)
        tag = tag_setup_dol(
            engine, direction="LONG", evaluated_at_ms=decision_ms,
            extra_levels=ledgers_by_epoch[event["epoch"]], include_native_levels=False,
        ).to_dict()
        dol = tag.get("current_dol")
        valid_dol = bool(tag.get("dol_status") == "OPEN" and dol and float(dol["pool_price"]) > entry)
        row["open_dol"] = valid_dol
        row["dol_status"] = tag.get("dol_status")
        if not valid_dol:
            row["terminal_reason"] = "NO_CAUSAL_OPEN_DOL_ABOVE_ENTRY"
            rows.append(row)
            continue
        potential_r = (float(dol["pool_price"]) - entry) / risk
        row.update(stage="OPEN_DOL", dol={
            "id": dol["pool_id"], "price": float(dol["pool_price"]), "status": dol["status"],
            "type": dol["priority_class"], "constituents": dol["constituent_levels"],
            "selection_timestamp": iso_ms(decision_ms),
        }, potential_r=float(potential_r))
        activation = int(np.searchsorted(engine.ms, decision_ms, side="left"))
        expiry_ms = decision_ms + 600_000
        fill_end = min(epoch["end"], study_end, int(np.searchsorted(engine.ms, expiry_ms, side="left")))
        fill_hits = np.flatnonzero(engine.l[activation:fill_end] <= entry - TICK) if activation < fill_end else np.asarray([])
        if len(fill_hits):
            fill_bar = activation + int(fill_hits[0])
            row.update(stage="ESTIMATED_FILL", estimated_fill=True, fill_bar=fill_bar,
                       fill_timestamp=iso_ms(int(engine.ms[fill_bar])))
        trading_day = str(raw.loc[raw.ts_event.eq(pd.Timestamp(decision_ms, unit="ms", tz="UTC")), "trading_day"].iloc[0].date()) if (raw.ts_event == pd.Timestamp(decision_ms, unit="ms", tz="UTC")).any() else str(pd.Timestamp(decision_ms, unit="ms", tz="UTC").tz_convert(CLOCK).date())
        row["trading_day"] = trading_day
        row["jadecap_metadata"] = jade_metadata(jade_rows, trading_day, decision_ms)

        # Full candidate must be identical when the context is truncated at BOS.
        prefix_n = int(setup["bos_bar"]) + 1
        disp_p = find_displacement_v2(ctx, trigger, "LONG", n_limit=prefix_n)
        life_p = Lifecycle(trigger, "LONG", tuple(event["level_ids"]))
        setup_p = confirm_setup_v2(ctx, disp_p, "LONG", n_limit=prefix_n, lifecycle=life_p) if disp_p else None
        if disp_p != disp or setup_p != setup:
            prefix_failures += 1
        rows.append(row)

    # Physical dedup is applied before final order/fill reporting.
    order_ready = [x for x in rows if x["open_dol"]]
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in order_ready:
        setup = row["setup"]
        disp = row["displacement"]
        key = (row["epoch"], "LONG", int(disp["fvg_bar"]), tuple(disp["fvg"]),
               int(setup["origin_bar"]), round(float(setup["origin"]), 8),
               round(float(setup["structure_level"]), 8), int(setup["bos_bar"]))
        unique.setdefault(key, row)
    final_orders = sorted(unique.values(), key=lambda x: (x["decision_ms"], x["event_id"]))
    fills = [x for x in final_orders if x["estimated_fill"]]

    # Causality and fidelity assertions.
    chronology = lookahead = fvg_ownership = retrospective_dol = roll_fail = 0
    for row in final_orders:
        epoch = epoch_by_id[row["epoch"]]
        disp, setup = row["displacement"], row["setup"]
        if not (row["bar"] < epoch["start"] + disp["s"] <= epoch["start"] + disp["u"] < epoch["start"] + disp["completion_bar"] < epoch["start"] + setup["origin_bar"] < row["bos_global"]):
            chronology += 1
        if not (disp["s"] <= int(disp["fvg_bar"]) - 1 <= disp["u"]):
            fvg_ownership += 1
        if not (epoch["start"] <= row["bar"] < row["bos_global"] < epoch["end"]):
            roll_fail += 1
        open_members = [member for member in row["dol"]["constituents"] if member["status"] == "OPEN"]
        if not open_members:
            retrospective_dol += 1
        for member in open_members:
            level = next((x for x in ledgers_by_epoch[row["epoch"]] if x.id == member["id"]), None)
            if level is None or level.born > row["bos_global"] or level.touch_hint <= row["bos_global"]:
                retrospective_dol += 1
        for level_id in row["level_ids"]:
            level = next(x for x in catalog if x.epoch == row["epoch"] and x.id == level_id)
            if level.born >= row["bar"]:
                lookahead += 1

    fill_times = [pd.Timestamp(x["fill_timestamp"]) for x in fills]
    months = sorted({x.strftime("%Y-%m") for x in fill_times})
    weeks = Counter(x.strftime("%G-W%V") for x in fill_times)
    largest_week = max(weeks.values(), default=0)
    source_breakouts = Counter(f for x in events for f in x["families"])
    source_fills = Counter(f for x in fills for f in x["families"])
    geom = geometry([float(x["potential_r"]) for x in fills])
    stage_counts = {
        "registered_bsl_available": len(study_catalog),
        "bullish_close_through": len(rows),
        "valid_displacement": sum(bool(x["valid_displacement"]) for x in rows),
        "event_owned_fvg": sum(bool(x["owned_fvg"]) for x in rows),
        "later_pullback": sum(bool(x["later_pullback"]) for x in rows),
        "hold_0_6": sum(bool(x["hold"]) for x in rows),
        "causal_structure": sum(bool(x["structure"]) for x in rows),
        "bullish_bos": sum(bool(x["bos"]) for x in rows),
        "valid_entry_sl": sum(bool(x["valid_entry_sl"]) for x in rows),
        "open_bullish_dol_raw": len(order_ready),
        "independent_open_dol_orders": len(final_orders),
        "independent_estimated_fills": len(fills),
    }
    order = list(stage_counts)
    conversions = {}
    for i, key in enumerate(order):
        denom = stage_counts[order[i - 1]] if i else stage_counts[key]
        conversions[key] = {"N": stage_counts[key], "conversion_pct_from_prior": 100.0 if i == 0 else pct(stage_counts[key], denom)}

    causal = {
        "chronology_failures": chronology,
        "lookahead_or_future_pivot_failures": lookahead,
        "retrospective_fvg_ownership_failures": fvg_ownership,
        "retrospective_dol_selection_failures": retrospective_dol,
        "roll_contamination_failures": roll_fail,
        "raw_duplicate_physical_orders_removed": len(order_ready) - len(final_orders),
        "final_duplicate_physical_events": len(final_orders) - len(unique),
        "prefix_invariance_failures": prefix_failures,
    }
    failure_total = sum(v for k, v in causal.items() if k.endswith("failures") or k == "final_duplicate_physical_events")
    per_month = round(len(fills) / 6.0, 6)
    if failure_total:
        decision = "J2 LONG STILL REQUIRES UNFROZEN STRATEGY SEMANTICS — STOP"
    elif per_month < 1.0:
        decision = "J2 LONG IS CAUSAL BUT TOO RARE — STOP"
    elif len(months) >= 4 and geom["median"] is not None and geom["median"] >= 2.0:
        decision = "J2 LONG BREAKOUT-PULLBACK DOL CONTINUATION PASSES MINI FEASIBILITY — AUTHORIZE FULL OUTCOME-FREE FREEZE"
    else:
        decision = "J2 LONG IS CAUSAL BUT TOO RARE — STOP"

    result = {
        "study": "J2 LONG BREAKOUT-PULLBACK DOL CONTINUATION",
        "scope": {"start_trading_day_inclusive": str(START_DAY.date()), "end_trading_day_exclusive": str(END_DAY.date()),
                  "months": 6.0, "long_only": True, "validation_or_sealed_loaded": False,
                  "outcomes_read_or_computed": False},
        "funnel": conversions,
        "frequency": {"independent_estimated_fills_per_month": per_month,
                      "months_represented": months, "month_count": len(months),
                      "weeks_represented": sorted(weeks), "week_count": len(weeks),
                      "largest_week_fills": largest_week,
                      "largest_week_share_pct": pct(largest_week, len(fills))},
        "bsl_source_distribution": {"breakouts": dict(source_breakouts), "fills": dict(source_fills)},
        "potential_r_geometry_estimated_fills": geom,
        "causality": causal,
        "decision": decision,
        "input_hashes": {
            "mnq_csv": sha(DATA), "spec": sha(HERE / "SPEC.json"),
            "canonical_v2": sha(ROOT / "detcore/a_cont_v2.py"),
            "gold_structure": sha(ROOT / "detcore/a_cont_v3_gold/structure.py"),
            "ranked_dol": sha(ROOT / "detcore/a_cont_v3_ict_dol.py"),
            "dol_ledger": sha(ROOT / "detcore/a_cont_v3_ict_ledger.py"),
            "jadecap_candidate_metadata": sha(JADE_CANDIDATES),
        },
        "catalog": {"registered_bsl": [
            {key: value for key, value in asdict(x).items() if key != "touch"}
            for x in study_catalog
        ]},
        "events": rows,
        "final_orders": final_orders,
        "prohibited_outcome_fields": [],
    }
    return result


def main() -> None:
    first = analyze()
    first_hash = stable_hash(first)
    second = analyze()
    second_hash = stable_hash(second)
    first["deterministic_rerun"] = {"first_hash": first_hash, "second_hash": second_hash,
                                    "identical": first_hash == second_hash}
    if first_hash != second_hash:
        first["causality"]["deterministic_rerun_failures"] = 1
        first["decision"] = "J2 LONG STILL REQUIRES UNFROZEN STRATEGY SEMANTICS — STOP"
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "RESULT.json").write_text(json.dumps(first, indent=2, sort_keys=True, allow_nan=False) + "\n")
    compact = {k: first[k] for k in ("study", "scope", "funnel", "frequency", "bsl_source_distribution",
                                      "potential_r_geometry_estimated_fills", "causality", "deterministic_rerun", "decision")}
    print(json.dumps(compact, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
