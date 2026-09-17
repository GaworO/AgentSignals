"""Outcome-blind representation/fidelity audit for V3 ICT narrative tags.

This script never scores performance.  Selection projects only structural
fields from the persisted V3 tags.  Charts stop at the completed catalyst bar,
before V2 displacement, and contain no entry, exit, or outcome markers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


HERE = Path(__file__).resolve().parent
V3 = HERE.parent
ROOT = V3.parents[1]
YEAR = ROOT / "audit_results/ict_125/year_20260910"
sys.path[:0] = [str(YEAR.parent), str(YEAR), str(ROOT)]

SEED = 317
GROUPS = {
    "COMPLETE": {"Complete ICT Narrative"},
    "DESTINATION_ONLY": {"Destination Only"},
    "INCOMPLETE_OTHER": {
        "Source Only", "Objective Already Delivered", "No Clear Narrative",
    },
}
EARLY_END = "2024-06-09"
TICK = 0.25


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str))


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structural_projection(row: dict) -> dict:
    """Explicitly excludes outcome, net P&L, MFE, entry, stop and target."""
    return {
        "trade": row["key"],
        "date": row["date"],
        "direction": row["direction"],
        "signal_emitted": row["signal_emitted"],
        "narrative_evaluated_at": row["narrative_evaluated_at"],
        "auto_source": row["source"],
        "auto_destination": row["destination"],
        "auto_objective_status": row["objective_status"],
        "auto_narrative": row["narrative"],
        "auto_dealing_range": row["dealing_range"],
        "auto_session_tags": row["session_tags"],
    }


def _take(items: list[dict], count: int, rng: random.Random) -> list[dict]:
    items = sorted(items, key=lambda row: row["trade"])
    rng.shuffle(items)
    return items[:count]


def select() -> list[dict]:
    # Load both cohorts only after projecting away every outcome-bearing field.
    pool = []
    for name in ("discovery_tags.jsonl", "validation_tags.jsonl"):
        pool.extend(structural_projection(row) for row in read_jsonl(V3 / name))
    rng = random.Random(SEED)
    selected = []
    for group, categories in GROUPS.items():
        members = [row for row in pool if row["auto_narrative"] in categories]
        early = [row for row in members if row["date"] < EARLY_END]
        recent = [row for row in members if row["date"] >= EARLY_END]
        chosen = []
        for cohort in (early, recent):
            longs = [row for row in cohort if row["direction"] == "LONG"]
            shorts = [row for row in cohort if row["direction"] == "SHORT"]
            part = _take(longs, 2, rng) + _take(shorts, 2, rng)
            used = {row["trade"] for row in part}
            rest = [row for row in cohort if row["trade"] not in used]
            part += _take(rest, 1, rng)
            assert len(part) == 5
            chosen += part
        for row in chosen:
            row["sample_group"] = group
            row["sample_period"] = "EARLIER" if row["date"] < EARLY_END else "RECENT"
        selected += chosen
    selected.sort(key=lambda row: (row["sample_group"], row["sample_period"], row["trade"]))
    assert len(selected) == 30 and len({row["trade"] for row in selected}) == 30
    write_jsonl(HERE / "blind_sample.jsonl", selected)
    write(HERE / "selection_manifest.json", {
        "seed": SEED,
        "method": "10 per auto-narrative stratum; within each, 5 Earlier and 5 Recent; minimum 2 LONG and 2 SHORT per period",
        "outcome_used_for_selection": False,
        "fields_used": [
            "key", "date", "direction", "narrative_evaluated_at",
            "automated narrative/source/destination/session tags",
        ],
        "excluded_fields": ["outcome", "net_R", "net_USD", "MFE", "entry", "stop", "target"],
        "input_hashes": {
            "discovery_tags": sha(V3 / "discovery_tags.jsonl"),
            "validation_tags": sha(V3 / "validation_tags.jsonl"),
        },
        "counts": {group: sum(row["sample_group"] == group for row in selected)
                   for group in GROUPS},
    })
    return selected


def event_type(engine, level) -> tuple[str, float]:
    i = int(level.touch)
    if i >= engine.n:
        return "UNTOUCHED", 0.0
    if level.side == 1:
        depth = float(engine.h[i] - level.price)
        close_through = engine.c[i] >= level.price + TICK
        reclaimed = depth >= TICK and engine.c[i] < level.price
    else:
        depth = float(level.price - engine.l[i])
        close_through = engine.c[i] <= level.price - TICK
        reclaimed = depth >= TICK and engine.c[i] > level.price
    if depth < TICK:
        return "TOUCH", max(0.0, depth)
    if close_through:
        return "CLOSE_THROUGH", depth
    if reclaimed:
        return "RECLAIM", depth
    return "TRADE_THROUGH_RAID", depth


def level_class(level) -> str:
    return {"day": "external", "week": "external", "session": "session",
            "H1_equal": "equal"}.get(level.kind, "other")


def iso(engine, index: int, available: bool = False) -> str:
    ms = int(engine.ms[int(index)]) + (60_000 if available else 0)
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def native_candidates(engine, row: dict) -> tuple[list[dict], list[dict]]:
    cutoff = int(np.searchsorted(
        engine.ms,
        int(pd.Timestamp(row["narrative_evaluated_at"]).timestamp() * 1000) - 60_000,
        side="right",
    ) - 1)
    z = 1 if row["direction"] == "LONG" else -1
    source = []
    destination = []
    manual_start = max(0, cutoff - 1_440)  # one full session-cycle review window
    current = float(engine.c[cutoff])
    for level in engine.levels:
        if level.born > cutoff:
            continue
        base = {
            "id": level.id, "kind": level.kind, "class": level_class(level),
            "side": "BSL" if level.side == 1 else "SSL",
            "price": float(level.price), "formed_at": iso(engine, level.source_start),
            "available_at": iso(engine, level.born),
        }
        if level.side == -z and manual_start <= level.touch <= cutoff and level.touch < level.expires:
            et, depth = event_type(engine, level)
            if et != "TOUCH" and z * (current - float(level.price)) > 0:
                source.append({
                    **base, "take_bar": int(level.touch),
                    "take_time": iso(engine, level.touch, available=True),
                    "event_type": et, "sweep_depth_points": depth,
                    "minutes_before_setup": cutoff - int(level.touch),
                    "inside_auto_120_bar_window": cutoff - int(level.touch) < 120,
                })
        if level.side == z and z * (float(level.price) - current) > 0:
            if level.born <= cutoff < level.expires:
                status = "OPEN" if level.touch > cutoff else "DELIVERED"
                destination.append({
                    **base, "status": status,
                    "distance_points": z * (float(level.price) - current),
                    "delivered_at": iso(engine, level.touch, available=True)
                    if level.touch <= cutoff else None,
                })
    source.sort(key=lambda x: (x["minutes_before_setup"],
                               {"external": 0, "equal": 1, "session": 2}.get(x["class"], 3), x["id"]))
    destination.sort(key=lambda x: (
        {"external": 0, "equal": 1, "session": 2}.get(x["class"], 3),
        x["distance_points"], x["id"]))
    return source[:12], destination[:12]


def h1_swings(engine, row: dict) -> tuple[list[dict], list[dict]]:
    cutoff_ms = int(pd.Timestamp(row["narrative_evaluated_at"]).timestamp() * 1000) - 60_000
    cutoff = int(np.searchsorted(engine.ms, cutoff_ms, side="right") - 1)
    z = 1 if row["direction"] == "LONG" else -1
    start = max(0, cutoff - 10 * 1_440)
    f = engine.f.iloc[start:cutoff + 1].copy()
    f["global_i"] = np.arange(start, cutoff + 1)
    h1 = f.set_index("ts").resample("1h").agg(
        start=("global_i", "min"), end=("global_i", "max"),
        high=("high", "max"), low=("low", "min"), count=("high", "size"),
    ).dropna()
    h1 = h1[h1["count"] == 60]
    levels = []
    for side, field in ((1, "high"), (-1, "low")):
        values = h1[field].to_numpy(float)
        for k in range(2, len(h1) - 2):
            if side * values[k] < max(side * values[k - 2:k + 3]):
                continue
            born = int(h1.iloc[k + 2]["end"]) + 1
            if born > cutoff:
                continue
            price = float(values[k])
            cross = np.flatnonzero(
                engine.h[born:cutoff + 1] >= price if side == 1
                else engine.l[born:cutoff + 1] <= price)
            touch = born + int(cross[0]) if len(cross) else engine.n
            levels.append({
                "id": f"H1_swing:{int(engine.ms[int(h1.iloc[k]['start'])])}:{side}",
                "kind": "H1_swing", "class": "swing",
                "side": "BSL" if side == 1 else "SSL", "side_int": side,
                "price": price, "formed_at": iso(engine, int(h1.iloc[k]["start"])),
                "available_at": iso(engine, born), "born": born, "touch": touch,
            })
    current = float(engine.c[cutoff])
    src, dst = [], []
    for level in levels:
        if level["side_int"] == -z and cutoff - 1_440 <= level["touch"] <= cutoff:
            if level["side_int"] == 1:
                depth = engine.h[level["touch"]] - level["price"]
                reclaim = engine.c[level["touch"]] < level["price"]
            else:
                depth = level["price"] - engine.l[level["touch"]]
                reclaim = engine.c[level["touch"]] > level["price"]
            if depth >= TICK and z * (current - level["price"]) > 0:
                src.append({k: v for k, v in level.items() if k not in {"side_int", "born", "touch"}} | {
                    "take_time": iso(engine, level["touch"], available=True),
                    "event_type": "RECLAIM" if reclaim else "CLOSE_THROUGH",
                    "sweep_depth_points": float(depth),
                    "minutes_before_setup": cutoff - level["touch"],
                })
        if level["side_int"] == z and z * (level["price"] - current) > 0 and level["touch"] > cutoff:
            dst.append({k: v for k, v in level.items() if k not in {"side_int", "born", "touch"}} | {
                "status": "OPEN", "distance_points": z * (level["price"] - current),
            })
    src.sort(key=lambda x: (x["minutes_before_setup"], x["id"]))
    dst.sort(key=lambda x: (x["distance_points"], x["id"]))
    return src[:8], dst[:8]


def build_packets(selected: list[dict], engine) -> list[dict]:
    packets = []
    for row in selected:
        native_source, native_destination = native_candidates(engine, row)
        swing_source, swing_destination = h1_swings(engine, row)
        packets.append({
            **row,
            "manual_source_candidates": native_source + swing_source,
            "manual_destination_candidates": native_destination + swing_destination,
            "review_boundary": row["narrative_evaluated_at"],
            "future_bars_included": False,
        })
    write_jsonl(HERE / "blind_review_packets.jsonl", packets)
    return packets


def resample_bars(engine, cutoff: int, minutes: int, interval: str):
    start = max(0, cutoff - minutes + 1)
    f = engine.f.iloc[start:cutoff + 1].set_index("ts")
    return f.resample(interval).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()


def draw_panel(draw, box, bars, levels, title):
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill="#1a1a2e", outline="#333333")
    draw.text((x0 + 8, y0 + 6), title, fill="#e0e0e0")
    if bars.empty:
        return
    lo = min(float(bars.low.min()), *(float(x["price"]) for x in levels))
    hi = max(float(bars.high.max()), *(float(x["price"]) for x in levels))
    pad = max((hi - lo) * .05, .25)
    lo -= pad; hi += pad
    def yy(price):
        return int(y1 - 20 - (float(price) - lo) / (hi - lo) * (y1 - y0 - 48))
    step = max(1.0, (x1 - x0 - 24) / len(bars))
    for k, (_ts, bar) in enumerate(bars.iterrows()):
        x = int(x0 + 12 + (k + .5) * step)
        up = bar.close >= bar.open
        color = "#00ff88" if up else "#ff4444"
        draw.line((x, yy(bar.low), x, yy(bar.high)), fill=color, width=1)
        left = int(x - max(1, step * .28)); right = int(x + max(1, step * .28))
        draw.rectangle((left, yy(max(bar.open, bar.close)), right,
                        max(yy(max(bar.open, bar.close)) + 1, yy(min(bar.open, bar.close)))),
                       fill=color)
    colors = {"external": "#ffaa00", "equal": "#cc44ff", "session": "#4488ff", "swing": "#00cccc"}
    for n, level in enumerate(levels[:10]):
        y = yy(level["price"])
        color = colors.get(level.get("class"), "#aaaaaa")
        draw.line((x0 + 4, y, x1 - 4, y), fill=color, width=1)
        draw.text((x0 + 8, max(y0 + 18, y - 13)),
                  f"{level['kind']} {level['side']} {level['price']:.2f}", fill=color)


def chart_packet(engine, packet: dict, index: int) -> Path:
    eval_ms = int(pd.Timestamp(packet["narrative_evaluated_at"]).timestamp() * 1000)
    cutoff = int(np.searchsorted(engine.ms, eval_ms - 60_000, side="right") - 1)
    levels = packet["manual_source_candidates"][:5] + packet["manual_destination_candidates"][:5]
    img = Image.new("RGB", (1600, 900), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    draw.text((24, 16), f"BLIND ICT REVIEW {index:02d} | {packet['trade']} | {packet['direction']} | cutoff {packet['narrative_evaluated_at']}", fill="#e0e0e0")
    draw.text((24, 42), "Orange external | purple equal | blue session | cyan closed-H1 swing | NO POST-CATALYST BARS", fill="#aaaaaa")
    context = resample_bars(engine, cutoff, 3 * 1_440, "15min")
    detail = resample_bars(engine, cutoff, 240, "1min")
    draw_panel(draw, (20, 70, 1580, 455), context, levels, "3-day / 15-minute context")
    draw_panel(draw, (20, 475, 1580, 860), detail, levels, "4-hour / 1-minute detail")
    out = HERE / "charts" / f"{index:02d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def charts(packets: list[dict], engine) -> None:
    manifest = []
    for index, packet in enumerate(packets, 1):
        path = chart_packet(engine, packet, index)
        manifest.append({"index": index, "trade": packet["trade"], "chart": str(path.relative_to(ROOT))})
    write(HERE / "chart_manifest.json", {
        "future_bars_included": False, "outcomes_shown": False,
        "charts": manifest,
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("select", "packets", "charts", "all"), required=True)
    args = parser.parse_args()
    if args.stage in {"select", "all"}:
        selected = select()
    else:
        selected = read_jsonl(HERE / "blind_sample.jsonl")
    if args.stage == "select":
        print(json.dumps({"selected": len(selected), "outcome_used": False}, indent=2)); return
    engine = pickle.loads((YEAR / "policy_engine.pkl").read_bytes())
    if args.stage in {"packets", "all"}:
        packets = build_packets(selected, engine)
    else:
        packets = read_jsonl(HERE / "blind_review_packets.jsonl")
    if args.stage in {"charts", "all"}:
        charts(packets, engine)
    print(json.dumps({"selected": len(selected), "packets": len(packets),
                      "charts": len(packets) if args.stage in {"charts", "all"} else 0,
                      "outcome_used": False}, indent=2))


if __name__ == "__main__":
    main()
