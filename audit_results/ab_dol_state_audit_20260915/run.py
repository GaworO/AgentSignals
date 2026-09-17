#!/usr/bin/env python3
"""Tag-only ranked-DOL audit of the frozen corrected 453-trade A/B replay."""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORRECTED = ROOT / "audit_results/ab_sequence_correction_20260913/replay"
NARRATIVE = ROOT / "audit_results/A_CONT_V3_ICT_NARRATIVE"
REVIEW = NARRATIVE / "representation_audit_20260914"
YEAR = ROOT / "audit_results/ict_125/year_20260910"
ANATOMY = ROOT / "audit_results/ab_a_cont_anatomy_20260913"
sys.path[:0] = [str(REVIEW), str(YEAR.parent), str(YEAR), str(ROOT)]

from audit import h1_swings, native_candidates  # noqa: E402
from detcore.a_cont_v3_ict_dol import tag_setup_dol  # noqa: E402
from detcore.a_cont_v3_ict_ledger import levels_from_review_packet  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, default=str))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def session_name(timestamp: str) -> str:
    stamp = pd.Timestamp(timestamp).tz_convert("America/New_York")
    minute = stamp.hour * 60 + stamp.minute
    if minute >= 1080 or minute < 120:
        return "ASIA"
    if minute < 300:
        return "LO"
    if minute < 570:
        return "PREM"
    if minute < 660:
        return "NYAM"
    if minute < 810:
        return "NYL"
    if minute < 960:
        return "NYPM"
    return "PM_AH"


def signal_map() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for path in sorted(CORRECTED.glob("*/causal_signals.jsonl")):
        for row in read_jsonl(path):
            rows[row["key"]] = row
    return rows


def frozen_orders_projection() -> list[dict]:
    signals = signal_map()
    orders = [
        row for row in read_jsonl(CORRECTED / "execution/orders.jsonl")
        if row.get("filled") and row.get("scored")
    ]
    projected = []
    for order in orders:
        signal = signals[order["key"]]
        projected.append({
            "key": order["key"],
            "setup_id": order["setup_id"],
            "date": order["date"],
            "fill_ms": int(order["fill_ms"]),
            "entry": float(order["entry"]),
            "stop": float(order["stop"]),
            "initial_risk_points": abs(float(order["entry"]) - float(order["stop"])),
            "setup_family": signal["cls"],
            "a_subtype": signal["model"] if signal["cls"] == "A" else None,
            "model": signal["model"],
            "direction": signal["dir"],
            "session": session_name(signal["emitted"]),
            "signal_emitted": signal["emitted"],
        })
    projected.sort(key=lambda row: (row["fill_ms"], row["key"]))
    if len(projected) != 453 or len({row["key"] for row in projected}) != 453:
        raise RuntimeError("corrected frozen population is not exactly 453 unique fills")
    return projected


def direction_tag(engine, base: dict, direction: str) -> dict:
    evaluated_at = pd.Timestamp(base["fill_ms"], unit="ms", tz="UTC").isoformat()
    review_row = {
        "direction": direction,
        "narrative_evaluated_at": evaluated_at,
    }
    native_source, native_destination = native_candidates(engine, review_row)
    swing_source, swing_destination = h1_swings(engine, review_row)
    levels = levels_from_review_packet(engine, {
        "manual_source_candidates": native_source + swing_source,
        "manual_destination_candidates": native_destination + swing_destination,
    })
    return tag_setup_dol(
        engine,
        direction=direction,
        evaluated_at_ms=base["fill_ms"],
        extra_levels=levels,
        include_native_levels=False,
    ).to_dict()


def latest_resolution(tag: dict) -> int | None:
    values = [row.get("resolution_bar") for row in tag["active_source_set"]]
    values = [int(value) for value in values if value is not None]
    return max(values) if values else None


def resolve_delivery(long_tag: dict, short_tag: dict) -> tuple[str | None, dict | None, str]:
    tags = {"LONG": long_tag, "SHORT": short_tag}
    sourced = {
        direction: latest_resolution(tag)
        for direction, tag in tags.items()
        if tag["source_present"]
    }
    if sourced:
        latest = max(value for value in sourced.values() if value is not None)
        winners = [direction for direction, value in sourced.items() if value == latest]
        if len(winners) == 1:
            return winners[0], tags[winners[0]], "latest_active_source_resolution"
        return None, None, "tied_active_source_resolution"
    open_directions = [
        direction for direction, tag in tags.items()
        if tag["dol_status"] == "OPEN" and tag["current_dol"] is not None
    ]
    if len(open_directions) == 1:
        direction = open_directions[0]
        return direction, tags[direction], "unique_dol_only_direction"
    return None, None, "dual_or_absent_unsourced_dol"


def compact_tag(base: dict, delivery_direction: str | None, selected: dict | None,
                reason: str) -> dict:
    evaluated_at_ms = int(base["fill_ms"])
    if delivery_direction is None or selected is None:
        return {
            **base,
            "dol_evaluated_at_ms": evaluated_at_ms,
            "dol_state": "NO_CLEAR_DOL",
            "selected_dol": None,
            "dol_price": None,
            "dol_tier": None,
            "dol_direction": None,
            "dol_status": "UNRESOLVED",
            "source_present": False,
            "narrative_class": "NO_CLEAR_NARRATIVE",
            "successor_dol": None,
            "stacked_constituents": [],
            "stacked_confluence": 0,
            "direction_aligned_with_dol": False,
            "distance_to_dol_points": None,
            "distance_to_dol_R": None,
            "delivery_resolution_reason": reason,
        }
    if selected.get("current_dol") is None:
        return {
            **base,
            "dol_evaluated_at_ms": evaluated_at_ms,
            "dol_state": "NO_CLEAR_DOL",
            "selected_dol": None,
            "dol_price": None,
            "dol_tier": None,
            "dol_direction": delivery_direction,
            "dol_status": selected["dol_status"],
            "source_present": bool(selected["source_present"]),
            "source_count": int(selected["active_source_count"]),
            "source_families": selected["source_families"],
            "narrative_class": selected["narrative"],
            "successor_dol": None,
            "stacked_constituents": [],
            "stacked_confluence": 0,
            "direction_aligned_with_dol": False,
            "distance_to_dol_points": None,
            "distance_to_dol_R": None,
            "delivery_resolution_reason": reason,
        }
    current = selected["current_dol"]
    status = selected["dol_status"]
    aligned = base["direction"] == delivery_direction
    if aligned and status == "OPEN":
        state = "ALIGNED_OPEN"
    elif aligned and status == "DELIVERED":
        state = "ALIGNED_DELIVERED"
    elif not aligned and status in {"OPEN", "DELIVERED"}:
        state = "OPPOSING"
    else:
        state = "NO_CLEAR_DOL"
    points = abs(float(current["pool_price"]) - float(base["entry"]))
    risk = float(base["initial_risk_points"])
    return {
        **base,
        "dol_evaluated_at_ms": evaluated_at_ms,
        "dol_state": state,
        "selected_dol": current["pool_id"],
        "dol_price": float(current["pool_price"]),
        "dol_tier": int(current["priority_tier"]),
        "dol_direction": delivery_direction,
        "dol_status": status,
        "source_present": bool(selected["source_present"]),
        "source_count": int(selected["active_source_count"]),
        "source_families": selected["source_families"],
        "narrative_class": selected["narrative"],
        "successor_dol": selected["next_dol_after_delivery"],
        "stacked_constituents": current["constituent_levels"],
        "stacked_confluence": int(current["stacked_confluence"]),
        "direction_aligned_with_dol": aligned,
        "distance_to_dol_points": points,
        "distance_to_dol_R": points / risk if risk > 0 else None,
        "delivery_resolution_reason": reason,
    }


def tag_stage() -> None:
    engine = pickle.loads((YEAR / "policy_engine.pkl").read_bytes())
    rows = []
    for index, base in enumerate(frozen_orders_projection(), 1):
        long_tag = direction_tag(engine, base, "LONG")
        short_tag = direction_tag(engine, base, "SHORT")
        direction, selected, reason = resolve_delivery(long_tag, short_tag)
        rows.append(compact_tag(base, direction, selected, reason))
        if index % 50 == 0:
            print(json.dumps({"tagged": index, "total": 453}), flush=True)
    write_jsonl(HERE / "dol_tags_453.jsonl", rows)
    write_json(HERE / "tag_manifest.json", {
        "rows": len(rows),
        "definitions_sha256": sha(HERE / "definitions.json"),
        "signals_regenerated": False,
        "execution_replayed": False,
        "v3_modified": False,
        "snapshot_cutoff": "strictly before fill",
        "state_counts": dict(Counter(row["dol_state"] for row in rows)),
    })


def cached_mfe() -> dict[str, dict]:
    out = {}
    for path in (
        ANATOMY / "discovery_rows.jsonl",
        ANATOMY / "validation_rows.jsonl",
        NARRATIVE / "dol_discovery_tags.jsonl",
        NARRATIVE / "dol_validation_tags.jsonl",
    ):
        if not path.exists():
            continue
        for row in read_jsonl(path):
            out[row["key"]] = row
    return out


def conservative_mfe(engine, order: dict) -> float:
    z = int(order["z"])
    entry = float(order["entry"])
    stop = float(order["stop"])
    risk = abs(entry - stop)
    start = int(np.searchsorted(engine.ms, int(order["fill_ms"]), side="right"))
    end = int(np.searchsorted(engine.ms, int(order["exit_ms"]), side="left"))
    best = 0.0
    for index in range(start, min(end, engine.n)):
        adverse = engine.l[index] <= stop if z == 1 else engine.h[index] >= stop
        if adverse:
            break
        excursion = engine.h[index] - entry if z == 1 else entry - engine.l[index]
        best = max(best, float(excursion) / risk)
    if order.get("outcome") == "TP":
        best = max(best, z * (float(order["target"]) - entry) / risk)
    return best


def profit_factor(rows: list[dict]) -> float | None:
    gains = sum(float(row["net_USD"]) for row in rows if float(row["net_USD"]) > 0)
    losses = -sum(float(row["net_USD"]) for row in rows if float(row["net_USD"]) < 0)
    return gains / losses if losses else (float("inf") if gains else None)


def metrics(rows: list[dict], *, mfe: bool = False) -> dict:
    count = len(rows)
    result = {
        "Trades": count,
        "WR_pct": 100 * sum(float(row["net_R"]) > 0 for row in rows) / count if count else None,
        "PF": profit_factor(rows),
        "Net_R": sum(float(row["net_R"]) for row in rows),
    }
    if mfe:
        result.update({
            "Avg_R": sum(float(row["net_R"]) for row in rows) / count if count else None,
            "+0.5R_Reach_pct": 100 * sum(row["reached_0_5R"] for row in rows) / count if count else None,
            "+1R_Reach_pct": 100 * sum(row["reached_1_0R"] for row in rows) / count if count else None,
        })
    return result


def period(row: dict) -> str:
    return "DISCOVERY" if row["date"] < "2024-06-09" else "VALIDATION"


def year_period(row: dict) -> str:
    value = row["date"]
    for label, start, end in (
        ("2022-23", "2022-06-09", "2023-06-09"),
        ("2023-24", "2023-06-09", "2024-06-09"),
        ("2024-25", "2024-06-09", "2025-06-09"),
        ("2025-26", "2025-06-09", "2026-06-09"),
    ):
        if start <= value < end:
            return label
    raise ValueError(value)


def analyze_stage() -> None:
    tags = {row["key"]: row for row in read_jsonl(HERE / "dol_tags_453.jsonl")}
    orders = {
        row["key"]: row for row in read_jsonl(CORRECTED / "execution/orders.jsonl")
        if row.get("filled") and row.get("scored")
    }
    engine = pickle.loads((YEAR / "policy_engine.pkl").read_bytes())
    cache = cached_mfe()
    rows = []
    reused = 0
    for key, tag in tags.items():
        order = orders[key]
        net_r = float(order["net"]) / (
            float(order["risk"]) * 2.0 * float(order["qty"])
        )
        cached = cache.get(key)
        geometry_ok = bool(
            cached
            and cached.get("entry") is not None
            and cached.get("stop") is not None
            and abs(float(cached["entry"]) - float(order["entry"])) <= 1.25
            and abs(float(cached["stop"]) - float(order["stop"])) <= 0.11
            and cached.get("reached_0_5R") is not None
            and cached.get("reached_1_0R") is not None
        )
        if geometry_ok:
            reached_05 = bool(cached["reached_0_5R"])
            reached_10 = bool(cached["reached_1_0R"])
            mfe_source = "existing_cache"
            reused += 1
        else:
            mfe_r = conservative_mfe(engine, order)
            reached_05 = mfe_r >= 0.5
            reached_10 = mfe_r >= 1.0
            mfe_source = "cached_1m_path"
        rows.append({
            **tag,
            "net_USD": float(order["net"]),
            "net_R": net_r,
            "reached_0_5R": reached_05,
            "reached_1_0R": reached_10,
            "mfe_source": mfe_source,
            "period": period(tag),
            "year_period": year_period(tag),
        })
    rows.sort(key=lambda row: (row["fill_ms"], row["key"]))
    states = ["ALIGNED_OPEN", "ALIGNED_DELIVERED", "OPPOSING", "NO_CLEAR_DOL"]
    families = ["A", "B"]
    tiers = [1, 2, 3, 4, 5]
    full = [{"DOL_state": "ALL_A/B", **metrics(rows, mfe=True)}]
    full += [{"DOL_state": state, **metrics([r for r in rows if r["dol_state"] == state], mfe=True)} for state in states]
    strategy_state = [
        {"Strategy": family, "DOL_state": state,
         **metrics([r for r in rows if r["setup_family"] == family and r["dol_state"] == state])}
        for family in families for state in states
    ]
    tier = [
        {"DOL_tier": value, **metrics([r for r in rows if r.get("dol_tier") == value])}
        for value in tiers
    ]
    tier.append({"DOL_tier": "NO_CLEAR", **metrics([r for r in rows if r.get("dol_tier") is None])})
    strategy_tier = [
        {"Strategy": family, "DOL_tier": value,
         **metrics([r for r in rows if r["setup_family"] == family and r.get("dol_tier") == value])}
        for family in families for value in tiers
    ]
    strategy_tier += [
        {"Strategy": family, "DOL_tier": "NO_CLEAR",
         **metrics([r for r in rows if r["setup_family"] == family and r.get("dol_tier") is None])}
        for family in families
    ]
    objective_delivery = [
        {"Strategy": family, "Objective_status": status,
         **metrics([r for r in rows
                    if (family == "Combined" or r["setup_family"] == family)
                    and r["dol_status"] == status])}
        for family in ["Combined", "A", "B"] for status in ["OPEN", "DELIVERED"]
    ]
    distance_tests = {
        "<1R": lambda value: value < 1,
        "1–2R": lambda value: 1 <= value <= 2,
        ">2R": lambda value: value > 2,
    }
    open_rows = [r for r in rows if r["dol_status"] == "OPEN" and r["distance_to_dol_R"] is not None]
    distance = [
        {"Distance": label, **metrics([r for r in open_rows if test(float(r["distance_to_dol_R"]))])}
        for label, test in distance_tests.items()
    ]
    chronological = [
        {"Period": split, "Strategy": family, "DOL_state": state,
         **metrics([r for r in rows
                    if r["period"] == split
                    and (family == "Combined" or r["setup_family"] == family)
                    and r["dol_state"] == state])}
        for split in ["DISCOVERY", "VALIDATION"]
        for family in ["Combined", "A", "B"]
        for state in states
    ]
    annual = [
        {"Period": split, "DOL_state": state,
         **metrics([r for r in rows if r["year_period"] == split and r["dol_state"] == state])}
        for split in ["2022-23", "2023-24", "2024-25", "2025-26"]
        for state in states
    ]
    result = {
        "audit": "Corrected canonical A/B ranked-DOL tag-only audit",
        "integrity": {
            "definitions_sha256": sha(HERE / "definitions.json"),
            "tag_manifest_sha256": sha(HERE / "tag_manifest.json"),
            "signals_regenerated": False,
            "execution_replayed": False,
            "v3_modified": False,
            "rows": len(rows),
            "mfe_cache_rows_reused": reused,
            "mfe_rows_from_cached_1m_path": len(rows) - reused,
        },
        "baseline": metrics(rows),
        "full_sample": full,
        "strategy_state": strategy_state,
        "tier": tier,
        "strategy_tier": strategy_tier,
        "objective_delivery": objective_delivery,
        "distance_open_dol_only": distance,
        "chronological": chronological,
        "annual": annual,
    }
    write_jsonl(HERE / "tagged_outcomes_453.jsonl", rows)
    write_json(HERE / "results.json", result)
    print(json.dumps({"baseline": result["baseline"], "integrity": result["integrity"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("tag", "analyze"))
    args = parser.parse_args()
    if args.stage == "tag":
        tag_stage()
    else:
        analyze_stage()


if __name__ == "__main__":
    main()
