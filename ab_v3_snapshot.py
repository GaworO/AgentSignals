"""Worker-only causal DOL decoration. Does not change eligibility/SL/TP."""
from __future__ import annotations

import math


def attach(raw, candidates, orders, base, short_engine):
    engine, epochs, long_ledger = base.dol_resources(raw)
    short_ledger = short_engine.ssl_ledger(engine, epochs)
    ledgers = {"LONG": long_ledger, "SHORT": short_ledger}
    tags = {}

    def tag(side, at, epoch):
        key = (side, int(at), int(epoch))
        if key not in tags:
            tags[key] = base.tag_setup_dol(engine, direction=side, evaluated_at_ms=int(at),
                extra_levels=ledgers[side].get(int(epoch), []), include_native_levels=False).to_dict()
        return tags[key]

    by_id = {}
    for row in candidates:
        side = row.get("dir", "LONG")
        z = 1 if side == "LONG" else -1
        source = row.get("source_event") or {}
        prefix = "bsl" if z == 1 else "ssl"
        at = int(row["entry_ms"])
        d = tag(side, at, row["epoch"])
        pool = d.get("current_dol") or {}
        price = pool.get("pool_price")
        known = (d.get("dol_status") == "OPEN" and price is not None
                 and z*(base.tick(price)-float(row.get("final_entry", row["entry"]))) > 0)
        edge = row.get("fvg_lo" if z == 1 else "fvg_hi")
        snap = dict(policy="AB_V3_MTF_SOURCE_DOL_2R", selected_at_ms=at,
            source_side=prefix.upper(), source_name=source.get(prefix+"_name"),
            source_level=source.get(prefix+"_price"), source_event=dict(source),
            fvg_edge=(math.floor(float(edge)/.25)*.25 if z == 1 else math.ceil(float(edge)/.25)*.25) if edge is not None else None,
            frozen_dol=dict(id=pool.get("pool_id") if known else None,
                price=base.tick(price) if known else None, state="OPEN" if known else "UNKNOWN",
                tier=pool.get("priority_tier"), constituents=pool.get("constituent_levels", []), selected_at_ms=at),
            source_contract_identity="ARCHIVE_REQUIRES_VERIFICATION", dol_tag=d)
        row["v3_snapshot"] = snap
        by_id[row["candidate_id"]] = snap
    for order in orders:
        order["v3_snapshot"] = by_id.get(order["candidate_id"])
    latest = int(raw.ts_event.iloc[-1].timestamp()*1000)+60000
    ep = next(e["epoch"] for e in epochs if e["start"] <= len(raw)-1 < e["end"])
    return {"evaluated_at_ms": latest, "LONG": tag("LONG", latest, ep), "SHORT": tag("SHORT", latest, ep),
            "contract_identity": "ARCHIVE_REQUIRES_VERIFICATION"}
