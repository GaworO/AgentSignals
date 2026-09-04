#!/usr/bin/env python3
"""Forward-only, execution-incapable shadow book for M15 -> M5 A/B + shallow.

This module intentionally imports neither guardrails nor any broker/webhook sender.  Its
only side effects are local JSON files and an isolated detector subprocess.  It can be
mounted in the main Flask service without giving the strategy an order path.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from m15_hybrid_detector import SETTINGS


HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
BUFFER_PATH = os.environ.get("M15_SHADOW_BUF") or os.environ.get("BUF") or os.path.join(DATA_DIR, "buffer.csv")
SEEN_PATH = os.path.join(DATA_DIR, "m15_seen.json")
TRACE_PATH = os.path.join(DATA_DIR, "m15_candidate_trace.json")
GROUPS_PATH = os.path.join(DATA_DIR, "m15_shadow_groups.json")
STATUS_PATH = os.path.join(DATA_DIR, "m15_shadow_status.json")
LOCK_PATH = os.path.join(DATA_DIR, "m15_shadow_scan.lock")
PINE_PATH = os.path.join(HERE, "Pine_M15_M5_AB_Shallow_April_2026.txt")
EXAMPLES_MD = os.path.join(HERE, "M15_M5_AB_Shallow_examples_April_2026.md")
BACKTEST_PATH = os.path.join(HERE, "M15_M5_AB_SHALLOW_BACKTEST_RESULTS.json")
ASSET_DIR = os.path.join(HERE, "m15_examples")

VERSION = SETTINGS.version
ENABLED = os.environ.get("M15_SHADOW_ENABLED", "1") != "0"
PV = 2.0
TICK = 0.25
COST_PER_CONTRACT = 2.24
GROUP_RISK_USD = 500.0
LEG_RISK_USD = GROUP_RISK_USD / 2.0
MAX_QTY = 15
EOD_HOUR, EOD_MINUTE = 15, 55
MAX_TRADES_DAY = 3
DAY_LOSS_N = 1
DAY_LOSS_USD = 550.0
DAY_TARGET_USD = 1000.0
FRESH_MIN = 10
GAP_REPRIME_MIN = 30
NY = ZoneInfo("America/New_York")

_state_lock = threading.RLock()
_worker_lock = threading.Lock()
_worker_running = False
_pending_asof_ms: Optional[int] = None


def _json_load(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _json_save(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    staged = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
    with open(staged, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(staged, path)


class _FileLock:
    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
        self.handle = open(LOCK_PATH, "a+")
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self.handle.close()


def _parse_ms(value: Any) -> int:
    text = str(value or "").strip().replace("Z", "+00:00")
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return int(stamp.timestamp() * 1000)


def _et(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=dt.timezone.utc).astimezone(NY)


def _session(stamp: dt.datetime) -> str:
    minute = stamp.hour * 60 + stamp.minute
    if minute >= 18 * 60 or minute < 2 * 60:
        return "ASIA"
    if minute < 5 * 60:
        return "LO"
    if minute < 9 * 60 + 30:
        return "PREM"
    if minute < 11 * 60:
        return "NYAM"
    if minute < 13 * 60 + 30:
        return "NYL"
    if minute < 16 * 60:
        return "NYPM"
    return "PM_AH"


def _trading_day(stamp: dt.datetime) -> str:
    if stamp.hour >= 18:
        stamp += dt.timedelta(days=1)
    return stamp.date().isoformat()


def _tick(value: float) -> float:
    return round(round(float(value) / TICK) * TICK, 10)


def _qty(entry: float, stop: float) -> int:
    risk_cost = abs(float(entry) - float(stop)) * PV + COST_PER_CONTRACT
    return min(MAX_QTY, int(LEG_RISK_USD // risk_cost)) if risk_cost > 0 else 0


def signal_key(signal: Dict[str, Any]) -> str:
    return "%s|%d|%s|%.2f|%.2f" % (
        VERSION, int(signal["bos_ms"]), signal["dir"],
        round(float(signal["entry"]), 2), round(float(signal["SL"]), 2),
    )


def _profile(signal: Dict[str, Any]) -> Tuple[bool, str, str, str, dt.datetime]:
    stamp = _et(int(signal["bos_ms"]))
    sess, day = _session(stamp), _trading_day(stamp)
    if stamp.weekday() == 0 and sess == "PREM":
        return False, "monday_prem", sess, day, stamp
    minute = stamp.hour * 60 + stamp.minute
    if 15 * 60 + 30 <= minute < 18 * 60:
        return False, "late_15_30_to_18_00", sess, day, stamp
    return True, "ok", sess, day, stamp


def _group_guard(groups: Iterable[Dict[str, Any]], day: str) -> Tuple[bool, str]:
    admitted = [g for g in groups if g.get("decision") == "shadow"]
    if any(g.get("status") in ("pending", "open") for g in admitted):
        return False, "position_open"
    resolved = [g for g in admitted if g.get("day") == day and g.get("filled")]
    if len(resolved) >= MAX_TRADES_DAY:
        return False, "max_3_filled_groups"
    net = sum(float(g.get("net") or 0.0) for g in resolved)
    losses = sum(float(g.get("net") or 0.0) < 0 for g in resolved)
    if losses >= DAY_LOSS_N:
        return False, "stop_after_1_losing_group"
    if net <= -DAY_LOSS_USD:
        return False, "daily_loss_lock"
    if net >= DAY_TARGET_USD:
        return False, "daily_profit_lock"
    return True, "ok"


def build_group(signal: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Create one group decision and its two independently scored sibling limits."""
    key = signal_key(signal)
    allowed, reason, sess, day, stamp = _profile(signal)
    if allowed:
        allowed, reason = _group_guard(existing, day)
    direction = signal["dir"]
    sign = 1.0 if direction == "LONG" else -1.0
    deep_entry = float(signal["entry"])
    stop = float(signal["SL"])
    signal_close = float(signal["signal_close"])
    shallow_entry = _tick(signal_close + SETTINGS.shallow_fraction * (deep_entry - signal_close))
    shallow_risk = abs(shallow_entry - stop)
    leg_specs = [
        ("deep", deep_entry, stop, float(signal["TP"]), signal.get("tp_src") or "swing_or_2R"),
    ]
    omitted_legs = []
    if shallow_risk >= SETTINGS.min_leg_stop_points:
        leg_specs.append((
            "shallow", shallow_entry, _tick(stop),
            _tick(shallow_entry + sign * 2.0 * shallow_risk), "shallow_2R",
        ))
    else:
        omitted_legs.append("shallow_stop_below_5_points")

    legs = []
    for name, entry, sl, tp, tp_src in leg_specs:
        qty = _qty(entry, sl)
        if qty < 1:
            omitted_legs.append("%s_qty_zero" % name)
            continue
        risk = abs(float(entry) - float(sl))
        legs.append({
            "key": key + "|" + name, "leg": name, "entry": round(float(entry), 2),
            "sl": round(float(sl), 2), "tp": round(float(tp), 2), "tp_src": tp_src,
            "risk_pts": round(risk, 2), "qty": qty, "risk_budget_usd": LEG_RISK_USD,
            "actual_risk_usd": round(risk * PV * qty, 2), "fill_window_m1_bars": 20,
            "risk_with_cost_usd": round((risk * PV + COST_PER_CONTRACT) * qty, 2),
            "status": "waiting" if allowed else "blocked", "filled": False,
            "fill_bars_seen": 0, "last_bar_ms": int(signal["bos_ms"]),
            "fill_ms": None, "fill_price": None, "exit_ms": None,
            "exit_price": None, "gross_r": None, "net": None,
        })
    # Each sibling owns half of the group budget.  If one sibling cannot be
    # sized, omit it instead of discarding the other valid setup; block only
    # when neither leg fits.  This matches the historical replay.
    if not legs:
        allowed, reason = False, "no_leg_fits_risk_budget"

    return {
        "key": key, "setup_group_id": key, "strategy": "M15 A/B + Shallow",
        "strategy_version": VERSION, "mode": "shadow_only", "execution_capable": False,
        "decision": "shadow" if allowed else "blocked", "block_reason": None if allowed else reason,
        "status": "pending" if allowed else "blocked", "filled": False,
        "setup_timeframe": "M15", "bos_timeframe": "M5", "bos_ms": int(signal["bos_ms"]),
        "entry_ms": int(signal["bos_ms"]), "bos_iso": signal.get("bos_iso"),
        "et": stamp.strftime("%Y-%m-%d %H:%M"), "date": stamp.date().isoformat(),
        "day": day, "week": (stamp - dt.timedelta(days=stamp.weekday())).date().isoformat(),
        "session": sess, "dir": direction, "model": signal.get("model"),
        "cat": signal.get("cat"), "cls": signal.get("cls"), "bias": signal.get("bias"),
        "signal_close": signal_close, "fvg_lo": signal.get("fvg_lo"),
        "fvg_hi": signal.get("fvg_hi"), "net": None, "gross_r": None,
        "end_ms": None, "omitted_legs": omitted_legs, "legs": legs,
    }


def _eod_ms(fill_ms: int) -> int:
    stamp = _et(fill_ms)
    cutoff = stamp.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
    if stamp.hour >= 18 or stamp >= cutoff:
        cutoff += dt.timedelta(days=1)
    return int(cutoff.timestamp() * 1000)


def _finish_leg(leg: Dict[str, Any], outcome: str, gross_r: float, bar_ms: int,
                exit_price: Optional[float] = None) -> None:
    risk_dollars = float(leg["risk_pts"]) * PV * int(leg["qty"])
    leg.update(
        status=outcome, outcome=outcome, gross_r=round(float(gross_r), 6),
        net=round(float(gross_r) * risk_dollars - COST_PER_CONTRACT * int(leg["qty"]), 2),
        exit_ms=int(bar_ms), end_ms=int(bar_ms),
        exit_price=round(float(exit_price), 2) if exit_price is not None else None,
    )


def _advance_leg(leg: Dict[str, Any], group: Dict[str, Any], bar: Dict[str, Any], bar_ms: int) -> bool:
    if leg.get("status") not in ("waiting", "open") or bar_ms <= int(leg.get("last_bar_ms") or -1):
        return False
    previous_ms = int(leg.get("last_bar_ms") or bar_ms)
    previous_close = leg.get("last_close")
    if (leg.get("status") == "open" and leg.get("eod_ms") is not None
            and bar_ms >= int(leg["eod_ms"])):
        # If the feed jumps across 15:55, never score post-cutoff highs/lows.  Exit
        # from the last close that was genuinely known before the cutoff.
        exit_close = float(previous_close) if previous_close is not None else float(bar["close"])
        bull = group["dir"] == "LONG"
        entry, stop = float(leg["entry"]), float(leg["sl"])
        gross = (exit_close - entry) / abs(entry - stop) if bull else (entry - exit_close) / abs(entry - stop)
        _finish_leg(leg, "eod", gross, previous_ms, exit_close)
        return True
    leg["last_bar_ms"] = int(bar_ms)
    direction = group["dir"]
    bull = direction == "LONG"
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    entry, stop, target = float(leg["entry"]), float(leg["sl"]), float(leg["tp"])

    if leg["status"] == "waiting":
        # Parity with the study: the one-minute bar stamped exactly at M5 close is
        # skipped; the first fillable bar starts one minute later.
        if bar_ms <= int(group["bos_ms"]):
            return True
        if int(leg.get("fill_bars_seen") or 0) >= SETTINGS.fill_window_m1_bars:
            # The first 20 following M1 bars were eligible.  The next bar makes
            # the no-fill final, matching the historical replay's end timestamp.
            leg.update(status="no_fill", outcome="no_fill", end_ms=int(bar_ms), exit_ms=int(bar_ms))
            return True
        leg["fill_bars_seen"] = int(leg.get("fill_bars_seen") or 0) + 1
        through = low <= entry - TICK if bull else high >= entry + TICK
        if through:
            leg.update(status="open", filled=True, fill_ms=int(bar_ms), eod_ms=_eod_ms(bar_ms),
                       fill_price=entry, last_close=close)
            hit_stop = low <= stop if bull else high >= stop
            if hit_stop:
                _finish_leg(leg, "loss", -1.0, bar_ms, stop)
            # No target credit on the fill bar: the favourable extreme may pre-date fill.
            return True
        return True

    hit_stop = low <= stop if bull else high >= stop
    hit_target = high >= target if bull else low <= target
    if hit_stop:
        _finish_leg(leg, "loss", -1.0, bar_ms, stop)
    elif bar_ms > int(leg.get("fill_ms") or bar_ms) and hit_target:
        win_r = abs(target - entry) / max(abs(entry - stop), 1e-12)
        _finish_leg(leg, "win", win_r, bar_ms, target)
    elif bar_ms + 60_000 >= int(leg.get("eod_ms") or (2 ** 63 - 1)):
        gross = (close - entry) / abs(entry - stop) if bull else (entry - close) / abs(entry - stop)
        _finish_leg(leg, "eod", gross, bar_ms, close)
    else:
        leg["last_close"] = close
    return True


def _refresh_group(group: Dict[str, Any]) -> None:
    if group.get("decision") != "shadow":
        return
    legs = group.get("legs") or []
    if any(leg.get("status") == "open" for leg in legs):
        group["status"] = "open"
        return
    if any(leg.get("status") == "waiting" for leg in legs):
        group["status"] = "pending"
        return
    group["end_ms"] = max((int(leg.get("end_ms") or group["bos_ms"]) for leg in legs), default=group["bos_ms"])
    filled = [leg for leg in legs if leg.get("filled")]
    group["filled"] = bool(filled)
    if not filled:
        group.update(status="no_fill", net=0.0, gross_r=0.0)
        return
    net = sum(float(leg.get("net") or 0.0) for leg in filled)
    gross = sum(float(leg.get("gross_r") or 0.0) for leg in filled)
    status = "win" if net > 0 else ("loss" if net < 0 else "scratch")
    group.update(status=status, net=round(net, 2), gross_r=round(gross, 6))


def advance_bar(bar: Dict[str, Any]) -> int:
    """Advance the dedicated shadow book on one closed M1 bar."""
    try:
        bar_ms = _parse_ms(bar.get("ts_event"))
        for key in ("high", "low", "close"):
            float(bar[key])
    except Exception:
        return 0
    # The file lock keeps the dedicated book safe even if the Flask service is
    # ever started with more than one worker process.
    with _state_lock, _FileLock():
        groups = _json_load(GROUPS_PATH, [])
        changed = 0
        for group in groups:
            if group.get("decision") != "shadow" or group.get("status") not in ("pending", "open"):
                continue
            touched = False
            for leg in group.get("legs", []):
                if _advance_leg(leg, group, bar, bar_ms):
                    touched = True
            if touched:
                _refresh_group(group)
                changed += 1
        if changed:
            _json_save(GROUPS_PATH, groups)
        return changed


def process_scan_result(signals: List[Dict[str, Any]], trace: List[Dict[str, Any]],
                        detector_status: Dict[str, Any], asof_ms: int) -> Dict[str, Any]:
    """Prime/deduplicate a detector snapshot and admit only genuinely fresh groups."""
    with _state_lock, _FileLock():
        seen_state = _json_load(SEEN_PATH, None)
        groups = _json_load(GROUPS_PATH, [])
        keys = [signal_key(signal) for signal in signals]
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if not isinstance(seen_state, dict) or seen_state.get("version") != VERSION:
            seen_state = {"version": VERSION, "keys": sorted(set(keys)), "last_asof_ms": int(asof_ms)}
            _json_save(SEEN_PATH, seen_state)
            result = {"primed": len(keys), "new_groups": 0, "stale_skipped": 0, "gap_reprime": False}
        else:
            seen = set(seen_state.get("keys") or [])
            fresh = [(signal, key) for signal, key in zip(signals, keys) if key not in seen]
            previous = int(seen_state.get("last_asof_ms") or 0)
            gap = bool(previous and int(asof_ms) - previous > GAP_REPRIME_MIN * 60_000)
            stale = 0
            added = 0
            for signal, key in fresh:
                age_ms = int(asof_ms) - int(signal["bos_ms"])
                if gap or age_ms < 0 or age_ms > FRESH_MIN * 60_000:
                    stale += 1
                else:
                    group = build_group(signal, groups)
                    groups.append(group)
                    added += 1
                seen.add(key)
            seen_state.update(keys=sorted(seen), last_asof_ms=int(asof_ms))
            _json_save(SEEN_PATH, seen_state)
            if added:
                _json_save(GROUPS_PATH, groups)
            result = {"primed": 0, "new_groups": added, "stale_skipped": stale, "gap_reprime": gap}

        _json_save(TRACE_PATH, trace)
        status = dict(detector_status or {})
        status.update(result)
        status.update({
            "ok": True, "enabled": ENABLED, "execution_capable": False,
            "strategy_version": VERSION, "asof_ms": int(asof_ms), "updated_at": now_iso,
            "groups_total": len(groups), "shadow_groups": sum(g.get("decision") == "shadow" for g in groups),
            "blocked_groups": sum(g.get("decision") == "blocked" for g in groups),
        })
        _json_save(STATUS_PATH, status)
        return status


def scan_sync(asof_ms: Optional[int] = None, input_path: Optional[str] = None) -> Dict[str, Any]:
    """Run one isolated detector snapshot.  Intended for the background worker and tests."""
    source = input_path or BUFFER_PATH
    if not os.path.exists(source):
        return {"ok": False, "error": "buffer_missing", "execution_capable": False}
    if asof_ms is None:
        try:
            with open(source, newline="", encoding="utf-8") as handle:
                last = None
                for last in csv.DictReader(handle):
                    pass
            asof_ms = _parse_ms((last or {}).get("ts_event")) + 60_000
        except Exception:
            asof_ms = int(time.time() * 1000)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="m15_shadow_") as work:
        signals_path = os.path.join(work, "signals.json")
        trace_path = os.path.join(work, "trace.json")
        status_path = os.path.join(work, "status.json")
        command = [
            sys.executable, os.path.join(HERE, "m15_hybrid_detector.py"),
            "--input", source, "--signals-out", signals_path, "--trace-out", trace_path,
            "--status-out", status_path, "--work-dir", work,
            "--tail-rows", os.environ.get("M15_SHADOW_TAIL", "28000"),
        ]
        try:
            run = subprocess.run(command, capture_output=True, timeout=180)
            if run.returncode != 0:
                raise RuntimeError((run.stderr or b"").decode("utf-8", "replace")[-1500:])
            signals = _json_load(signals_path, [])
            trace = _json_load(trace_path, [])
            detector_status = _json_load(status_path, {})
            detector_status["scan_seconds"] = round(time.time() - started, 3)
            return process_scan_result(signals, trace, detector_status, int(asof_ms))
        except Exception as exc:
            status = _json_load(STATUS_PATH, {})
            status.update(ok=False, error="%s: %s" % (type(exc).__name__, exc),
                          execution_capable=False, updated_at=dt.datetime.now(dt.timezone.utc).isoformat())
            _json_save(STATUS_PATH, status)
            return status


def _worker() -> None:
    global _worker_running, _pending_asof_ms
    while True:
        with _worker_lock:
            target = _pending_asof_ms
            _pending_asof_ms = None
            if target is None:
                _worker_running = False
                return
        result = scan_sync(target)
        print("[m15-shadow] scan", result, flush=True)


def schedule_scan(asof_ms: int) -> bool:
    global _worker_running, _pending_asof_ms
    if not ENABLED:
        return False
    with _worker_lock:
        _pending_asof_ms = max(int(asof_ms), int(_pending_asof_ms or 0))
        if _worker_running:
            return False
        _worker_running = True
        threading.Thread(target=_worker, daemon=True, name="m15-shadow-scan").start()
        return True


def on_bar(bar: Dict[str, Any]) -> Dict[str, Any]:
    """Cheap hook for agent.py: score M1 now, scan only at a newly closed M5."""
    if not ENABLED:
        return {"enabled": False, "scheduled": False}
    changed = advance_bar(bar)
    try:
        bar_ms = _parse_ms(bar.get("ts_event"))
        close_ms = bar_ms + 60_000
        closes_m5 = (close_ms // 60_000) % 5 == 0
        scheduled = schedule_scan(close_ms) if closes_m5 else False
    except Exception:
        closes_m5 = scheduled = False
    return {"enabled": True, "advanced": changed, "m5_closed": closes_m5, "scheduled": scheduled}


def _shadow_payload() -> Dict[str, Any]:
    groups = _json_load(GROUPS_PATH, [])
    shadow = [g for g in groups if g.get("decision") == "shadow"]
    resolved = [g for g in shadow if g.get("filled") and g.get("status") not in ("pending", "open")]
    values = [float(g.get("net") or 0.0) for g in resolved]
    wins = sum(v > 0 for v in values)
    losses = sum(v < 0 for v in values)
    weeks = {g.get("week") for g in resolved if g.get("week")}
    research = _json_load(BACKTEST_PATH, {})
    all_period = research.get("all_period") or {}
    oos = research.get("oos_2024_through_2026_04") or {}
    return {
        "groups": groups, "shadow_groups": shadow,
        "summary": {
            "candidates": len(groups), "admitted": len(shadow), "blocked": len(groups) - len(shadow),
            "pending": sum(g.get("status") in ("pending", "open") for g in shadow),
            "filled_groups": len(resolved), "wins": wins, "losses": losses,
            "net": round(sum(values), 2),
            "expectancy_usd": round(sum(values) / len(values), 2) if values else None,
            "per_week": round(len(resolved) / len(weeks), 2) if weeks else None,
        },
        "status": _json_load(STATUS_PATH, {"ok": False, "execution_capable": False}),
        "reference": {
            "period": "2019-06 through 2026-04", "profile": "all_sessions + daily group guard",
            "eligible_candidates_per_month": all_period.get("eligible_candidates_per_month", 23.675),
            "filled_groups_per_month": all_period.get("filled_groups_per_month", 10.145),
            "oos_filled_groups_per_month": oos.get("filled_groups_per_month", 10.821),
            "profit_factor": all_period.get("profit_factor", 3.601),
            "note": "Clean ORPH-disabled replay: every confirmation is M5. Research reference only.",
        },
    }


def _pine_string(value: Any) -> str:
    """Escape one Python value for a Pine string literal."""
    return (str(value or "")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "")
            .replace("\n", "\\n"))


def _pine_float(value: Any) -> str:
    try:
        number = float(value)
        if not math.isfinite(number):
            return "na"
    except Exception:
        return "na"
    rendered = ("%.8f" % number).rstrip("0").rstrip(".")
    return rendered if "." in rendered else rendered + ".0"


def _leg_exit_price(group: Dict[str, Any], leg: Dict[str, Any]) -> Optional[float]:
    """Return stored exit price, with a backward-compatible legacy reconstruction."""
    try:
        if leg.get("exit_price") is not None:
            return round(float(leg["exit_price"]), 2)
        if not leg.get("filled") or leg.get("gross_r") is None:
            return None
        entry, stop = float(leg["entry"]), float(leg["sl"])
        signed = 1.0 if group.get("dir") == "LONG" else -1.0
        return round(entry + signed * float(leg["gross_r"]) * abs(entry - stop), 2)
    except Exception:
        return None


def build_shadow_pine(groups: Iterable[Dict[str, Any]], exported_at_ms: Optional[int] = None,
                      limit: int = 40, group_key: Optional[str] = None) -> str:
    """Build a visual-only Pine v6 snapshot from the real forward shadow ledger.

    TradingView Pine cannot fetch the Flask endpoint, so the generated source embeds a
    bounded snapshot.  Reopening the generator produces a fresh script.
    """
    exported_at_ms = int(exported_at_ms or time.time() * 1000)
    rows = [g for g in groups if g.get("decision") == "shadow"]
    rows.sort(key=lambda g: int(g.get("bos_ms") or 0))
    if group_key:
        rows = [g for g in rows if str(g.get("key")) == str(group_key)]
    else:
        rows = rows[-max(1, min(40, int(limit or 40))):]

    all_times = [exported_at_ms]
    all_times.extend(int(g.get("bos_ms") or exported_at_ms) for g in rows)
    for group in rows:
        all_times.extend(int(leg.get("exit_ms") or 0) for leg in group.get("legs") or [])
    positive_times = [value for value in all_times if value > 0]
    first_ms = (min(int(g.get("bos_ms") or exported_at_ms) for g in rows)
                if rows else exported_at_ms - 31 * 86_400_000)
    last_ms = max(positive_times) if positive_times else exported_at_ms
    from_ms = first_ms - 6 * 60 * 60 * 1000
    to_ms = max(last_ms, exported_at_ms) + 24 * 60 * 60 * 1000

    resolved = [g for g in rows if g.get("filled") and g.get("status") not in ("pending", "open")]
    total_net = round(sum(float(g.get("net") or 0.0) for g in resolved), 2)
    wins = sum(float(g.get("net") or 0.0) > 0 for g in resolved)
    losses = sum(float(g.get("net") or 0.0) < 0 for g in resolved)
    exported_iso = dt.datetime.fromtimestamp(
        exported_at_ms / 1000.0, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    calls: List[str] = []
    for group_id, group in enumerate(rows, 1):
        legs = group.get("legs") or []
        for leg_index, leg in enumerate(legs):
            calls.append(
                "    shownLegs += drawLeg(%d, %s, \"%s\", \"%s\", %s, %s, "
                "\"%s\", \"%s\", %d, %d, %d, %s, %s, %s, %s, %s, %d)" % (
                    group_id,
                    "true" if group.get("dir") == "LONG" else "false",
                    _pine_string(group.get("cat") or group.get("model") or "setup"),
                    _pine_string(group.get("status") or "pending"),
                    _pine_float(group.get("net")),
                    "true" if leg_index == 0 else "false",
                    _pine_string(leg.get("leg") or "leg"),
                    _pine_string(leg.get("status") or "waiting"),
                    int(group.get("bos_ms") or 0),
                    int(leg.get("fill_ms") or 0),
                    int(leg.get("exit_ms") or 0),
                    _pine_float(leg.get("entry")),
                    _pine_float(leg.get("sl")),
                    _pine_float(leg.get("tp")),
                    _pine_float(leg.get("net")),
                    _pine_float(_leg_exit_price(group, leg)),
                    int(leg.get("qty") or 0),
                )
            )
    if not calls:
        calls.append("    // No admitted forward-shadow groups were stored when this snapshot was exported.")

    group_scope = "one selected group" if group_key else "the latest %d admitted groups" % len(rows)
    source = """//@version=6
indicator("M15→M5 A/B + Shallow — current forward shadow", overlay=true, max_boxes_count=500, max_labels_count=500, max_lines_count=500)

// VISUAL-ONLY SERVER SNAPSHOT — no strategy orders, alerts or webhooks.
// Generated __EXPORTED__. It contains __SCOPE__ from m15_shadow_groups.json.
// Pine cannot fetch new server data. Reopen /m15/shadow/pine and copy again to refresh.

groupWindow = "Zakres eksportu"
fromTime = input.time(__FROM__, "Od", group=groupWindow)
toTime   = input.time(__TO__, "Do", group=groupWindow)

groupLayers = "Warstwy"
showDeep       = input.bool(true, "Deep", group=groupLayers)
showShallow    = input.bool(true, "Shallow", group=groupLayers)
showRiskReward = input.bool(true, "Strefy risk/reward po fill", group=groupLayers)
showMarkers    = input.bool(true, "BOS, fill i exit", group=groupLayers)
showNoFill     = input.bool(true, "Limity bez fill", group=groupLayers)

color longColor    = color.rgb(16, 185, 129)
color shortColor   = color.rgb(239, 68, 68)
color deepColor    = color.rgb(148, 163, 184)
color shallowColor = color.rgb(249, 115, 22)
color stopColor    = color.rgb(248, 113, 113)
color targetColor  = color.rgb(74, 222, 128)
color bosColor     = color.rgb(34, 211, 238)
color neutralColor = color.rgb(96, 165, 250)

priceText(float value) =>
    str.tostring(value, format.mintick)

pnlText(float value) =>
    na(value) ? "—" : (value >= 0 ? "+$" : "-$") + str.tostring(math.abs(value), "#.00")

drawLeg(int groupId, bool isLong, string catalyst, string groupStatus, float groupNet,
        bool firstLeg, string legName, string legStatus, int bosT, int fillT, int exitT,
        float entryPrice, float stopPrice, float targetPrice, float legNet,
        float exitPrice, int qty) =>
    int included = 0
    bool selectedLeg = (legName == "deep" and showDeep) or (legName == "shallow" and showShallow)
    bool selectedTime = bosT >= fromTime and bosT < toTime
    bool shouldDraw = selectedLeg and selectedTime and (legStatus != "no_fill" or showNoFill)
    if shouldDraw
        included := 1
        color sideColor = isLong ? longColor : shortColor
        color legColor = legName == "deep" ? deepColor : shallowColor
        int resolvedT = exitT > 0 ? exitT : timenow
        int orderRightT = fillT > 0 ? fillT : resolvedT
        string sideText = isLong ? "LONG" : "SHORT"

        line.new(x1=bosT, y1=entryPrice, x2=orderRightT, y2=entryPrice, xloc=xloc.bar_time,
          color=legColor, style=line.style_dashed, width=2)
        if fillT == 0
            line.new(x1=bosT, y1=stopPrice, x2=orderRightT, y2=stopPrice, xloc=xloc.bar_time,
              color=color.new(stopColor, 35), style=line.style_dotted, width=1)
            line.new(x1=bosT, y1=targetPrice, x2=orderRightT, y2=targetPrice, xloc=xloc.bar_time,
              color=color.new(targetColor, 35), style=line.style_dotted, width=1)

        if firstLeg and showMarkers
            label.new(x=bosT, y=entryPrice,
              text="#" + str.tostring(groupId) + " " + catalyst + " " + sideText +
                   "\nGROUP " + groupStatus + " " + pnlText(groupNet) + "\nBOS M5",
              xloc=xloc.bar_time, yloc=yloc.price,
              style=isLong ? label.style_label_up : label.style_label_down,
              color=color.new(sideColor, 5), textcolor=color.white, size=size.small)

        if fillT > 0
            line.new(x1=fillT, y1=entryPrice, x2=resolvedT, y2=entryPrice, xloc=xloc.bar_time,
              color=legColor, width=2)
            line.new(x1=fillT, y1=stopPrice, x2=resolvedT, y2=stopPrice, xloc=xloc.bar_time,
              color=stopColor, width=2)
            line.new(x1=fillT, y1=targetPrice, x2=resolvedT, y2=targetPrice, xloc=xloc.bar_time,
              color=targetColor, width=2)
            if showRiskReward
                box.new(left=fillT, top=math.max(entryPrice, stopPrice), right=resolvedT,
                  bottom=math.min(entryPrice, stopPrice), xloc=xloc.bar_time,
                  border_color=color.new(stopColor, 45), bgcolor=color.new(stopColor, 88))
                box.new(left=fillT, top=math.max(entryPrice, targetPrice), right=resolvedT,
                  bottom=math.min(entryPrice, targetPrice), xloc=xloc.bar_time,
                  border_color=color.new(targetColor, 45), bgcolor=color.new(targetColor, 90))
            if showMarkers
                label.new(x=fillT, y=entryPrice,
                  text="FILL " + legName + " ×" + str.tostring(qty), xloc=xloc.bar_time,
                  yloc=yloc.price, style=isLong ? label.style_triangleup : label.style_triangledown,
                  color=legColor, textcolor=legColor, size=size.tiny)
                if exitT > 0
                    float exitY = na(exitPrice) ? entryPrice : exitPrice
                    color resultColor = na(legNet) ? neutralColor : legNet > 0 ? targetColor : legNet < 0 ? stopColor : neutralColor
                    label.new(x=exitT, y=exitY,
                      text="EXIT " + legName + " " + legStatus + " " + pnlText(legNet),
                      xloc=xloc.bar_time, yloc=yloc.price, style=label.style_label_left,
                      color=color.new(resultColor, 5), textcolor=color.black, size=size.tiny)
                else
                    label.new(x=resolvedT, y=entryPrice, text="OPEN " + legName,
                      xloc=xloc.bar_time, yloc=yloc.price, style=label.style_label_left,
                      color=color.new(neutralColor, 5), textcolor=color.black, size=size.tiny)
        else if showMarkers
            color waitColor = legStatus == "no_fill" ? neutralColor : bosColor
            label.new(x=orderRightT, y=entryPrice, text=legName + " " + legStatus,
              xloc=xloc.bar_time, yloc=yloc.price, style=label.style_label_left,
              color=color.new(waitColor, 8), textcolor=color.black, size=size.tiny)
    included

var bool drawn = false
var int shownLegs = 0
if barstate.islast and not drawn
__CALLS__
    drawn := true

var table summary = table.new(position.top_right, 2, 7, bgcolor=color.new(color.black, 12), frame_color=color.new(color.silver, 35), frame_width=1)
if barstate.islast
    bool fiveMinuteChart = timeframe.isminutes and timeframe.multiplier == 5
    table.cell(summary, 0, 0, "M15→M5 SHADOW", text_color=color.white, bgcolor=color.new(color.blue, 45))
    table.cell(summary, 1, 0, "snapshot", text_color=color.white, bgcolor=color.new(color.blue, 45))
    table.cell(summary, 0, 1, "Grupy", text_color=color.silver)
    table.cell(summary, 1, 1, "__GROUPS__", text_color=color.white)
    table.cell(summary, 0, 2, "Widoczne nogi", text_color=color.silver)
    table.cell(summary, 1, 2, str.tostring(shownLegs), text_color=color.white)
    table.cell(summary, 0, 3, "W / L", text_color=color.silver)
    table.cell(summary, 1, 3, "__WINS__ / __LOSSES__", text_color=color.white)
    table.cell(summary, 0, 4, "Net zakończonych", text_color=color.silver)
    table.cell(summary, 1, 4, "__NET__", text_color=__NET_COLOR__)
    table.cell(summary, 0, 5, "Eksport", text_color=color.silver)
    table.cell(summary, 1, 5, "__EXPORTED__", text_color=color.white)
    table.cell(summary, 0, 6, "Wykres", text_color=color.silver)
    table.cell(summary, 1, 6, fiveMinuteChart ? "M5 ✓" : "Ustaw M5", text_color=fiveMinuteChart ? targetColor : color.rgb(245, 158, 11))
"""
    net_label = ("+$%.2f" % total_net) if total_net >= 0 else ("-$%.2f" % abs(total_net))
    return (source.replace("__EXPORTED__", exported_iso)
            .replace("__SCOPE__", group_scope)
            .replace("__FROM__", str(from_ms))
            .replace("__TO__", str(to_ms))
            .replace("__CALLS__", "\n".join(calls))
            .replace("__GROUPS__", str(len(rows)))
            .replace("__WINS__", str(wins))
            .replace("__LOSSES__", str(losses))
            .replace("__NET__", net_label)
            .replace("__NET_COLOR__", "targetColor" if total_net >= 0 else "stopColor"))


STYLE = """
*{box-sizing:border-box}body{margin:0;background:#0b0e14;color:#e6e9ef;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
h2{margin:0 0 4px}.mut{color:#8a94a6}.note,.card{background:#111827;border:1px solid #263044;border-radius:10px;padding:12px 14px;margin:10px 0}.safe{border-left:3px solid #4ade80}
.kpis{display:flex;gap:9px;flex-wrap:wrap}.kpi{min-width:145px;background:#141a28;border:1px solid #263044;border-radius:10px;padding:10px 13px}.kl{font-size:11px;color:#8a94a6}.kv{font-size:20px;font-weight:750}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #263044;white-space:nowrap}th{color:#8a94a6;position:sticky;top:0;background:#0b0e14}.wrap{overflow:auto;max-height:62vh;border:1px solid #263044;border-radius:10px}
.ok{color:#4ade80}.bad{color:#f87171}.wait{color:#fbbf24}.pill{padding:2px 7px;border:1px solid #334155;border-radius:12px;font-size:11px}button,.btn{display:inline-block;background:#14321f;color:#4ade80;border:1px solid #277642;border-radius:7px;padding:7px 11px;cursor:pointer;text-decoration:none}textarea{width:100%;height:62vh;background:#080b10;color:#d6deeb;border:1px solid #263044;border-radius:8px;padding:12px;font:12px/1.45 ui-monospace,monospace}img{max-width:100%;border:1px solid #263044;border-radius:10px;margin:8px 0}
"""


CANDIDATES_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><style>__STYLE__</style></head><body>
<h2>M15 → M5 candidates</h2><div class='mut'>M15 trigger / displacement / FVG / 90% hold; confirmation only by a closed M5 candle.</div>
<div class='note safe'><b>SHADOW ONLY.</b> This module has no broker, webhook or execution import. First scan primes history; only later fresh M5 confirmations enter the forward book.</div>
<div class=kpis id=k></div><div class=wrap><table id=t></table></div><script>
function esc(x){return String(x==null?'—':x).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
fetch('/m15/candidates/data?days=31&t='+Date.now(),{cache:'no-store'}).then(r=>r.json()).then(d=>{let s=d.status||{},r=d.rows||[];
document.getElementById('k').innerHTML=`<div class=kpi><div class=kl>recent rows</div><div class=kv>${r.length}</div></div><div class=kpi><div class=kl>canonical signals in buffer</div><div class=kv>${s.canonical_signals||0}</div></div><div class=kpi><div class=kl>forward groups</div><div class=kv>${s.shadow_groups||0}</div></div><div class=kpi><div class=kl>execution</div><div class='kv ok'>OFF</div></div>`;
let rows=r.map(x=>`<tr><td>${esc(x.trig)}</td><td>${esc(x.cat)}</td><td>${esc(x.dir)}</td><td><span class=pill>${esc(x.stage)}</span></td><td>${esc(x.fvg&&x.fvg.join('–'))}</td><td>${esc(x.bos)}</td><td>${esc(x.entry)}</td><td>${esc(x.shallow_entry)}</td><td>${esc(x.shadow_decision)}</td></tr>`).join('');
document.getElementById('t').innerHTML='<tr><th>trigger TFO UTC−4</th><th>catalyst</th><th>dir</th><th>stage</th><th>FVG M15</th><th>BOS M5</th><th>deep</th><th>shallow</th><th>shadow decision</th></tr>'+(rows||'<tr><td colspan=9 class=mut>No recent candidates.</td></tr>');});
</script></body></html>""".replace("__STYLE__", STYLE)


SHADOW_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><style>__STYLE__</style></head><body>
<h2>M15 → M5 forward shadow</h2><div class=mut>Two sibling limits, one $500 setup group; $250 budget per leg. 20 following M1 bars (normally ~20 minutes), adverse-first, EOD 15:55 ET.</div>
<div class='note safe'><b>Zero real orders.</b> Results begin only after the first priming scan and are kept separate from April historical examples.</div>
<div class=card><b>Zobacz aktualne shadow trades na TradingView:</b> <a class=btn href='/m15/shadow/pine'>Pine — wszystkie ostatnie trady</a> <span class=mut>Generator tworzy aktualny snapshot; po nowych tradach skopiuj go ponownie.</span></div>
<div class=kpis id=k></div><div class=card id=ref></div><div class=wrap><table id=t></table></div><script>
function m(v){return v==null?'—':(v>=0?'+$':'-$')+Math.abs(v).toFixed(2)}function e(x){return String(x==null?'—':x)}
fetch('/m15/shadow/data?t='+Date.now(),{cache:'no-store'}).then(r=>r.json()).then(d=>{let s=d.summary||{},q=d.reference||{};
document.getElementById('k').innerHTML=`<div class=kpi><div class=kl>admitted groups</div><div class=kv>${s.admitted||0}</div></div><div class=kpi><div class=kl>filled / resolved</div><div class=kv>${s.filled_groups||0}</div></div><div class=kpi><div class=kl>W / L</div><div class=kv>${s.wins||0} / ${s.losses||0}</div></div><div class=kpi><div class=kl>net</div><div class=kv>${m(s.net||0)}</div></div><div class=kpi><div class=kl>execution</div><div class='kv ok'>OFF</div></div>`;
document.getElementById('ref').innerHTML=`<b>Historical reference:</b> ${q.filled_groups_per_month} filled guarded groups/month (${q.oos_filled_groups_per_month} OOS); candidate supply ${q.eligible_candidates_per_month}/month. <span class=mut>${q.note}</span>`;
let rows=[];(d.groups||[]).slice().reverse().forEach(g=>(g.legs||[]).forEach(l=>{let p=g.decision==='shadow'?`<a class=btn href='/m15/shadow/pine?group=${encodeURIComponent(g.key)}'>Pine</a>`:'—';rows.push(`<tr><td>${e(g.et)}</td><td>${e(g.cat)}</td><td>${e(g.dir)}</td><td>${e(l.leg)}</td><td>${e(l.entry)}</td><td>${e(l.sl)}</td><td>${e(l.tp)}</td><td>${e(g.decision==='shadow'?l.status:g.block_reason)}</td><td>${m(l.net)}</td><td>${e(g.status)}</td><td>${p}</td></tr>`)}));
document.getElementById('t').innerHTML='<tr><th>BOS ET</th><th>catalyst</th><th>dir</th><th>leg</th><th>entry</th><th>SL</th><th>TP</th><th>leg result</th><th>net</th><th>group</th><th>chart</th></tr>'+(rows.join('')||'<tr><td colspan=11 class=mut>Forward book is empty until a new signal appears after priming.</td></tr>');});
</script></body></html>""".replace("__STYLE__", STYLE)


HOW_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><style>__STYLE__</style></head><body>
<h2>A/B + Shallow M15 → M5 · v1</h2>
<div class='note safe'><b>Status: research shadow only.</b> No alert and no order route. Minimum promotion gate: at least 30 filled forward groups and two weeks, then review expectancy, drawdown and parity before any live proposal.</div>
<div class=card><b>Signal:</b> catalyst → same-colour M15 displacement (≥3 bars) → FVG → M15 rejection holding 90% of the gap → closed M5 candle through structure.</div>
<div class=card><table><tr><th>Setting</th><th>v1</th><th>Meaning</th></tr>
<tr><td>DISPWIN</td><td>4 M15</td><td>about 1 hour, not four minutes</td></tr><tr><td>ATRMULT</td><td>0.75</td><td>relaxed displacement strength</td></tr><tr><td>LOOKBACK / TOL</td><td>6 / 1 pt</td><td>M15 structure and FVG tolerance</td></tr><tr><td>REJ_FRAC / RETWIN</td><td>0.90 / 20 M15</td><td>body hold and retrace life</td></tr><tr><td>M5 BOSWIN</td><td>60 bars</td><td>up to 5 hours</td></tr><tr><td>Entries</td><td>deep + 25% shallow</td><td>same SL; shallow TP fixed 2R</td></tr><tr><td>Fill</td><td>20 M1 bars</td><td>normally ~20 min; one-tick-through; no fill-bar TP</td></tr><tr><td>Risk</td><td>$500/group</td><td>$250 per sibling; MNQ point value $2</td></tr></table></div>
<div class=card><b>Clean implementation-parity reference:</b> 10.15 filled guarded groups/month over Jun 2019–Apr 2026; 10.82/month OOS (2024–Apr 2026). ORPH is disabled in both this replay and v1, so every confirmation is genuinely M5; forward shadow remains the decision source.</div>
<div class=card><b>Typical timing:</b> median M5 BOS → first fill 4 min; median fill → exit 3h48. The middle 50% of filled groups lasted about 1h45–6h01, while 10% lasted longer than roughly 9h37.</div>
</body></html>""".replace("__STYLE__", STYLE)


def _candidate_payload(days: float) -> Dict[str, Any]:
    trace = _json_load(TRACE_PATH, [])
    groups = _json_load(GROUPS_PATH, [])
    status = _json_load(STATUS_PATH, {"execution_capable": False})
    decisions = {g.get("key"): g for g in groups}
    # Anchor "last 31 days" to the latest market data rather than wall-clock time.
    # This keeps a downloaded/paused buffer inspectable (the supplied file ends in 2026).
    trace_anchor = max([int(row.get("trig_ms") or 0) for row in trace], default=0)
    anchor = int(status.get("asof_ms") or trace_anchor or int(time.time() * 1000))
    cutoff = int(anchor - max(1.0, days) * 86_400_000)
    rows = []
    for raw in trace:
        if int(raw.get("trig_ms") or 0) < cutoff:
            continue
        row = dict(raw)
        if row.get("stage") == "POTWIERDZONY" and row.get("entry") is not None:
            pseudo = {"bos_ms": row.get("bos_ms"), "dir": row.get("dir"),
                      "entry": row.get("entry"), "SL": row.get("SL")}
            key = signal_key(pseudo)
            close, deep, stop = float(row.get("signal_close")), float(row["entry"]), float(row["SL"])
            shallow = _tick(close + SETTINGS.shallow_fraction * (deep - close))
            row["shallow_entry"] = shallow
            row["shallow_tp"] = _tick(shallow + (2 if row.get("dir") == "LONG" else -2) * abs(shallow - stop))
            decision = decisions.get(key)
            row["shadow_decision"] = (decision or {}).get("decision")
            row["shadow_block_reason"] = (decision or {}).get("block_reason")
        rows.append(row)
    rows.sort(key=lambda x: int(x.get("trig_ms") or 0), reverse=True)
    return {"rows": rows, "status": status}


def register(app: Any) -> Any:
    try:
        from flask import Response, jsonify, request, send_from_directory
    except Exception:
        return app

    def candidates_page():
        return Response(CANDIDATES_PAGE, mimetype="text/html")

    def candidates_data():
        try:
            days = float(request.args.get("days", "31"))
        except Exception:
            days = 31.0
        response = jsonify(_candidate_payload(days))
        response.headers["Cache-Control"] = "no-store"
        return response

    def shadow_page():
        return Response(SHADOW_PAGE, mimetype="text/html")

    def shadow_data():
        response = jsonify(_shadow_payload())
        response.headers["Cache-Control"] = "no-store"
        return response

    def shadow_pine():
        try:
            limit = max(1, min(40, int(request.args.get("limit", "40"))))
        except Exception:
            limit = 40
        selected_key = request.args.get("group") or None
        # Take one coherent ledger snapshot; release the cross-worker lock before
        # rendering the comparatively large Pine source.
        with _state_lock, _FileLock():
            groups = _json_load(GROUPS_PATH, [])
        script = build_shadow_pine(groups, limit=limit, group_key=selected_key)
        if request.args.get("raw") == "1" or request.args.get("download") == "1":
            response = Response(script, mimetype="text/plain")
            response.headers["Cache-Control"] = "no-store"
            if request.args.get("download") == "1":
                response.headers["Content-Disposition"] = (
                    "attachment; filename=M15_M5_current_shadow.pine"
                )
            return response
        query = {"raw": "1", "limit": str(limit)}
        download_query = {"download": "1", "limit": str(limit)}
        if selected_key:
            query["group"] = selected_key
            download_query["group"] = selected_key
        raw_href = "/m15/shadow/pine?" + urlencode(query)
        download_href = "/m15/shadow/pine?" + urlencode(download_query)
        safe = html.escape(script)
        admitted = [g for g in groups if g.get("decision") == "shadow"]
        shown = sum(str(g.get("key")) == str(selected_key) for g in admitted) if selected_key else min(limit, len(admitted))
        page = (
            "<!doctype html><html><head><meta charset=utf-8><style>%s</style></head><body>"
            "<h2>Pine v6 · aktualne M15→M5 shadow trades</h2>"
            "<div class='note safe'><b>Snapshot forward shadow: %d grup.</b> Skrypt pokazuje BOS M5, "
            "limit entry, SL, TP, fill, exit oraz P&amp;L. Jest wskaźnikiem wizualnym — bez alertów i zleceń. "
            "Po nowych tradach odśwież tę stronę i skopiuj skrypt ponownie.</div>"
            "<button onclick=\"navigator.clipboard.writeText(document.getElementById('p').value);this.textContent='Skopiowano ✓'\">Kopiuj Pine</button> "
            "<a class=btn href='%s'>surowy tekst</a> <a class=btn href='%s'>pobierz plik</a> "
            "<a class=btn href='/m15/pine'>historyczne przykłady IV 2026</a>"
            "<textarea id=p>%s</textarea></body></html>" % (
                STYLE, shown, html.escape(raw_href, quote=True),
                html.escape(download_href, quote=True), safe,
            )
        )
        response = Response(page, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    def health():
        status = _json_load(STATUS_PATH, {})
        status.update(enabled=ENABLED, execution_capable=False, strategy_version=VERSION,
                      buffer=BUFFER_PATH, buffer_exists=os.path.exists(BUFFER_PATH))
        return jsonify(status)

    def how():
        return Response(HOW_PAGE, mimetype="text/html")

    def pine():
        try:
            text = Path(PINE_PATH).read_text(encoding="utf-8")
        except Exception:
            text = "Pine file is missing."
        if request.args.get("raw") == "1":
            return Response(text, mimetype="text/plain")
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        page = ("<!doctype html><html><head><meta charset=utf-8><style>%s</style></head><body>"
                "<h2>Pine v6 · April 2026 examples</h2><div class='note'>Frozen visual replay: 12 guarded groups from the last complete month in the supplied NQ data. Indicator only — no alerts or orders.</div>"
                "<button onclick=\"navigator.clipboard.writeText(document.getElementById('p').value);this.textContent='Copied ✓'\">Copy Pine</button>"
                " <a href='/m15/pine?raw=1' style='color:#60a5fa'>raw .txt</a><textarea id=p>%s</textarea></body></html>" % (STYLE, safe))
        return Response(page, mimetype="text/html")

    def examples():
        images = []
        if os.path.isdir(ASSET_DIR):
            # Browsers render the original 1600×900 SVGs exactly.  PNG thumbnails
            # remain a fallback for environments that cannot display SVG.
            names = sorted(os.listdir(ASSET_DIR))
            images = [name for name in names if name.lower().endswith(".svg")]
            if not images:
                images = [name for name in names if name.lower().endswith(".png")]
        try:
            md = Path(EXAMPLES_MD).read_text(encoding="utf-8")
        except Exception:
            md = "Examples file is missing."
        imgs = "".join("<img src='/m15/examples/assets/%s' alt='%s'>" % (name, name) for name in images)
        table = "<pre style='white-space:pre-wrap'>%s</pre>" % md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Response("<!doctype html><html><head><meta charset=utf-8><style>%s</style></head><body><h2>April 2026 graphical examples</h2>%s%s</body></html>" % (STYLE, imgs, table), mimetype="text/html")

    def example_asset(name: str):
        return send_from_directory(ASSET_DIR, name)

    app.add_url_rule("/m15/candidates", "m15_candidates", candidates_page)
    app.add_url_rule("/m15/candidates/data", "m15_candidates_data", candidates_data)
    app.add_url_rule("/m15/shadow", "m15_shadow", shadow_page)
    app.add_url_rule("/m15/shadow/data", "m15_shadow_data", shadow_data)
    app.add_url_rule("/m15/shadow/pine", "m15_shadow_pine", shadow_pine)
    app.add_url_rule("/m15/status", "m15_status", health)
    app.add_url_rule("/m15/how", "m15_how", how)
    app.add_url_rule("/m15/pine", "m15_pine", pine)
    app.add_url_rule("/m15/examples", "m15_examples", examples)
    app.add_url_rule("/m15/examples/assets/<path:name>", "m15_example_asset", example_asset)
    return app
