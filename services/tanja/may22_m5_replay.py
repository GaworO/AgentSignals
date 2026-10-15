"""Single-session, offline May 22 M5 momentum diagnostic. Never submits orders.

Research assumptions are named in ASSUMPTIONS. Reference prices are used only
after replay to compare the resulting plans; they cannot affect detection.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from vendor.tanya_replay import aggregate, fvg_events, load_bars

ET = ZoneInfo("America/New_York")
DAY = "2026-05-22"
ASSUMPTIONS = {
    "analysis_instrument": "MNQ M5 proxy for Tanja's NQ chart; roll/basis fidelity unverified",
    "swing": "most recent confirmed 2-left/2-right M5 pivot high, known before trigger bar opens",
    "break": "inversion M5 candle closes strictly above selected pivot high",
    "confirmation_strength": "not quantified; inversion and close break are the only measured confirmations",
    "entry": "next available M1 open after completed M5 confirmation, without intrabar foresight",
    "stop": "latest confirmed 2-left/2-right M5 pivot low minus one MNQ tick; experimental, not Tanja's RTH/PDA selection",
    "target": "highest observed MNQ high since supplied history begins, before confirmation; ATH proxy only",
    "replay_window": "09:30–11:00 New York; fixed for this one demonstrated setup",
}
REFERENCE = {"entry": 29598.75, "initial_stop": 29514.25,
             "later_stop": 29601.75, "target_region": 29780.0}


def local_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def pivot(rows, index: int, side: str) -> bool:
    """A two-sided pivot is available only after two subsequent bars close."""
    if index < 2 or index + 2 >= len(rows):
        return False
    value = getattr(rows[index], side)
    neighbors = [getattr(rows[j], side) for j in (*range(index - 2, index),
                                                   *range(index + 1, index + 3))]
    return value > max(neighbors) if side == "high" else value < min(neighbors)


def run(es_path: Path, mnq_path: Path) -> dict:
    es, mnq = load_bars(es_path), load_bars(mnq_path)
    session = [b for b in mnq if datetime.fromtimestamp(b.time, ET).date().isoformat() == DAY]
    es_session = [b for b in es if datetime.fromtimestamp(b.time, ET).date().isoformat() == DAY]
    if not session or not es_session:
        return {"status": "missing_data", "candidates": [], "assumptions": ASSUMPTIONS}
    first = next((b.time for b in session if datetime.fromtimestamp(b.time, ET).strftime("%H:%M") == "09:30"), None)
    last = next((b.time for b in session if datetime.fromtimestamp(b.time, ET).strftime("%H:%M") == "11:00"), None)
    if first is None or last is None:
        return {"status": "incomplete_window", "candidates": [], "assumptions": ASSUMPTIONS}
    cutoff = last + 60
    m5 = aggregate(session, 5, cutoff)
    by_start = {b.time: b for b in session}
    es_starts = {b.time for b in es_session}
    events = [e for e in fvg_events(session, 5, cutoff)
              if e["kind"] == "IFVG" and e["direction"] == "long"
              and first < e["available_at"] <= last]
    candidates = []
    for event in events:
        known_at = event["available_at"]
        trigger = next((b for b in m5 if b.time + 300 == known_at), None)
        if trigger is None:
            continue
        # Pivots and the target are computed afresh at each cutoff. No later
        # bar or historical screenshot is consulted to select an anchor.
        visible = [b for b in m5 if b.time + 300 <= trigger.time]
        highs = [b for i, b in enumerate(visible) if pivot(visible, i, "high")]
        lows = [b for i, b in enumerate(visible) if pivot(visible, i, "low")]
        selected = highs[-1] if highs else None
        stop_pivot = lows[-1] if lows else None
        previous = [b.high for b in mnq if b.time + 60 <= trigger.time]
        target = max(previous) if previous else None
        es_complete = all(t in es_starts for t in range(trigger.time, known_at, 60))
        reasons = []
        if not selected:
            reasons.append("no confirmed prior M5 pivot high")
        elif not trigger.close > selected.high:
            reasons.append("inversion close did not break selected swing high")
        if not stop_pivot:
            reasons.append("no confirmed prior M5 pivot low for experimental stop")
        if known_at not in by_start:
            reasons.append("no next M1 open for causal entry")
        if not es_complete:
            reasons.append("paired ES M1 data incomplete over trigger")
        if target is None:
            reasons.append("no prior high for target proxy")
        entry = by_start[known_at].open if known_at in by_start else None
        stop = stop_pivot.low - .25 if stop_pivot else None
        if entry is not None and stop is not None and stop >= entry:
            reasons.append("experimental stop is not below entry")
        if entry is not None and target is not None and target <= entry:
            reasons.append("available-history high is not above entry")
        candidates.append({
            "state": "rejected" if reasons else "accepted_experimental",
            "signal_time_ny": local_time(known_at),
            "trigger_m5_start_ny": local_time(trigger.time),
            "inversion_zone": {k: event[k] for k in ("zone_id", "lower", "upper")},
            "selected_swing": None if selected is None else {"time_ny": local_time(selected.time), "high": selected.high},
            "trigger_close": trigger.close,
            "entry_time_ny": local_time(known_at) if entry is not None else None,
            "entry": entry, "initial_stop": stop,
            "stop_pivot": None if stop_pivot is None else {"time_ny": local_time(stop_pivot.time), "low": stop_pivot.low},
            "target_proxy": target, "es_paired_data": es_complete,
            "reasons": reasons or ["closed M5 inversion and selected pivot break; experimental plan anchors available"],
        })
    return {"status": "offline_complete", "session": DAY,
            "source": {"ES": str(es_path), "MNQ": str(mnq_path)},
            "window_ny": [local_time(first), local_time(last)],
            "assumptions": ASSUMPTIONS, "reference_observations_not_used_in_detection": REFERENCE,
            "candidates": candidates,
            "comparison": [{"signal_time_ny": c["signal_time_ny"],
                            "entry_delta": None if c["entry"] is None else round(c["entry"] - REFERENCE["entry"], 2),
                            "stop_delta": None if c["initial_stop"] is None else round(c["initial_stop"] - REFERENCE["initial_stop"], 2),
                            "target_delta": None if c["target_proxy"] is None else round(c["target_proxy"] - REFERENCE["target_region"], 2),
                            "swing_match": "unverifiable: screenshot does not identify exact pivot candle",
                            "inversion_match": "requires candle-level annotation, not playback timestamp"}
                           for c in candidates],
            "orders_sent": 0, "broker_paths_invoked": False,
            "later_stop_replayed": False,
            "limitations": ["The 29,601.75 stop is a later observed modification, not an entry rule.",
                            "The supplied history begins in May 2026, so the target proxy is not a verified all-time high.",
                            "No fill, slippage, partial or final outcome is inferred from screenshots."]}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    data = root / "research_tanya_may_replay_20261007" / "data"
    report = run(data / "es_bars.json", data / "mnq_bars.json")
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("may22_m5_diagnostic.json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "candidates": len(report["candidates"]),
                      "accepted": sum(c["state"] == "accepted_experimental" for c in report["candidates"]),
                      "diagnostic": str(output)}))
