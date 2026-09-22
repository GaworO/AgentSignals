#!/usr/bin/env python3
"""Isolated executable for the original, hashed Continuation research detector.

The production detector has its own older detcore package.  Import the frozen
research detcore first in this child process, so no production module or
Reversal state is replaced in the Flask worker.
"""
from __future__ import annotations

import json
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


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--import-check":
        print(json.dumps({"engine": str(Path(freeze.__file__).resolve()),
                          "detcore": str(Path(detcore.__file__).resolve())}))
        return
    if len(sys.argv) != 2 or sys.argv[1] != "--scan":
        raise SystemExit("usage: continuation_scan_runtime.py --import-check|--scan")
    import continuation_shadow as shadow  # noqa: E402

    raw = shadow._load_history()
    if raw.empty:
        raise RuntimeError("history contains no valid bars")
    theses = freeze.jade_theses(raw)
    outputs, triggers, detector_meta = freeze.generate_detector(raw)
    freeze.detector_meta = detector_meta
    for trigger in triggers:
        day = freeze.trading_day_at(int(trigger["trigger_ms"]))
        thesis = theses.get(day, {"thesis": "NONE", "reason": "missing_day"})
        trigger["trading_day"] = day
        trigger["jade_thesis"] = thesis.get("thesis", "NONE")
        trigger["jade_thesis_reason"] = thesis.get("reason")
    candidates, orders, funnel = freeze.build_manifests(raw, outputs, triggers, theses)
    current_day = freeze.trading_day_at(int(raw.ts_event.iloc[-1].timestamp() * 1000))
    current_thesis = theses.get(current_day, {"thesis": "NONE", "reason": "missing_day"})
    print(json.dumps(shadow._safe({
        "outputs": outputs, "triggers": triggers, "candidates": candidates,
        "orders": orders, "funnel": funnel, "current_thesis": current_thesis,
    }), sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
