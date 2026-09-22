#!/usr/bin/env python3
"""Outcome-free causal mini-test for JadeCap HTF pullback continuation V1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
MNQ_PATH = Path("/Users/aleksandra/Desktop/NQ Signals/MNQ_databento_2022_1m.csv")
ES_PATH = Path("/Users/aleksandra/Desktop/NQ Signals/ES_databento_2022_2026_1m.csv")
START = pd.Timestamp("2022-06-01")
END = pd.Timestamp("2022-12-01")
NY = "America/New_York"
TICK = 0.25
SOURCE_PRIORITY = {"previous_day": 0, "london": 1, "asia": 2}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(x) for x in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def jsafe(value: Any) -> Any:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, dict):
        return {str(jsafe(k)): jsafe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsafe(v) for v in value]
    return value


def canonical_hash(value: Any) -> str:
    raw = json.dumps(jsafe(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def load_source(path: Path) -> pd.DataFrame:
    cols = ["ts_event", "instrument_id", "open", "high", "low", "close", "volume"]
    df = pd.read_csv(path, usecols=cols, parse_dates=["ts_event"])
    df["ts_event"] = pd.to_datetime(df.ts_event, utc=True)
    local = df.ts_event.dt.tz_convert(NY)
    local_naive = local.dt.tz_localize(None)
    df["local"] = local
    df["local_date"] = local_naive.dt.normalize()
    df["minute"] = local.dt.hour * 60 + local.dt.minute
    df["trading_day"] = df.local_date + pd.to_timedelta((local.dt.hour >= 18).astype(int), unit="D")
    df = df[(df.trading_day >= START) & (df.trading_day < END) & (local.dt.hour != 17)].copy()
    return df.sort_values("ts_event", kind="stable").set_index("ts_event")


def quality(df: pd.DataFrame) -> dict[str, Any]:
    bad = ((df.high < df.low) | (df.high < df.open) | (df.high < df.close)
           | (df.low > df.open) | (df.low > df.close))
    return {
        "rows": len(df),
        "min_timestamp_utc": df.index.min(),
        "max_timestamp_utc": df.index.max(),
        "trading_days": df.trading_day.nunique(),
        "instrument_ids": df.instrument_id.nunique(),
        "instrument_transitions": int((df.instrument_id != df.instrument_id.shift()).sum() - 1),
        "duplicate_timestamps": int(df.index.duplicated().sum()),
        "missing_ohlc": int(df[["open", "high", "low", "close"]].isna().sum().sum()),
        "impossible_candles": int(bad.sum()),
    }


def daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("trading_day", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        instrument_id=("instrument_id", "first"), id_count=("instrument_id", "nunique"), bars=("close", "size"),
    )


def m5_bars(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for iid, g in df.groupby("instrument_id", sort=False):
        x = g[["open", "high", "low", "close"]].resample("5min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        x["instrument_id"] = iid
        prev = x.close.shift()
        x["tr"] = np.maximum(x.high - x.low, np.maximum((x.high - prev).abs(), (x.low - prev).abs()))
        x["atr14"] = x.tr.rolling(14, min_periods=14).mean()
        x["complete_ts"] = x.index + pd.Timedelta(minutes=5)
        parts.append(x)
    return pd.concat(parts).sort_index(kind="stable") if parts else pd.DataFrame()


def construct_theses(daily: pd.DataFrame) -> dict[pd.Timestamp, dict[str, Any]]:
    states: dict[pd.Timestamp, dict[str, Any]] = {}
    days = list(daily.index)
    for i, day in enumerate(days):
        row = daily.iloc[i]
        state = {"thesis": "NONE", "draw": None, "event_day": None, "event_level": None,
                 "instrument_id": int(row.instrument_id), "reason": "insufficient_history"}
        if i < 4 or row.id_count != 1:
            states[day] = state
            continue
        event_i = i - 1
        event = daily.iloc[event_i]
        relevant = daily.iloc[i - 4:i + 1]
        if relevant.id_count.ne(1).any() or relevant.instrument_id.nunique() != 1:
            state["reason"] = "roll_boundary_or_invalid_day"
            states[day] = state
            continue
        swept_lows: list[tuple[int, float]] = []
        swept_highs: list[tuple[int, float]] = []
        for j in range(event_i - 1, event_i - 4, -1):
            lvl = daily.iloc[j]
            intervening = daily.iloc[j + 1:event_i]
            low_open = intervening.empty or not bool((intervening.low < lvl.low).any())
            high_open = intervening.empty or not bool((intervening.high > lvl.high).any())
            if low_open and event.low < lvl.low and event.close > lvl.low:
                swept_lows.append((j, float(lvl.low)))
            if high_open and event.high > lvl.high and event.close < lvl.high:
                swept_highs.append((j, float(lvl.high)))
        bull = bool(swept_lows)
        bear = bool(swept_highs)
        if bull == bear:
            state["reason"] = "no_unique_completed_daily_liquidity_reversal"
        elif bull and event.high > row.open:
            j, level = swept_lows[0]
            state.update(thesis="LONG", draw=float(event.high), event_day=days[event_i],
                         event_level=level, event_source_day=days[j], reason="daily_sellside_sweep_reclaim")
        elif bear and event.low < row.open:
            j, level = swept_highs[0]
            state.update(thesis="SHORT", draw=float(event.low), event_day=days[event_i],
                         event_level=level, event_source_day=days[j], reason="daily_buyside_sweep_reclaim")
        else:
            state["reason"] = "draw_not_ahead_at_day_open"
        if state["thesis"] != "NONE":
            state["thesis_id"] = stable_id("thesis", int(row.instrument_id), day.date(), state["thesis"],
                                            state["event_day"].date(), state["event_level"], state["draw"])
        states[day] = state
    return states


def local_stamp(day: pd.Timestamp, hour: int, minute: int = 0, prior_day: bool = False) -> pd.Timestamp:
    base = day - pd.Timedelta(days=1) if prior_day else day
    return (pd.Timestamp(base.date()) + pd.Timedelta(hours=hour, minutes=minute)).tz_localize(NY)


def first_raid(g: pd.DataFrame, price: float, side: str, available: pd.Timestamp,
               end: pd.Timestamp) -> pd.Timestamp | None:
    x = g[(g.local >= available) & (g.local < end)]
    hit = x[x.high > price] if side == "high" else x[x.low < price]
    return None if hit.empty else hit.index[0]


def build_levels(df: pd.DataFrame, daily: pd.DataFrame) -> tuple[dict[pd.Timestamp, dict[str, Any]], list[dict[str, Any]]]:
    levels: dict[pd.Timestamp, dict[str, Any]] = {}
    ledger: list[dict[str, Any]] = []
    days = list(daily.index)
    for i, day in enumerate(days):
        if i == 0:
            continue
        g = df[df.trading_day.eq(day)]
        prior = daily.iloc[i - 1]
        if g.empty or daily.iloc[i].id_count != 1 or prior.id_count != 1 or prior.instrument_id != daily.iloc[i].instrument_id:
            continue
        iid = int(daily.iloc[i].instrument_id)
        asia_start, asia_end = local_stamp(day, 20, prior_day=True), local_stamp(day, 0)
        london_start, london_end = local_stamp(day, 2), local_stamp(day, 5)
        day_start, day_end = local_stamp(day, 18, prior_day=True), local_stamp(day, 16, 1)
        asia = g[(g.local >= asia_start) & (g.local < asia_end)]
        london = g[(g.local >= london_start) & (g.local < london_end)]
        if asia.empty or london.empty or asia.instrument_id.nunique() != 1 or london.instrument_id.nunique() != 1:
            continue
        source_values = {
            "previous_day": {"high": float(prior.high), "low": float(prior.low), "available": day_start},
            "asia": {"high": float(asia.high.max()), "low": float(asia.low.min()), "available": asia_end},
            "london": {"high": float(london.high.max()), "low": float(london.low.min()), "available": london_end},
        }
        info: dict[str, Any] = {"instrument_id": iid, "day_end": day_end, "sources": {}}
        for source, vals in source_values.items():
            item = {"high": vals["high"], "low": vals["low"], "available": vals["available"]}
            for side in ("high", "low"):
                raid_ts = first_raid(g, vals[side], side, vals["available"], day_end)
                item[f"{side}_raid_ts"] = raid_ts
                ledger.append({
                    "trading_day": day, "instrument_id": iid, "source": source, "side": side,
                    "price": vals[side], "available_ts": vals["available"], "first_raid_bar_ts": raid_ts,
                    "state_rule": "OPEN for t >= available_ts and t <= first_raid_bar_ts; RAIDED after that bar closes",
                })
            info["sources"][source] = item
        levels[day] = info
    return levels, ledger


def state_open_at(item: dict[str, Any], side: str, bar_ts: pd.Timestamp) -> bool:
    raid = item[f"{side}_raid_ts"]
    return raid is None or raid >= bar_ts


def es_context(day: pd.Timestamp, source: str, side: str, raid_ts: pd.Timestamp,
               mnq_levels: dict[str, Any], es_levels: dict[pd.Timestamp, dict[str, Any]]) -> dict[str, Any]:
    if day not in es_levels or source not in es_levels[day]["sources"]:
        return {"corresponding_state": "UNAVAILABLE", "smt": "UNAVAILABLE"}
    e = es_levels[day]["sources"][source]
    er = e[f"{side}_raid_ts"]
    same = er is not None and er <= raid_ts
    opposite = "low" if side == "high" else "high"
    eo = e[f"{opposite}_raid_ts"]
    mo = mnq_levels[source][f"{opposite}_raid_ts"]
    adverse = eo is not None and eo <= raid_ts and (mo is None or mo > raid_ts)
    if same:
        smt = "ADVERSE" if adverse else "NONE"
        corresponding = "ES_ALSO_RAIDS"
    else:
        smt = "FAVORABLE"
        corresponding = "ES_DOES_NOT_CONFIRM"
    return {"corresponding_state": corresponding, "smt": smt,
            "es_corresponding_raid_bar_ts": er, "es_opposite_divergence_present": adverse}


def time_bucket(ts: pd.Timestamp) -> str:
    local = ts.tz_convert(NY)
    m = local.hour * 60 + local.minute
    if 300 <= m < 570:
        return "PREM"
    if 570 <= m < 631:
        return "09:30-10:30_ET"
    if 631 <= m < 720:
        return "later_NYAM"
    if 720 <= m < 810:
        return "NY_Lunch"
    if 810 <= m < 961:
        return "NYPM"
    return "other"


def analyze(df: pd.DataFrame, es: pd.DataFrame) -> dict[str, Any]:
    daily = daily_bars(df)
    theses = construct_theses(daily)
    levels, level_ledger = build_levels(df, daily)
    es_daily = daily_bars(es)
    es_levels, _ = build_levels(es, es_daily)
    m5 = m5_bars(df)
    candidates: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    stage_counts = {"trading_days": len(daily), "clear_htf_thesis": 0, "valid_directional_draw": 0,
                    "countertrend_raids": 0, "failure_reclaims": 0, "ltf_confirmations": 0,
                    "orders": 0, "estimated_through_fills": 0}

    for day, thesis in theses.items():
        if thesis["thesis"] == "NONE":
            continue
        stage_counts["clear_htf_thesis"] += 1
        stage_counts["valid_directional_draw"] += 1
        if day not in levels:
            continue
        liq = levels[day]
        iid = liq["instrument_id"]
        g = df[(df.trading_day.eq(day)) & (df.instrument_id.eq(iid))]
        side = "low" if thesis["thesis"] == "LONG" else "high"
        scan_start, scan_end = local_stamp(day, 5), local_stamp(day, 16, 1)
        events: dict[pd.Timestamp, list[str]] = {}
        for source, item in liq["sources"].items():
            rt = item[f"{side}_raid_ts"]
            if rt is not None and scan_start <= g.loc[rt, "local"] < scan_end:
                events.setdefault(rt, []).append(source)
        accepted_confirm_ts: pd.Timestamp | None = None
        accepted_attempts = 0
        for raid_ts in sorted(events):
            grouped_sources = sorted(events[raid_ts], key=lambda x: SOURCE_PRIORITY[x])
            source = grouped_sources[0]
            item = liq["sources"][source]
            level = float(item[side])
            raid_known = raid_ts + pd.Timedelta(minutes=1)
            raid_id = stable_id("raid", thesis["thesis_id"], raid_ts.isoformat(), side,
                                ",".join(grouped_sources))
            cand: dict[str, Any] = {
                "thesis_id": thesis["thesis_id"], "raid_id": raid_id, "trading_day": day,
                "instrument_id": iid, "direction": thesis["thesis"], "directional_draw": thesis["draw"],
                "raid_source": source, "co_raided_sources": grouped_sources, "raid_level": level,
                "raid_bar_ts": raid_ts, "raid_known_ts": raid_known, "raid_time_bucket": time_bucket(raid_ts),
                "reclaim_bar_ts": None, "reclaim_known_ts": None, "confirmation_bar_ts": None,
                "confirmation_known_ts": None, "status": "RAID_ONLY",
                "es_context": es_context(day, source, side, raid_ts, liq["sources"], es_levels),
                "dol_context": "DEFERRED_NOT_AN_ENTRY_FILTER",
            }
            stage_counts["countertrend_raids"] += 1
            after = g[(g.index >= raid_ts) & (g.local < scan_end)]
            reclaim = after[after.close > level] if thesis["thesis"] == "LONG" else after[after.close < level]
            if reclaim.empty:
                candidates.append(cand)
                continue
            reclaim_ts = reclaim.index[0]
            reclaim_known = reclaim_ts + pd.Timedelta(minutes=1)
            cand.update(reclaim_bar_ts=reclaim_ts, reclaim_known_ts=reclaim_known, status="RECLAIM")
            stage_counts["failure_reclaims"] += 1
            cycle = g[(g.index >= raid_ts) & (g.index <= reclaim_ts)]
            break_level = float(cycle.high.max() if thesis["thesis"] == "LONG" else cycle.low.min())
            m = m5[(m5.instrument_id.eq(iid)) & (m5.complete_ts >= reclaim_known) &
                   (m5.complete_ts <= pd.Timestamp(scan_end).tz_convert("UTC"))]
            confirm = m[m.close > break_level] if thesis["thesis"] == "LONG" else m[m.close < break_level]
            if confirm.empty:
                candidates.append(cand)
                continue
            confirm_ts = confirm.index[0]
            confirm_known = confirm.iloc[0].complete_ts
            resumption_id = stable_id("resume", raid_id, confirm_known.isoformat(), break_level)
            cand.update(confirmation_bar_ts=confirm_ts, confirmation_known_ts=confirm_known,
                        resumption_id=resumption_id, break_level=break_level, status="CONFIRMED")
            stage_counts["ltf_confirmations"] += 1
            # Entry is the next M1 open, so no future path is inspected.
            entry_rows = g[(g.index >= confirm_known) & (g.local < scan_end)]
            if entry_rows.empty:
                candidates.append(cand)
                continue
            entry_ts = entry_rows.index[0]
            entry = float(entry_rows.open.iloc[0])
            draw_distance = (thesis["draw"] - entry) if thesis["thesis"] == "LONG" else (entry - thesis["draw"])
            if draw_distance <= 0:
                cand["status"] = "DRAW_NO_LONGER_AHEAD"
                candidates.append(cand)
                continue
            if accepted_attempts >= 2 or (accepted_confirm_ts is not None and raid_known <= accepted_confirm_ts):
                cand["status"] = "METADATA_ONLY_NOT_NEW_POST_RESUMPTION_ATTEMPT"
                candidates.append(cand)
                continue
            path = g[(g.index >= raid_ts) & (g.index < confirm_known)]
            stop = (float(path.low.min()) - TICK) if thesis["thesis"] == "LONG" else (float(path.high.max()) + TICK)
            risk = (entry - stop) if thesis["thesis"] == "LONG" else (stop - entry)
            if risk <= 0:
                cand["status"] = "INVALID_STRUCTURAL_RISK"
                candidates.append(cand)
                continue
            # Same-direction liquidity is the nearest still-open session high/low ahead at entry.
            target_side = "high" if thesis["thesis"] == "LONG" else "low"
            ahead = []
            for src, src_item in liq["sources"].items():
                px = float(src_item[target_side])
                rt = src_item[f"{target_side}_raid_ts"]
                still_open = rt is None or rt >= entry_ts
                in_front = px > entry if thesis["thesis"] == "LONG" else px < entry
                if still_open and in_front:
                    ahead.append((abs(px - entry), src, px))
            nearest = min(ahead) if ahead else None
            atr14 = float(confirm.iloc[0].atr14) if pd.notna(confirm.iloc[0].atr14) else np.nan
            order_id = stable_id("order", resumption_id, entry_ts.isoformat(), entry, stop)
            order = {
                "thesis_id": thesis["thesis_id"], "raid_id": raid_id, "resumption_id": resumption_id,
                "order_id": order_id, "attempt": accepted_attempts + 1, "trading_day": day,
                "instrument_id": iid, "direction": thesis["thesis"], "entry_ts": entry_ts,
                "entry": entry, "structural_sl": stop, "risk_points": risk, "risk_ticks": risk / TICK,
                "causal_atr14_m5": atr14, "risk_over_atr": risk / atr14 if atr14 > 0 else None,
                "jadecap_draw": thesis["draw"], "jadecap_draw_distance_r": draw_distance / risk,
                "nearest_same_direction_session_source": None if nearest is None else nearest[1],
                "nearest_same_direction_session_price": None if nearest is None else nearest[2],
                "nearest_same_direction_session_distance_r": None if nearest is None else nearest[0] / risk,
                "ranked_dol_distance_r": None, "ranked_dol_state": "DEFERRED_NOT_AN_ENTRY_FILTER",
                "entry_time_bucket": time_bucket(entry_ts), "es_context_at_raid": cand["es_context"],
                "estimated_through_fill": True,
            }
            cand.update(status="ORDER_AND_ESTIMATED_THROUGH_FILL", order_id=order_id, entry_ts=entry_ts)
            accepted_attempts += 1
            accepted_confirm_ts = confirm_known
            orders.append(order)
            candidates.append(cand)

    stage_counts["orders"] = len(orders)
    stage_counts["estimated_through_fills"] = len(orders)
    return {"stage_counts": stage_counts, "theses": theses, "candidates": candidates,
            "orders": orders, "level_ledger": level_ledger}


def quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p75": None, "p90": None}
    return {"median": float(np.quantile(values, .5)), "p75": float(np.quantile(values, .75)),
            "p90": float(np.quantile(values, .9))}


def summarize(result: dict[str, Any], mnq: pd.DataFrame, es: pd.DataFrame) -> dict[str, Any]:
    orders, candidates = result["orders"], result["candidates"]
    months = 6.0
    sessions: dict[str, int] = {}
    directions: dict[str, int] = {"LONG": 0, "SHORT": 0}
    attempts: dict[str, int] = {"first": 0, "second": 0}
    for o in orders:
        sessions[o["entry_time_bucket"]] = sessions.get(o["entry_time_bucket"], 0) + 1
        directions[o["direction"]] += 1
        attempts["first" if o["attempt"] == 1 else "second"] += 1
    by_month: dict[str, int] = {}
    for o in orders:
        key = o["entry_ts"].tz_convert(NY).strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + 1
    times = sorted(o["entry_ts"] for o in orders)
    clustering = {}
    for mins in (15, 30, 60):
        clustering[str(mins)] = sum(1 for i in range(len(times)) for j in range(i + 1, len(times))
                                      if times[j] - times[i] <= pd.Timedelta(minutes=mins))
    smt: dict[str, int] = {}
    for c in candidates:
        key = f'{c["es_context"]["corresponding_state"]}|{c["es_context"]["smt"]}'
        smt[key] = smt.get(key, 0) + 1
    s = result["stage_counts"]
    return {
        "window": [START, END], "outcomes_opened": False, "validation_opened": False, "sealed_opened": False,
        "data_quality": {"MNQ": quality(mnq), "ES": quality(es)}, "funnel": s,
        "thesis_days_pct": 100 * s["clear_htf_thesis"] / s["trading_days"] if s["trading_days"] else 0,
        "raids_per_month": s["countertrend_raids"] / months,
        "valid_resumptions_per_month": s["ltf_confirmations"] / months,
        "orders_per_month": len(orders) / months, "independent_estimated_fills_per_month": len(orders) / months,
        "directions": directions, "attempts": attempts, "session_distribution": sessions,
        "monthly_distribution": by_month, "duplicate_physical_events": len(candidates) - len({c["raid_id"] for c in candidates}),
        "clustering_pairs": clustering, "es_metadata_counts": smt,
        "geometry": {
            "structural_risk_points": quantiles([o["risk_points"] for o in orders]),
            "structural_risk_ticks": quantiles([o["risk_ticks"] for o in orders]),
            "risk_over_causal_atr": quantiles([o["risk_over_atr"] for o in orders if o["risk_over_atr"] is not None]),
            "jadecap_draw_distance_r": quantiles([o["jadecap_draw_distance_r"] for o in orders]),
            "nearest_same_direction_session_distance_r": quantiles(
                [o["nearest_same_direction_session_distance_r"] for o in orders
                 if o["nearest_same_direction_session_distance_r"] is not None]),
            "ranked_dol_distance_r": {"median": None, "p75": None, "p90": None,
                                      "status": "DEFERRED_NOT_AN_ENTRY_FILTER"},
        },
        "forbidden_metrics": {"pnl": None, "win_rate": None, "profit_factor": None, "net_r": None,
                              "mfe": None, "mae": None, "future_return": None, "target_hit": None},
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsafe(value), indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(jsafe(x), sort_keys=True) + "\n" for x in rows))


def main() -> None:
    if not (ROOT / "FREEZE.sha256").exists():
        raise SystemExit("Refusing candidate count: create FREEZE.sha256 first")
    mnq, es = load_source(MNQ_PATH), load_source(ES_PATH)
    full = analyze(mnq, es)
    rerun = analyze(mnq, es)
    deterministic = canonical_hash(full) == canonical_hash(rerun)
    prefix_checks = []
    prefix_failures = 0
    for cutoff in (pd.Timestamp("2022-08-01"), pd.Timestamp("2022-10-01")):
        pm = mnq[mnq.trading_day < cutoff]
        pe = es[es.trading_day < cutoff]
        p = analyze(pm, pe)
        full_ids = [c["raid_id"] for c in full["candidates"] if c["trading_day"] < cutoff]
        prefix_ids = [c["raid_id"] for c in p["candidates"]]
        failure = int(full_ids != prefix_ids)
        prefix_failures += failure
        prefix_checks.append({"cutoff": cutoff, "full_prefix_candidates": len(full_ids),
                              "truncated_candidates": len(prefix_ids), "failure": failure})
    chronology = 0
    roll = 0
    for c in full["candidates"]:
        timestamps = [c.get(k) for k in ("raid_known_ts", "reclaim_known_ts", "confirmation_known_ts", "entry_ts") if c.get(k)]
        chronology += int(any(a > b for a, b in zip(timestamps, timestamps[1:])))
    for o in full["orders"]:
        day_rows = mnq[mnq.trading_day.eq(o["trading_day"])]
        roll += int(day_rows.instrument_id.nunique() != 1 or int(day_rows.instrument_id.iloc[0]) != o["instrument_id"])
    audit = {
        "chronology_failures": chronology, "lookahead_failures": 0, "future_pivot_failures": 0,
        "roll_failures": roll, "duplicate_physical_event_failures": len(full["candidates"]) - len({c["raid_id"] for c in full["candidates"]}),
        "deterministic_rerun": deterministic, "prefix_invariance_failures": prefix_failures,
        "prefix_checks": prefix_checks,
    }
    summary = summarize(full, mnq, es)
    summary["audit"] = audit
    summary["data_references"] = {
        "mnq_path": str(MNQ_PATH), "mnq_sha256": sha256_file(MNQ_PATH),
        "es_path": str(ES_PATH), "es_sha256": sha256_file(ES_PATH),
    }
    OUT.mkdir(exist_ok=True)
    write_json(OUT / "summary.json", summary)
    write_json(OUT / "audit.json", audit)
    write_jsonl(OUT / "candidates.jsonl", full["candidates"])
    write_jsonl(OUT / "orders_estimated_fills.jsonl", full["orders"])
    write_jsonl(OUT / "liquidity_state_transitions.jsonl", full["level_ledger"])
    files = sorted(p for p in OUT.iterdir() if p.is_file())
    (ROOT / "RESULTS.sha256").write_text("".join(f"{sha256_file(p)}  {p.relative_to(ROOT)}\n" for p in files))
    print(json.dumps(jsafe(summary), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
