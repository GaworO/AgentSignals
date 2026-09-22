#!/usr/bin/env python3
"""Freeze the outcome-free MNQ Continuation canonical research baseline.

The module intentionally contains no exit replay or P&L calculation.  It uses
the registered generic detector configuration, isolates the Continuation state
from Reversal, then applies the separately frozen Jade thesis and OPEN-DOL
eligibility gates before constructing resting orders.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd


FREEZE = Path(__file__).resolve().parents[1]
REPO = FREEZE.parent
DATA = REPO / "jadecap_research_20260921/data_dev/mnq_development.csv"
REGISTRATION = REPO / "audit_results/ict_125/year_20260910/registration.json"
SOURCE_GATE = REPO / "MNQ_CONTINUATION_HTF_CANONICAL_REVERSAL_SOURCE_GATE/SOURCE_GATE.json"
JADE_SOURCE = REPO / "MNQ_JADECAP_HTF_PULLBACK_V1_OUTCOME_FREE_FREEZE/source/mini_v1_authoritative.py"
START = pd.Timestamp("2022-06-01T00:00:00Z")
END = pd.Timestamp("2024-06-01T00:00:00Z")
TICK = 0.25

sys.path[:0] = [str(REPO), str(REPO / "audit_results/ict_125"), str(REPO / "audit_results/ict_125/year_20260910")]
from detcore.config import Config  # noqa: E402
from detcore.data import load  # noqa: E402
from detcore.catalysts import build_levels  # noqa: E402
from detcore.emit import emit_orphan, try_chain  # noqa: E402
from detcore.scaffolding import _armed_hit_positions, _cap_a1  # noqa: E402
from detcore.a_cont_v3_ict_dol import tag_setup_dol  # noqa: E402
from policy import Engine  # noqa: E402
from J2_LONG_BREAKOUT_PULLBACK_MINITEST_20260922.run_outcome_free import (  # noqa: E402
    epoch_ranges, ledger_catalog, registered_catalog,
)
from MNQ_JADECAP_HTF_PULLBACK_V1_OUTCOME_FREE_FREEZE.source.mini_v1_authoritative import (  # noqa: E402
    construct_theses, daily_bars,
)


DETECTOR_ENV = {
    "DISPWIN": "30", "ATRMULT": "1.0", "STOP_CAP": "30", "STOP_CAP_TRIGGER": "0",
    "ENTRY_PRIMARY": "fibo", "ENTRY_OFFSET_PTS": "1", "MAX_STOP_R": "40",
    "DISP_MODE": "chain", "MAX_RETEST": "4", "REJ_FRAC": "0.6", "DET_CAUSAL": "1",
    "ORPHAN_FVG": "1", "ORPHAN_MAX_BARS": "120", "ORPHAN_WINDOW": "caps",
    "ORPHAN_LIFE": "day", "SWING_TP": "1", "SWING_TP_K": "5",
    "SWING_TP_LOOKBACK": "240", "SWING_TP_MIN_R": "1", "SWING_TP_MAX_R": "3",
    "SL_STRUCT_MAX_R": "30", "SL_ANCHOR_BUF": ".25",
    # Explicit research values: no production cutoff and no production file/output paths.
    "CUTOFF": "", "MODE": "confirm", "CAP_DAYS": "10", "EOD_INTRADAY": "",
}


def safe(v: Any) -> Any:
    if v is None or v is pd.NaT: return None
    if isinstance(v, pd.Timestamp): return v.isoformat()
    if isinstance(v, (np.integer,)): return int(v)
    if isinstance(v, (np.floating,)): return None if np.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)): return bool(v)
    if isinstance(v, float) and np.isnan(v): return None
    if isinstance(v, dict): return {str(k): safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [safe(x) for x in v]
    return v


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(safe(x), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for x in rows))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return prefix + "_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:20]


def tick(v: float) -> float:
    return round(round(float(v) / TICK) * TICK, 2)


@contextmanager
def detector_environment():
    with patch.dict(os.environ, DETECTOR_ENV, clear=False):
        yield


def load_dev() -> pd.DataFrame:
    use = ["ts_event", "instrument_id", "open", "high", "low", "close", "volume"]
    df = pd.read_csv(DATA, usecols=use, parse_dates=["ts_event"])
    df["ts_event"] = pd.to_datetime(df.ts_event, utc=True)
    df = df[(df.ts_event >= START) & (df.ts_event < END)].sort_values("ts_event", kind="stable")
    return df.drop_duplicates("ts_event", keep="last").reset_index(drop=True)


def jade_theses(raw: pd.DataFrame) -> dict[pd.Timestamp, dict]:
    x = raw.copy()
    local = x.ts_event.dt.tz_convert("America/New_York")
    x["local"] = local
    x["local_date"] = local.dt.tz_localize(None).dt.normalize()
    x["minute"] = local.dt.hour * 60 + local.dt.minute
    x["trading_day"] = x.local_date + pd.to_timedelta((local.dt.hour >= 18).astype(int), unit="D")
    x = x[local.dt.hour != 17].set_index("ts_event")
    return construct_theses(daily_bars(x))


def trading_day_at(ms: int) -> pd.Timestamp:
    t = pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert("America/New_York")
    return pd.Timestamp(t.date()) + (pd.Timedelta(days=1) if t.hour >= 18 else pd.Timedelta(0))


def epoch_frames(raw: pd.DataFrame) -> list[tuple[int, int, int, pd.DataFrame]]:
    changes = np.flatnonzero(raw.instrument_id.to_numpy()[1:] != raw.instrument_id.to_numpy()[:-1]) + 1
    starts, ends = np.r_[0, changes], np.r_[changes, len(raw)]
    return [(k, int(a), int(b), raw.iloc[a:b].copy().reset_index(drop=True))
            for k, (a, b) in enumerate(zip(starts, ends))]


def level_specs(ctx) -> list[dict]:
    specs = []
    sh = {"ASIA": "AH", "LO": "LH", "NYAM": "NYAMH", "NYL": "NYLH", "NYPM": "NYPMH"}
    for sname, _s0, eidx, hh, _ll in ctx.sessinst:
        if sname in sh:
            specs.append(dict(name=sh[sname], price=float(hh), form_t=int(ctx.T[eidx]), intraday=True))
    for di in range(1, len(ctx.days)):
        d = ctx.days[di]
        if d in ctx.day_first_idx:
            specs.append(dict(name="PDH", price=float(ctx.day_hl[ctx.days[di - 1]][0]),
                              form_t=int(ctx.T[ctx.day_first_idx[d]]) - 1, intraday=False))
    iso = ctx.df.dt.dt.isocalendar()
    weeks: collections.OrderedDict[tuple, list] = collections.OrderedDict()
    for i, key in enumerate(zip(iso.year.values, iso.week.values)):
        if key not in weeks: weeks[key] = [i, i, float(ctx.hi[i])]
        else: weeks[key][1] = i; weeks[key][2] = max(weeks[key][2], float(ctx.hi[i]))
    keys = list(weeks)
    for wi in range(1, len(keys)):
        specs.append(dict(name="PWH", price=float(weeks[keys[wi - 1]][2]),
                          form_t=int(ctx.T[weeks[keys[wi]][0]]) - 1, intraday=False))
    for price, form_t in ctx.eqH:
        specs.append(dict(name="BSL H1", price=float(price), form_t=int(form_t), intraday=False))
    # Physical duplicates from the same source definition are harmless but are
    # removed before state evaluation to make the manifest deterministic.
    unique = {(x["name"], round(x["price"], 8), x["form_t"]): x for x in specs}
    return sorted(unique.values(), key=lambda x: (x["form_t"], x["name"], x["price"]))


def run_level_cont_only(ctx, spec: dict, epoch: int, triggers: list[dict]) -> None:
    a0, a1 = _cap_a1(ctx, spec["form_t"])
    if a1 <= a0: return
    base = np.arange(a0, a1)
    win = base[ctx.T[a0:a1] > spec["form_t"]]
    if not len(win): return
    hit = np.flatnonzero(ctx.cl[win] > spec["price"])
    rearm = np.flatnonzero(ctx.cl[win] < spec["price"] - ctx.cfg.buf)
    events = _armed_hit_positions(hit, rearm)
    if ctx.cfg.eod_intraday and spec["intraday"] and events:
        first = int(win[events[0]])
        di0 = ctx.dayi[ctx.dates[first]]; extra = 1 if ctx.H[first] >= 18 else 0
        exp = ctx.day_last_idx[ctx.days[min(di0 + extra, len(ctx.days) - 1)]]
        events = [q for q in events if int(win[q]) <= exp]
    for number, q in enumerate(events, 1):
        i = int(win[q]); ctx.cur_break = number
        trigger = {
            "epoch": epoch, "trigger_bar": i, "trigger_ms": int(ctx.T[i]) * 1000,
            "bsl_name": spec["name"], "bsl_price": spec["price"],
            "level_formed_ms": int(spec["form_t"]) * 1000,
            "close": float(ctx.cl[i]), "close_through": bool(ctx.cl[i] > spec["price"]),
        }
        triggers.append(trigger)
        before, obefore = len(ctx.out), len(ctx.orphans)
        try_chain(ctx, i, "LONG", "Cont", spec["name"])
        for row in ctx.out[before:]: row["_source"] = trigger
        for orphan in ctx.orphans[obefore:]: orphan["_source"] = trigger
        if len(ctx.out) > before: return


def generate_detector(raw: pd.DataFrame) -> tuple[list[dict], list[dict], dict]:
    outputs, triggers = [], []
    epoch_meta = []
    with detector_environment():
        cfg = Config.from_env(DETECTOR_ENV)
        for epoch, global_a, global_b, frame in epoch_frames(raw):
            # load() is kept untouched; the patch only supplies the already
            # selected physical contract epoch instead of making a temporary CSV.
            with patch("detcore.data.pd.read_csv", return_value=frame.copy()):
                ctx = load(cfg)
            build_levels(ctx)
            specs = level_specs(ctx)
            for spec in specs: run_level_cont_only(ctx, spec, epoch, triggers)
            if ctx.orphans:
                seen = set()
                for o in ctx.orphans:
                    key = (o["dr"], round(o["disp"]["fvg"][0], 2), round(o["disp"]["fvg"][1], 2), int(o["disp"]["fvg_bar"]))
                    if key in seen: continue
                    seen.add(key); before = len(ctx.out); emit_orphan(ctx, o)
                    for row in ctx.out[before:]: row["_source"] = o.get("_source", {})
            seen, ded = set(), []
            for row in sorted(ctx.out, key=lambda z: z["emit_bar"]):
                key = (row["model"], row["cat"], row["dir"], row["emit_bar"] // 30)
                if key in seen: continue
                seen.add(key); ded.append(row)
            for row in ded:
                src = row.pop("_source", {})
                for key in ("s", "u", "fvg_bar", "origin_bar", "bos_bar", "emit_bar", "entry_bar", "sfvg_bar", "hh_bar"):
                    if row.get(key) is not None: row[key] = int(row[key]) + global_a
                row["entry_ms"] = int(row["entry_ms"])
                row["bos_ms"] = int(row["bos_ms"])
                row["epoch"] = epoch
                row["instrument_id"] = int(frame.instrument_id.iloc[0])
                row["source_event"] = src
                outputs.append(row)
            epoch_meta.append({"epoch": epoch, "instrument_id": int(frame.instrument_id.iloc[0]),
                               "global_start": global_a, "global_end_exclusive": global_b,
                               "first_timestamp": frame.ts_event.iloc[0], "last_timestamp": frame.ts_event.iloc[-1],
                               "registered_bsl_objects": len(specs), "raw_close_through_events": sum(x["epoch"] == epoch for x in triggers),
                               "canonical_outputs_before_cross_epoch_sort": len(ded)})
    outputs.sort(key=lambda x: (x["bos_ms"], x["cat"], x["entry"]))
    return outputs, triggers, {"epochs": epoch_meta, "effective_config": asdict(cfg)}


def dol_resources(raw: pd.DataFrame):
    x = raw.rename(columns={"ts_event": "ts"}).copy()
    x["ts_event"] = x["ts"]
    engine = Engine(x[["ts_event", "ts", "open", "high", "low", "close"]].copy())
    eps = epoch_ranges(raw.assign(ts=raw.ts_event), engine)
    catalog = registered_catalog(engine, eps)
    ledger = ledger_catalog(catalog)
    by_epoch = {e["epoch"]: [z for z in ledger if e["start"] <= z.born < e["end"]] for e in eps}
    return engine, eps, by_epoch


def build_manifests(raw: pd.DataFrame, outputs: list[dict], triggers: list[dict], theses: dict) -> tuple[list[dict], list[dict], dict]:
    engine, eps, ledgers = dol_resources(raw)
    epoch_by_id = {x["epoch"]: x for x in eps}
    ms = raw.ts_event.astype("int64").to_numpy() // 1_000_000
    iid = raw.instrument_id.to_numpy(np.int64)
    low = raw.low.to_numpy(float)
    candidates, orders = [], []
    reasons = collections.Counter()
    for n, out in enumerate(outputs, 1):
        day = trading_day_at(out["bos_ms"])
        thesis = theses.get(day, {"thesis": "NONE", "reason": "missing_day"})
        row = dict(out)
        row["candidate_id"] = stable_id("CONT", out["instrument_id"], out["bos_ms"], out["cat"], out["fvg_bar"], out["entry"])
        row["chronological_index"] = n
        row["trading_day"] = day
        row["jade_thesis"] = thesis.get("thesis", "NONE")
        row["jade_thesis_reason"] = thesis.get("reason")
        row["jade_thesis_fixed_at"] = (day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)).tz_localize("America/New_York").tz_convert("UTC")
        row["eligible"] = False
        row["rejection_reason"] = None
        if row["jade_thesis"] != "LONG":
            row["rejection_reason"] = "HTF_THESIS_NOT_LONG"; reasons[row["rejection_reason"]] += 1; candidates.append(row); continue
        ep = epoch_by_id[row["epoch"]]
        decision_ms = int(row["entry_ms"])
        tag = tag_setup_dol(engine, direction="LONG", evaluated_at_ms=decision_ms,
                            extra_levels=ledgers[row["epoch"]], include_native_levels=False).to_dict()
        dol = tag.get("current_dol")
        if not (tag.get("dol_status") == "OPEN" and dol is not None):
            row["rejection_reason"] = "NO_CAUSAL_OPEN_BULLISH_DOL"; reasons[row["rejection_reason"]] += 1; candidates.append(row); continue
        entry = tick(float(row["entry"]) + float(DETECTOR_ENV["ENTRY_OFFSET_PTS"]))
        stop = tick(float(row["SL"])); risk = round(entry - stop, 8)
        if risk <= 0:
            row["rejection_reason"] = "INVALID_FINAL_ENTRY_SL_GEOMETRY"; reasons[row["rejection_reason"]] += 1; candidates.append(row); continue
        dol_price = tick(float(dol["pool_price"]))
        if dol_price <= entry:
            row["rejection_reason"] = "OPEN_DOL_NOT_ABOVE_FINAL_ENTRY"; reasons[row["rejection_reason"]] += 1; candidates.append(row); continue
        activation = decision_ms
        expiry = activation + 10 * 60_000
        a = max(int(np.searchsorted(ms, activation, side="left")), int(ep["start"]))
        b = min(int(np.searchsorted(ms, expiry, side="left")), int(ep["end"]))
        hits = np.flatnonzero((iid[a:b] == row["instrument_id"]) & (low[a:b] <= entry - TICK))
        fill_i = None if not len(hits) else a + int(hits[0])
        row.update(eligible=True, dol_status="OPEN", dol_id=dol["pool_id"], dol_price=dol_price,
                   dol_priority_class=dol["priority_class"], dol_selected_at=pd.Timestamp(decision_ms, unit="ms", tz="UTC"),
                   final_entry=entry, final_structural_sl=stop, final_initial_risk_points=risk,
                   policy_A_target=tick(entry + 2 * risk), policy_B_target=dol_price,
                   entry_activation_timestamp=pd.Timestamp(activation, unit="ms", tz="UTC"),
                   order_expiry=pd.Timestamp(expiry, unit="ms", tz="UTC"), estimated_fill=fill_i is not None,
                   estimated_fill_timestamp=None if fill_i is None else raw.ts_event.iloc[fill_i])
        if fill_i is None: reasons["UNFILLED_WITHIN_10_MINUTES"] += 1
        candidates.append(row)
        order_id = stable_id("ORDER", row["candidate_id"], activation, entry, stop)
        orders.append({
            "order_id": order_id, "candidate_id": row["candidate_id"], "chronological_index": n,
            "trading_day": day, "instrument_id": row["instrument_id"], "epoch": row["epoch"],
            "activation_timestamp": row["entry_activation_timestamp"], "expiry_timestamp": row["order_expiry"],
            "entry_price": entry, "structural_sl_price": stop, "initial_risk_points": risk,
            "policy_A_target": row["policy_A_target"], "policy_B_target": dol_price,
            "dol_id": row["dol_id"], "estimated_fill": row["estimated_fill"],
            "estimated_fill_timestamp": row["estimated_fill_timestamp"],
            "fill_rule": "first same-instrument minute low <= entry - 0.25; activation inclusive; expiry exclusive",
        })
    funnel = {
        "registered_bsl_objects": sum(x["registered_bsl_objects"] for x in detector_meta["epochs"]),
        "independent_close_through_events": len(triggers),
        "no_canonical_confirmation": len(triggers) - len(outputs),
        "canonical_outputs": len(outputs),
        "jade_long_eligible": sum(x["jade_thesis"] == "LONG" for x in candidates),
        "causal_open_dol_eligible": sum(x.get("eligible", False) for x in candidates),
        "resting_orders": len(orders), "estimated_fills": sum(x["estimated_fill"] for x in orders),
        "principal_rejection_reasons": dict(reasons),
    }
    return candidates, orders, funnel


def effective_freeze(detector_meta: dict) -> dict:
    reg = json.loads(REGISTRATION.read_text())
    gate = json.loads(SOURCE_GATE.read_text())
    return {
        "identity": "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1",
        "classification": "new standalone research baseline; no historical Reversal profitability inherited",
        "champion_source_gate": {"gate": gate["gate"], "reason": gate["reason"], "unchanged": True},
        "selected_generic_configuration": {"path": str(REGISTRATION), "sha256": sha(REGISTRATION),
                                             "detector_env_exact": reg["detector_env"]},
        "effective_detector_environment": DETECTOR_ENV,
        "effective_config_object": detector_meta["effective_config"],
        "authorized_research_overrides": {
            "identity": "new identity and isolated Continuation-only state",
            "eligibility": "Jade LONG thesis mandatory, not metadata-only",
            "dol": "separate causal OPEN bullish DOL mandatory before entry",
            "entry_offset_points": 1.0,
            "policies": {"A": "fixed +2R", "B": "same frozen OPEN DOL"},
            "round_trip_cost_usd": 3.50,
            "cost_difference_from_registration": "replaces historical $2.24 research assumption; no slippage added again",
            "development_interval": [START, END],
            "order_expiry_minutes": 10,
            "limit_fill": "one tick through",
            "intrabar_priority": "adverse/SL first",
        },
        "independent_state": "Only registered high-side liquidity is evaluated; no Reversal call shares ctx.out, orphan store, expiry, re-arm or dedup state.",
        "registered_bsl_sources": ["AH", "LH", "NYAMH", "NYLH", "NYPMH", "PDH", "PWH", "BSL H1"],
        "validation_opened": False, "sealed_opened": False, "production_reversal_modified": False,
    }


def manifest_hashes() -> None:
    members = {}
    for p in sorted(FREEZE.rglob("*")):
        if p.is_file() and p.name not in {"SHA256_MANIFEST.json", "FREEZE.sha256"}:
            members[str(p.relative_to(FREEZE))] = sha(p)
    write_json(FREEZE / "SHA256_MANIFEST.json", members)
    (FREEZE / "FREEZE.sha256").write_text(f"{sha(FREEZE / 'SHA256_MANIFEST.json')}  SHA256_MANIFEST.json\n")


def main() -> None:
    global detector_meta
    raw = load_dev()
    theses = jade_theses(raw)
    outputs1, triggers1, detector_meta = generate_detector(raw)
    outputs2, triggers2, detector_meta2 = generate_detector(raw)
    canon = lambda x: hashlib.sha256(json.dumps(safe(x), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    determinism = {"status": "PASS" if canon(outputs1) == canon(outputs2) and canon(triggers1) == canon(triggers2) else "FAIL",
                   "outputs_hash_run_1": canon(outputs1), "outputs_hash_run_2": canon(outputs2),
                   "triggers_hash_run_1": canon(triggers1), "triggers_hash_run_2": canon(triggers2)}
    if determinism["status"] != "PASS": raise RuntimeError("detector determinism failed")
    candidates, orders, funnel = build_manifests(raw, outputs1, triggers1, theses)
    first20 = []
    for row in candidates[:20]:
        first20.append({k: row.get(k) for k in (
            "chronological_index", "candidate_id", "bos_iso", "cat", "source_event", "s", "u", "fvg_bar",
            "fvg_lo", "fvg_hi", "origin_bar", "bos_bar", "entry_ms", "sl_src", "entry", "SL",
            "jade_thesis", "dol_status", "dol_id", "dol_price", "eligible", "rejection_reason",
            "final_entry", "final_structural_sl", "estimated_fill", "estimated_fill_timestamp")})
    write_json(FREEZE / "BASELINE_CONFIGURATION.json", effective_freeze(detector_meta))
    write_json(FREEZE / "DETECTOR_EPOCHS.json", detector_meta)
    write_json(FREEZE / "FUNNEL.json", funnel)
    write_json(FREEZE / "DETERMINISM.json", determinism)
    write_jsonl(FREEZE / "trigger_manifest.jsonl", triggers1)
    write_jsonl(FREEZE / "candidate_manifest.jsonl", candidates)
    write_jsonl(FREEZE / "order_manifest.jsonl", orders)
    write_jsonl(FREEZE / "first20_audit.jsonl", first20)
    write_json(FREEZE / "POLICIES_COST_TERMINAL.json", {
        "policy_A": "entry + 2 * initial risk", "policy_B": "frozen pre-entry OPEN DOL price",
        "cost_usd_round_trip": 3.50, "mnq_point_value_usd": 2.0, "tick_points": 0.25,
        "slippage": "not added separately", "expiry_minutes": 10, "one_tick_through": True,
        "fill_bar_target_evaluation": False, "intrabar_priority": "SL before target",
        "gap_stop": "adverse open when open below stop", "contract_roll": "terminate at last bar of physical epoch",
        "split_boundary": "terminate at last Development bar", "development_end_exclusive": END,
    })
    write_json(FREEZE / "SOURCE_HASHES.json", {
        str(p.relative_to(REPO)): sha(p) for p in [SOURCE_GATE, REGISTRATION, JADE_SOURCE,
            REPO / "detcore/config.py", REPO / "detcore/data.py", REPO / "detcore/catalysts.py",
            REPO / "detcore/scaffolding.py", REPO / "detcore/confirmation.py", REPO / "detcore/entries.py",
            REPO / "detcore/emit.py", REPO / "detcore/a_cont_v3_ict_dol.py",
            REPO / "audit_results/ict_125/policy.py", DATA]
    })
    report = ["# MNQ Continuation HTF Canonical Baseline V1 — outcome-free freeze", "",
              "The historical profitable Reversal champion remains unreconstructable; this package does not inherit that claim.", "",
              f"Determinism: {determinism['status']}", "", json.dumps(safe(funnel), sort_keys=True), "",
              "Development outcomes, Validation and SEALED were not opened by this freeze."]
    (FREEZE / "FREEZE_REPORT.md").write_text("\n".join(report) + "\n")
    manifest_hashes()
    print(json.dumps(safe({"funnel": funnel, "determinism": determinism}), indent=2, sort_keys=True))


if __name__ == "__main__": main()
