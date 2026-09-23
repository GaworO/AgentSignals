#!/usr/bin/env python3
"""Visualize existing archive-49 ledger only; no signal or P&L replay."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import continuation_shadow as shadow  # noqa: E402


def millis(value: str) -> int:
    return int(dt.datetime.fromisoformat(value).timestamp() * 1000)


def main() -> None:
    source = HERE / "closed_trades.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    # Identical timestamps and execution geometry overlap exactly on a chart.
    # Collapse visual markers only; the underlying trade ledger is untouched.
    unique = {}
    for row in rows:
        key = (row["direction"], row["fill_timestamp"], row["entry"], row["sl"], row["dol_target"])
        unique.setdefault(key, row)
    trades = [
        {
            "direction": row["direction"], "order_id": row["order_id"], "state": "CLOSED",
            "fill_ms": millis(row["fill_timestamp"]), "exit_ms": millis(row["exit_timestamp"]),
            "entry_price": row["entry"], "stop_price": row["sl"], "target_price": row["dol_target"],
            "exit_price": row["exit_price"], "exit_reason": row["exit_reason"], "net_r": row["net_r"],
        }
        for row in sorted(unique.values(), key=lambda x: (x["fill_timestamp"], x["order_id"]))
    ]
    pine = shadow._pine_forward_source(trades, None)
    pine = pine.replace("MNQ Continuation forward shadow fills", "MNQ Continuation archive49 diagnostic")
    pine = pine.replace(
        "// Display-only export of forward shadow ledger. No alerts, orders, or signal recalculation.",
        "// HISTORICAL DIAGNOSTIC: existing archive-49 replay, NOT live forward fills or independent validation.\n"
        f"// {len(rows)} order records shown as {len(trades)} distinct execution markers; no new P&L calculated.",
    )
    target = HERE / "archive49_historical_diagnostic_trades.pine"
    target.write_text(pine, encoding="utf-8")
    print(f"{target}: {len(rows)} ledger rows, {len(trades)} distinct chart markers")


if __name__ == "__main__":
    main()
