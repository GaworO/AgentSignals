"""Load only the registered 30 fills, their order ledger, and matching 1m bars."""
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .types import Bar, Trade

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "ML RL"
SAMPLE = SOURCE / "frozen_sample_30.jsonl"
DOL = SOURCE / "frozen_30_dol_outputs.jsonl"
ORDERS = Path(__file__).with_name("frozen_orders_30.jsonl")
SIGNALS = Path(__file__).with_name("frozen_signals_30.jsonl")
DOL_STATES = Path(__file__).with_name("frozen_dol_states_30.jsonl")
BARS = ROOT / "audit_data/MNQ_databento_2022_1m.csv"
EXPECTED_SHA = "446e2ac01ac3f1f7eac93a8e5ea3df5a67a4de6566e8bc63f5df0bdfcf16442f"
ORDERS_SHA = "2d9d3f093abc2afb37f03a1770b1a01d89257fdeb46b1b92e58ff8bb2f437243"
SIGNALS_SHA = "5d2321571f616cd9df7d9f0ba7b4e07dea307679e680b4ae27a4b0a52e93935a"
DOL_STATES_SHA = "4c23e7fb2e287f7a2422fdf0af7dfcc26f06cbaf4b13ab24e878839fb63980b5"


def _rows(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_frozen_trades(sample=SAMPLE, dol=DOL, orders=ORDERS, bars=BARS,
                       signals=SIGNALS, dol_states=DOL_STATES, include_dol_states=True):
    sample = Path(sample)
    if hashlib.sha256(sample.read_bytes()).hexdigest() != EXPECTED_SHA:
        raise ValueError("Frozen 30-trade sample hash mismatch")
    if hashlib.sha256(Path(orders).read_bytes()).hexdigest() != ORDERS_SHA:
        raise ValueError("Frozen canonical order snapshot hash mismatch")
    if hashlib.sha256(Path(signals).read_bytes()).hexdigest() != SIGNALS_SHA:
        raise ValueError("Frozen causal signal snapshot hash mismatch")
    if include_dol_states and hashlib.sha256(Path(dol_states).read_bytes()).hexdigest() != DOL_STATES_SHA:
        raise ValueError("Frozen per-step DOL snapshot hash mismatch")
    selected = _rows(sample)
    if len(selected) != 30 or len({x["key"] for x in selected}) != 30:
        raise ValueError("Expected 30 distinct frozen trades")
    keys = {x["key"] for x in selected}
    tagged = {x["key"]: x for x in _rows(dol) if x["key"] in keys}
    canonical = {x["key"]: x for x in _rows(orders) if x.get("key") in keys and x.get("filled") and x.get("scored")}
    setups = {x["key"]: x for x in _rows(signals) if x["key"] in keys}
    states = {x["key"]: x["steps"] for x in _rows(dol_states)} if include_dol_states else {}
    if len(tagged) != 30 or len(canonical) != 30 or len(setups) != 30:
        raise ValueError("Frozen DOL tags, canonical orders, or signals missing")
    if include_dol_states and len(states) != 30:
        raise ValueError("Frozen per-step DOL state missing")
    windows = {}
    by_day = {}
    for row in selected:
        key = row["key"]
        order = canonical[key]
        if int(order["fill_ms"]) != int(row["fill_ms"]):
            raise ValueError(f"Fill mismatch: {key}")
        first = int(order["fill_ms"])
        # SL/TP exit_ms marks bar close; EOD exit_ms marks flatten bar open.
        last = int(order["exit_ms"]) - (0 if order["outcome"] == "EOD" else 60_000)
        windows[key] = (first, last)
        by_day.setdefault(row["date"], []).append(key)
    matched = {key: [] for key in keys}
    with Path(bars).open(newline="") as stream:
        for row in csv.DictReader(stream):
            day = row["ts_event"][:10]
            if day not in by_day:
                continue
            ms = int(datetime.fromisoformat(row["ts_event"]).timestamp() * 1000)
            for key in by_day[day]:
                first, last = windows[key]
                if first - 120 * 60_000 <= ms <= last:
                    matched[key].append(Bar(ms, *(float(row[name]) for name in ("open", "high", "low", "close"))))
    trades = []
    for row in selected:
        key = row["key"]
        order, tag, signal = canonical[key], tagged[key], setups[key]
        all_bars = tuple(sorted(matched[key], key=lambda x: x.ms))
        first, last = windows[key]
        path = tuple(bar for bar in all_bars if bar.ms >= first)
        pre = tuple(bar for bar in all_bars if bar.ms < first)
        if not path or path[0].ms != first or path[-1].ms != last:
            raise ValueError(f"Missing endpoint bars: {key}")
        if any(b.ms - a.ms != 60_000 for a, b in zip(path, path[1:])):
            raise ValueError(f"Missing execution minute: {key}")
        risk = float(order["risk"])
        if risk <= 0 or int(order["qty"]) <= 0:
            raise ValueError(f"Invalid risk/size: {key}")
        direction = int(order["z"])
        entry, stop, target = (float(order[n]) for n in ("entry", "stop", "target"))
        if direction * (entry - stop) <= 0 or direction * (target - entry) <= 0:
            raise ValueError(f"Invalid bracket: {key}")
        expected = float(order["net"]) / (risk * 2 * int(order["qty"]))
        if abs(expected - float(tag["net_R"])) > 1e-10:
            raise ValueError(f"Canonical accounting mismatch: {key}")
        if int(datetime.fromisoformat(signal["emitted"]).timestamp() * 1000) > first:
            raise ValueError(f"FVG signal unavailable at entry: {key}")
        steps = tuple(states.get(key, ()))
        if include_dol_states and (len(steps) != len(path) or any(
            int(step["decision_ms"]) != first + i * 60_000 for i, step in enumerate(steps)
        )):
            raise ValueError(f"Per-step DOL timestamps mismatch: {key}")
        trades.append(Trade(
            key, int(row["sample_index"]), direction, entry, stop, target,
            int(order["qty"]), risk, first, int(order["exit_ms"]), order["outcome"],
            expected, tag["session"], tag["setup_id"],
            1 if tag.get("htf_dol_direction") == "LONG" else -1 if tag.get("htf_dol_direction") == "SHORT" else 0,
            float(tag["dol_price"]) if tag.get("dol_price") is not None else None,
            tag.get("dol_status"),
            1 if tag.get("execution_dol_direction") == "LONG" else -1 if tag.get("execution_dol_direction") == "SHORT" else 0,
            float(tag["execution_dol_price"]) if tag.get("execution_dol_price") is not None else None,
            path, pre, float(signal["fvg_lo"]), float(signal["fvg_hi"]), steps,
            int(datetime.fromisoformat(signal["emitted"]).timestamp() * 1000),
            float(signal["ce"]),
        ))
    return tuple(trades)
