#!/usr/bin/env python3
"""Freeze HTF and execution-horizon DOLs for the existing random ten.

This stage deliberately does not open the stored outcome file.  HTF DOL is the
already-frozen ranked-DOL snapshot.  Execution DOL uses only completed M15/M5
bars, causally confirmed two-right-bar swings, and already-registered major
completed-session liquidity.  Ordinary M1 pivots are never candidates.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
YEAR = ROOT / "audit_results/ict_125/year_20260910"
sys.path[:0] = [str(YEAR), str(YEAR.parent), str(ROOT)]

from detcore.a_cont_v3_ict_ledger import (  # noqa: E402
    CLUSTER_TOLERANCE_POINTS,
    _family,
    _native_levels,
    _objective_state,
)


TICK = 0.25


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


def sha(rows: list[dict]) -> str:
    raw = "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows)
    return hashlib.sha256(raw.encode()).hexdigest()


def continuous_prefix(engine, cutoff: int) -> pd.DataFrame:
    """Return only the uninterrupted M1 segment visible at the cutoff."""
    start = cutoff
    while start > 0 and int(engine.ms[start]) - int(engine.ms[start - 1]) == 60_000:
        start -= 1
    frame = engine.f.iloc[start:cutoff + 1].copy()
    frame["ms"] = engine.ms[start:cutoff + 1]
    return frame


def closed_bars(frame: pd.DataFrame, minutes: int, evaluated_at_ms: int) -> list[dict]:
    """Create only fully observed, wall-clock-aligned bars."""
    bucket_ms = minutes * 60_000
    groups: dict[int, list[tuple]] = {}
    for row in frame.itertuples(index=False):
        ms = int(row.ms)
        groups.setdefault(ms // bucket_ms * bucket_ms, []).append(
            (ms, float(row.open), float(row.high), float(row.low), float(row.close))
        )
    out = []
    for bucket, rows in sorted(groups.items()):
        rows.sort()
        known_ms = bucket + bucket_ms
        if known_ms > evaluated_at_ms or len(rows) != minutes:
            continue
        if rows[0][0] != bucket or rows[-1][0] != bucket + bucket_ms - 60_000:
            continue
        out.append({
            "ms": bucket, "known_ms": known_ms,
            "o": rows[0][1], "h": max(x[2] for x in rows),
            "l": min(x[3] for x in rows), "c": rows[-1][4],
        })
    return out


def timeframe_state(bars: list[dict], tf: str) -> tuple[list[dict], list[dict]]:
    """Return causal raid/close-break events and confirmed swings for one TF."""
    confirmed: list[dict] = []
    events: list[dict] = []
    k = 2
    for i, bar in enumerate(bars):
        # Existing confirmed-swing convention: pivot becomes known after two
        # right-hand M15 candles close.
        p = i - k
        if p >= k:
            window = bars[p - k:p + k + 1]
            center = bars[p]
            if center["h"] == max(x["h"] for x in window) and sum(x["h"] == center["h"] for x in window) == 1:
                confirmed.append({"id": f"{tf}_SWING_HIGH|{center['ms']}", "side": 1,
                                  "price": center["h"], "known_ms": bar["known_ms"], "tf": tf})
            if center["l"] == min(x["l"] for x in window) and sum(x["l"] == center["l"] for x in window) == 1:
                confirmed.append({"id": f"{tf}_SWING_LOW|{center['ms']}", "side": -1,
                                  "price": center["l"], "known_ms": bar["known_ms"], "tf": tf})
        for level in confirmed:
            if level.get("consumed_ms") is not None or level["known_ms"] > bar["known_ms"]:
                continue
            if level["side"] == 1 and bar["h"] >= level["price"] + TICK:
                direction = "SHORT" if bar["c"] < level["price"] else "LONG" if bar["c"] >= level["price"] + TICK else None
                if direction:
                    kind = "opposite_side_raid" if direction == "SHORT" else "m15_close_break"
                    events.append({"known_ms": bar["known_ms"], "direction": direction,
                                   "kind": kind, "level_id": level["id"], "price": level["price"], "tf": tf})
                level["consumed_ms"] = bar["known_ms"]
            elif level["side"] == -1 and bar["l"] <= level["price"] - TICK:
                direction = "LONG" if bar["c"] > level["price"] else "SHORT" if bar["c"] <= level["price"] - TICK else None
                if direction:
                    kind = "opposite_side_raid" if direction == "LONG" else "m15_close_break"
                    events.append({"known_ms": bar["known_ms"], "direction": direction,
                                   "kind": kind, "level_id": level["id"], "price": level["price"], "tf": tf})
                level["consumed_ms"] = bar["known_ms"]
    return events, confirmed


def major_session_candidates(engine, cutoff: int, current: float, direction: str,
                             native_levels=None) -> list[dict]:
    z = 1 if direction == "LONG" else -1
    out = []
    for level in native_levels if native_levels is not None else _native_levels(engine):
        if level.kind != "session" or level.born > cutoff or cutoff >= level.expires or level.side != z:
            continue
        family, _rank, session = _family(level, engine)
        if family != "MAJOR_SESSION" or z * (level.price - current) <= 0:
            continue
        state = _objective_state(engine, level, cutoff)
        if state["status"] != "OPEN":
            continue
        out.append({"id": level.id, "price": float(level.price), "type": f"SESSION_{session}",
                    "tf": "session", "distance_points": z * (float(level.price) - current)})
    return out


def execution_dol(engine, cutoff: int, evaluated_at_ms: int, native_levels=None) -> dict:
    current = float(engine.c[cutoff])
    prefix = continuous_prefix(engine, cutoff)
    events15, levels15 = timeframe_state(closed_bars(prefix, 15, evaluated_at_ms), "M15")
    events5, levels5 = timeframe_state(closed_bars(prefix, 5, evaluated_at_ms), "M5")
    events = events15 + events5
    event = max(events, key=lambda row: (row["known_ms"], 1 if row["tf"] == "M15" else 0,
                                         row["kind"], row["level_id"]), default=None)
    direction = event["direction"] if event else None
    if direction is None:
        return {"direction": None, "price": None, "type": None, "tf": None,
                "distance_points": None, "resolution": None,
                "selection_reason": "no causal M15 raid or close-break establishes local delivery"}
    z = 1 if direction == "LONG" else -1
    candidates = []
    for level in levels15 + levels5:
        if level["side"] != z or level["known_ms"] > evaluated_at_ms:
            continue
        if level.get("consumed_ms") is not None and level["consumed_ms"] <= evaluated_at_ms:
            continue
        if z * (level["price"] - current) <= 0:
            continue
        candidates.append({"id": level["id"], "price": float(level["price"]),
                           "type": f"{level['tf']}_CONFIRMED_SWING", "tf": level["tf"],
                           "distance_points": z * (float(level["price"]) - current)})
    candidates.extend(major_session_candidates(engine, cutoff, current, direction, native_levels))
    if not candidates:
        return {"direction": direction, "price": None, "type": None, "tf": None,
                "distance_points": None, "resolution": event,
                "selection_reason": "local delivery is resolved, but no open M15/major-session objective remains"}
    candidates.sort(key=lambda row: (row["price"], row["id"]))
    clusters: list[list[dict]] = []
    for row in candidates:
        if not clusters or abs(row["price"] - clusters[-1][-1]["price"]) > CLUSTER_TOLERANCE_POINTS:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    pools = []
    for group in clusters:
        tf_rank = {"M15": 0, "M5": 1, "session": 2}
        representative = min(group, key=lambda row: (row["distance_points"], tf_rank[row["tf"]], row["id"]))
        pools.append({**representative, "constituents": group, "stacked": len(group)})
    chosen = min(pools, key=lambda row: (row["distance_points"], -row["stacked"], row["id"]))
    return {"direction": direction, "price": chosen["price"], "type": chosen["type"],
            "tf": chosen["tf"], "distance_points": chosen["distance_points"],
            "resolution": event, "constituents": chosen["constituents"],
            "selection_reason": "nearest open confirmed M15/M5 external swing or registered major-session pool after the latest causal M15/M5 raid/close-break"}


def relationship(htf: dict, execution: dict, entry: float) -> str:
    hd, ed = htf.get("dol_direction"), execution.get("direction")
    hp, ep = htf.get("dol_price"), execution.get("price")
    if hd and hp is not None and ed and ep is not None:
        if hd != ed:
            return "LOCAL_COUNTERMOVE_WITH_HTF"
        if abs(ep - entry) < abs(hp - entry):
            return "SAME_DIRECTION_NESTED"
        return "CONFLICTING / AMBIGUOUS"
    if hd and hp is not None:
        return "HTF_ONLY"
    if ed and ep is not None:
        return "EXECUTION_ONLY"
    return "CONFLICTING / AMBIGUOUS"


def main() -> None:
    # This file is intentionally the only input packet: it was frozen before
    # the prior reveal and contains no outcome/P&L columns.
    packets = read_jsonl(HERE / "preoutcome_assessments.jsonl")
    engine = pickle.loads((YEAR / "policy_engine.pkl").read_bytes())
    out = []
    for packet in packets:
        htf = packet["dol"]
        evaluated = int(htf["fill_ms"])
        cutoff = int(np.searchsorted(engine.ms, evaluated - 60_000, side="right") - 1)
        execution = execution_dol(engine, cutoff, evaluated)
        risk = float(htf["initial_risk_points"])
        execution["distance_from_entry_points"] = (
            abs(float(execution["price"]) - float(htf["entry"]))
            if execution.get("price") is not None else None
        )
        execution["distance_R"] = (
            execution["distance_from_entry_points"] / risk
            if execution["distance_from_entry_points"] is not None and risk > 0 else None
        )
        relation = relationship(htf, execution, float(htf["entry"]))
        trade_dir = packet["direction"]
        vs_htf = "ALIGNED" if trade_dir == htf.get("dol_direction") else "OPPOSING" if htf.get("dol_direction") else "UNCLEAR"
        vs_execution = "ALIGNED" if trade_dir == execution.get("direction") else "OPPOSING" if execution.get("direction") else "UNCLEAR"
        if vs_htf == "ALIGNED" and vs_execution == "ALIGNED":
            interpretation = "local trade aligned with both layers"
        elif vs_htf == "OPPOSING" and vs_execution == "ALIGNED":
            interpretation = "trade aligned only with execution liquidity inside the broader opposing delivery"
        elif vs_htf == "ALIGNED" and vs_execution == "OPPOSING":
            interpretation = "HTF-aligned trade entered during an opposing local counter-move"
        elif vs_htf == "OPPOSING" and vs_execution == "OPPOSING":
            interpretation = "trade contradicts both resolved layers"
        else:
            interpretation = "one horizon is not defensibly resolved"
        first = execution.get("price") if execution.get("price") is not None else htf.get("dol_price")
        first_label = "execution DOL" if execution.get("price") is not None else "HTF DOL"
        same = execution.get("direction") == htf.get("dol_direction") and execution.get("price") is not None
        after = ("yes; after the nearer same-direction pool is delivered, the open HTF pool remains the logical next ladder objective"
                 if same and abs(execution["price"] - htf["entry"]) < abs(htf["dol_price"] - htf["entry"])
                 else "no; the execution objective is a counter-move, so HTF delivery would require a fresh local turn"
                 if execution.get("price") is not None and execution.get("direction") != htf.get("dol_direction")
                 else "not established because no separate execution DOL is defensible")
        z = 1 if trade_dir == "LONG" else -1
        needs_htf = bool(htf.get("dol_price") is not None and trade_dir == htf.get("dol_direction") and
                         z * (float(packet["setup"]["target"]) - float(htf["dol_price"])) >= 0)
        out.append({
            "sample_index": packet["sample_index"], "trade_id": packet["trade_id"],
            "family": packet["setup_family"], "direction": trade_dir,
            "entry": float(htf["entry"]), "stop": float(htf["stop"]),
            "target": float(packet["setup"]["target"]), "risk_points": risk,
            "htf_dol": {"direction": htf.get("dol_direction"), "price": htf.get("dol_price"),
                        "tier": htf.get("dol_tier"), "distance_R": htf.get("distance_to_dol_R"),
                        "status": htf.get("dol_status"), "id": htf.get("selected_dol")},
            "execution_dol": execution, "horizon_relationship": relation,
            "vs_htf": vs_htf, "vs_execution": vs_execution,
            "alignment_interpretation": interpretation,
            "path_expectation": {
                "first_liquidity": {"layer": first_label, "price": first},
                "normal_local_delivery": (f"price advances {execution.get('direction', htf.get('dol_direction')).lower()} toward {first:.2f} before invalidating the local structure" if first is not None else "no directional local path is frozen"),
                "htf_after_execution": after,
                "trade_needs_htf_to_succeed": needs_htf,
            },
            "preoutcome_inputs_only": True,
        })
    write_jsonl(HERE / "two_horizon_preoutcome.jsonl", out)
    manifest = {
        "rows": len(out), "sample_ids_unchanged": True,
        "input": "preoutcome_assessments.jsonl only (no outcome fields)",
        "htf_layer": "previously frozen ranked DOL snapshot",
        "execution_layer": "latest causal closed-M15/M5 raid or close-break -> nearest open confirmed-M15/M5 or registered major-session pool",
        "m1_pivots_used": False, "m5_used": True,
        "m5_reason": "closed M5 bars and the existing two-right-bar causal swing convention are available; no M1 pivots are admitted",
        "sha256": sha(out),
    }
    (HERE / "two_horizon_preoutcome_freeze.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    for row in out:
        print(json.dumps({k: row[k] for k in ("sample_index", "direction", "htf_dol", "execution_dol", "horizon_relationship", "vs_htf", "vs_execution")}, default=str))


if __name__ == "__main__":
    main()
