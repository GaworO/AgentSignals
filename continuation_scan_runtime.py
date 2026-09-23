#!/usr/bin/env python3
"""Isolated executable for the original, hashed Continuation research detector.

The production detector has its own older detcore package.  Import the frozen
research detcore first in this child process, so no production module or
Reversal state is replaced in the Flask worker.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "continuation_runtime"
if not (RUNTIME / "detcore" / "__init__.py").is_file():
    raise RuntimeError("isolated Continuation detcore is unavailable")
sys.path.insert(0, str(RUNTIME))
import detcore  # noqa: E402  -- pinned in this child process before freeze import

if Path(detcore.__file__).resolve().parent != (RUNTIME / "detcore").resolve():
    raise RuntimeError("Continuation imported the production detcore")

from MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE.source import freeze_baseline as freeze  # noqa: E402
import continuation_short_engine as short_engine  # noqa: E402


THESIS_MODES = {"strict", "allow_none"}


def thesis_mode() -> str:
    """Return the explicit runtime policy without weakening the safe default."""
    mode = os.environ.get("CONTINUATION_HTF_THESIS_MODE", "strict").strip().lower()
    if mode not in THESIS_MODES:
        raise RuntimeError(
            "CONTINUATION_HTF_THESIS_MODE must be strict or allow_none"
        )
    return mode


def directional_theses(theses: dict, side: str, mode: str) -> dict:
    """Build a direction-local view; never turn an opposite thesis into a pass."""
    if mode not in THESIS_MODES:
        raise ValueError(f"invalid thesis mode: {mode}")
    result = {}
    for day, source in theses.items():
        row = dict(source or {})
        original = str(row.get("thesis", "NONE")).upper()
        row["original_thesis"] = original
        row["original_reason"] = row.get("reason")
        row["override_applied"] = False
        if mode == "allow_none" and original == "NONE":
            row["thesis"] = side
            row["reason"] = "allow_none_override:" + str(row.get("reason") or "unspecified")
            row["override_applied"] = True
        result[day] = row
    return result


def annotate_policy(rows: list[dict], orders: list[dict], original_theses: dict,
                    side: str, mode: str) -> None:
    """Preserve the real thesis in ledgers while recording the effective policy."""
    by_candidate = {}
    for row in rows:
        day = row.get("trading_day")
        source = original_theses.get(day, {"thesis": "NONE", "reason": "missing_day"})
        original = str(source.get("thesis", "NONE")).upper()
        effective = str(row.get("jade_thesis", original)).upper()
        overridden = mode == "allow_none" and original == "NONE" and effective == side
        row["jade_thesis_original"] = original
        row["jade_thesis_effective"] = effective
        row["jade_thesis_mode"] = mode
        row["jade_thesis_override_applied"] = overridden
        if overridden:
            row["jade_thesis"] = original
            row["jade_thesis_reason"] = source.get("reason")
            row["eligibility_override_reason"] = "THESIS_NONE_ALLOWED"
        by_candidate[str(row.get("candidate_id"))] = row
    for order in orders:
        candidate = by_candidate.get(str(order.get("candidate_id")), {})
        order["jade_thesis_original"] = candidate.get("jade_thesis_original")
        order["jade_thesis_effective"] = candidate.get("jade_thesis_effective")
        order["jade_thesis_mode"] = mode
        order["jade_thesis_override_applied"] = bool(candidate.get("jade_thesis_override_applied"))


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--import-check":
        print(json.dumps({"engine": str(Path(freeze.__file__).resolve()),
                          "detcore": str(Path(detcore.__file__).resolve()),
                          "short_engine": str(Path(short_engine.__file__).resolve())}))
        return
    if len(sys.argv) != 2 or sys.argv[1] != "--scan":
        raise SystemExit("usage: continuation_scan_runtime.py --import-check|--scan")
    import continuation_shadow as shadow  # noqa: E402

    raw = shadow._load_history()
    if raw.empty:
        raise RuntimeError("history contains no valid bars")
    theses = freeze.jade_theses(raw)
    mode = thesis_mode()
    outputs, triggers, detector_meta = freeze.generate_detector(raw)
    freeze.detector_meta = detector_meta
    short_outputs, short_triggers, short_detector_meta = short_engine.generate_detector(raw)
    for trigger in triggers + short_triggers:
        day = freeze.trading_day_at(int(trigger["trigger_ms"]))
        thesis = theses.get(day, {"thesis": "NONE", "reason": "missing_day"})
        trigger["trading_day"] = day
        trigger["jade_thesis"] = thesis.get("thesis", "NONE")
        trigger["jade_thesis_reason"] = thesis.get("reason")
    candidates, orders, funnel = freeze.build_manifests(
        raw, outputs, triggers, directional_theses(theses, "LONG", mode))
    short_candidates, short_orders, short_funnel = short_engine.build_manifests(
        raw, short_outputs, directional_theses(theses, "SHORT", mode))
    annotate_policy(candidates, orders, theses, "LONG", mode)
    annotate_policy(short_candidates, short_orders, theses, "SHORT", mode)
    funnel["thesis_mode"] = mode
    funnel["neutral_thesis_overrides"] = sum(
        bool(x.get("jade_thesis_override_applied")) for x in candidates)
    short_funnel["thesis_mode"] = mode
    short_funnel["neutral_thesis_overrides"] = sum(
        bool(x.get("jade_thesis_override_applied")) for x in short_candidates)
    current_day = freeze.trading_day_at(int(raw.ts_event.iloc[-1].timestamp() * 1000))
    current_thesis = theses.get(current_day, {"thesis": "NONE", "reason": "missing_day"})
    print(json.dumps(shadow._safe({
        "thesis_mode": mode,
        "outputs": outputs, "triggers": triggers, "candidates": candidates,
        "orders": orders, "funnel": funnel, "current_thesis": current_thesis,
        "short_outputs": short_outputs, "short_triggers": short_triggers,
        "short_candidates": short_candidates, "short_orders": short_orders,
        "short_funnel": short_funnel, "short_detector_meta": short_detector_meta,
    }), sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
