#!/usr/bin/env python3
"""Outcome-free A/B Directional manifests built from the canonical chain.

This module deliberately applies no HTF-thesis or DOL eligibility gate.  It
only converts an already causal LONG/SHORT liquidity -> displacement/FVG ->
pullback/hold/BOS output into the predeclared fixed-2R resting order.
"""
from __future__ import annotations

import collections
from typing import Any

import numpy as np
import pandas as pd

from MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE.source import freeze_baseline as base


IDENTITY = "AB_DIRECTIONAL_CAUSAL_CHAIN_FIXED_2R_V1"
ORDER_EXPIRY_MS = 10 * 60_000


def build_manifests(raw: pd.DataFrame, outputs: list[dict[str, Any]], direction: str):
    """Create direction-symmetric fixed-2R candidates without outcome access."""
    side = str(direction).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    ms = raw.ts_event.astype("int64").to_numpy() // 1_000_000
    iid = raw.instrument_id.to_numpy(np.int64)
    touch = raw.low.to_numpy(float) if side == "LONG" else raw.high.to_numpy(float)
    candidates, orders = [], []
    seen_order_geometry: dict[tuple, str] = {}
    reasons = collections.Counter()
    for n, output in enumerate(outputs, 1):
        row = dict(output)
        decision_ms = int(row["entry_ms"])
        entry = base.tick(float(row["entry"]) + (1.0 if side == "LONG" else -1.0))
        stop = base.tick(float(row["SL"]))
        risk = round((entry - stop) if side == "LONG" else (stop - entry), 8)
        candidate_id = base.stable_id("ABDIR_" + side, row["instrument_id"], row["bos_ms"],
                                      row["cat"], row["fvg_bar"], row["entry"])
        row.update(candidate_id=candidate_id, chronological_index=n,
                   trading_day=base.trading_day_at(row["bos_ms"]),
                   strategy="AB_DIRECTIONAL", research_identity=IDENTITY,
                   eligible=False, rejection_reason=None)
        if risk <= 0:
            row["rejection_reason"] = "INVALID_FINAL_ENTRY_SL_GEOMETRY"
            reasons[row["rejection_reason"]] += 1
            candidates.append(row)
            continue
        target = base.tick(entry + 2.0 * risk) if side == "LONG" else base.tick(entry - 2.0 * risk)
        activation, expiry = decision_ms, decision_ms + ORDER_EXPIRY_MS
        geometry = (int(row["instrument_id"]), side, activation,
                    round(entry / base.TICK), round(stop / base.TICK))
        if geometry in seen_order_geometry:
            reasons["DUPLICATE_PHYSICAL_GEOMETRY"] += 1
            continue
        seen_order_geometry[geometry] = candidate_id
        left = int(np.searchsorted(ms, activation, side="left"))
        right = int(np.searchsorted(ms, expiry, side="left"))
        same = iid[left:right] == int(row["instrument_id"])
        crossed = touch[left:right] <= entry - base.TICK if side == "LONG" else touch[left:right] >= entry + base.TICK
        hits = np.flatnonzero(same & crossed)
        fill_i = None if not len(hits) else left + int(hits[0])
        row.update(eligible=True, final_entry=entry, final_structural_sl=stop,
                   final_initial_risk_points=risk, fixed_2r_target=target,
                   policy_A_target=target, policy_B_target=target, dol_id="FIXED_2R",
                   entry_activation_timestamp=pd.Timestamp(activation, unit="ms", tz="UTC"),
                   order_expiry=pd.Timestamp(expiry, unit="ms", tz="UTC"),
                   estimated_fill=fill_i is not None,
                   estimated_fill_timestamp=None if fill_i is None else raw.ts_event.iloc[fill_i])
        if fill_i is None:
            reasons["UNFILLED_WITHIN_10_MINUTES"] += 1
        candidates.append(row)
        orders.append({
            "order_id": base.stable_id("ORDER_ABDIR_" + side, candidate_id, activation, entry, stop),
            "candidate_id": candidate_id, "strategy": "AB_DIRECTIONAL", "direction": side,
            "chronological_index": n, "trading_day": row["trading_day"],
            "instrument_id": int(row["instrument_id"]), "epoch": int(row["epoch"]),
            "activation_timestamp": row["entry_activation_timestamp"],
            "expiry_timestamp": row["order_expiry"], "entry_price": entry,
            "structural_sl_price": stop, "initial_risk_points": risk,
            "policy_A_target": target, "policy_B_target": target, "dol_id": "FIXED_2R",
            "estimated_fill": row["estimated_fill"],
            "estimated_fill_timestamp": row["estimated_fill_timestamp"],
            "fill_rule": ("first same-instrument minute low <= entry - 0.25" if side == "LONG" else
                          "first same-instrument minute high >= entry + 0.25")
                         + "; activation inclusive; expiry exclusive",
        })
    return candidates, orders, {
        "identity": IDENTITY, "direction": side, "canonical_outputs": len(outputs),
        "valid_geometry": sum(bool(x.get("eligible")) for x in candidates),
        "resting_orders": len(orders), "estimated_fills": sum(bool(x["estimated_fill"]) for x in orders),
        "physical_geometry_duplicates_removed": reasons.get("DUPLICATE_PHYSICAL_GEOMETRY", 0),
        "principal_rejection_reasons": dict(reasons),
    }
