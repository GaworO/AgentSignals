#!/usr/bin/env python3
"""Forward-only DOL Delivery Reversal shadow strategy.

Consumes already-emitted canonical A/B Reversal records after the canonical
execution decision and after ranked DOL metadata has been attached.  It has no
broker, webhook, order, retry or guard authority.

Frozen v1 gate:
  * canonical A/B Reversal setup
  * native liquidity catalyst family (not F.P.FVG/VI research families)
  * ranked DOL narrative COMPLETE_DOL_NARRATIVE
  * selected DOL OPEN and aligned with the new reversal direction
  * theoretical target fixed at +2R from the existing canonical entry/SL
  * existing shadow scorer owns honest next-bar/through-fill modelling
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
from typing import Any

import shadow

INTERNAL_NAME = "DOL_DELIVERY_REVERSAL"
DISPLAY_NAME = "DOL Delivery Reversal"
SCHEMA = "DOL_DELIVERY_REVERSAL_SHADOW_V1"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
LOG = os.path.join(DATA_DIR, "dol_delivery_reversal_shadow.json")
_LOCK = threading.RLock()

_NATIVE_PREFIXES = {
    "AH", "AL", "LH", "LL", "NYAMH", "NYAML", "NYLH", "NYLL",
    "NYPMH", "NYPML", "PDH", "PDL", "PWH", "PWL", "BSL", "SSL",
}


def _base_cat(value: Any) -> str:
    text = str(value or "").upper().strip()
    # DIB/ORPH are detector variants of the same catalyst identity.
    text = text.split("+DIB", 1)[0].split("+ORPH", 1)[0]
    return text


def is_canonical_reversal(signal: dict[str, Any]) -> bool:
    model = str(signal.get("model") or "").strip().lower()
    return signal.get("_strat", "A/B") == "A/B" and model == "reversal"


def is_native_liquidity_catalyst(signal: dict[str, Any]) -> bool:
    cat = _base_cat(signal.get("cat"))
    # Exact common names plus BSL/SSL composite/equal-liquidity tags.
    if cat in _NATIVE_PREFIXES:
        return True
    return cat.startswith("BSL") or cat.startswith("SSL")


def eligibility(signal: dict[str, Any], dol: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dol if isinstance(dol, dict) else signal.get("_dol") or {}
    canonical = is_canonical_reversal(signal)
    native = is_native_liquidity_catalyst(signal)
    narrative = str(metadata.get("narrative_class") or "NO_CLEAR_NARRATIVE").upper()
    status = str(metadata.get("dol_status") or "UNAVAILABLE").upper()
    aligned = metadata.get("direction_aligned_with_dol") is True
    accepted = bool(
        canonical and native and narrative == "COMPLETE_DOL_NARRATIVE"
        and status == "OPEN" and aligned
    )
    reason = "ACCEPT" if accepted else "; ".join([
        "not canonical Reversal" if not canonical else "",
        "non-native catalyst" if not native else "",
        f"narrative={narrative}" if narrative != "COMPLETE_DOL_NARRATIVE" else "",
        f"dol_status={status}" if status != "OPEN" else "",
        "DOL not aligned" if not aligned else "",
    ]).strip("; ")
    return {
        "canonical_reversal": canonical,
        "native_liquidity_catalyst": native,
        "narrative_class": narrative,
        "dol_status": status,
        "direction_aligned_with_dol": aligned,
        "accepted": accepted,
        "reason": reason or "REJECT",
    }


def _load() -> list[dict[str, Any]]:
    try:
        with open(LOG, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _save(rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    tmp = LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, separators=(",", ":"), default=str)
    os.replace(tmp, LOG)


def _timestamp(signal: dict[str, Any], metadata: dict[str, Any]) -> int:
    if metadata.get("evaluated_at_ms") is not None:
        return int(metadata["evaluated_at_ms"])
    if signal.get("entry_ms") is not None:
        return int(signal["entry_ms"])
    return int(signal["bos_ms"]) + 60_000


def _fixed_2r(entry: float, sl: float, direction: str) -> float:
    risk = abs(float(entry) - float(sl))
    return round(float(entry) + (2.0 * risk if direction == "LONG" else -2.0 * risk), 2)


def _record(signal: dict[str, Any], metadata: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    gate = eligibility(signal, metadata)
    direction = str(signal.get("dir") or "").upper()
    entry = float(signal["entry"])
    sl = float(signal["SL"])
    tp = _fixed_2r(entry, sl, direction)
    accepted = gate["accepted"]
    return {
        "schema": SCHEMA,
        "strategy": INTERNAL_NAME,
        "display_name": DISPLAY_NAME,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "timestamp_ms": _timestamp(signal, metadata),
        "candidate_id": str(candidate_id),
        "direction": direction,
        "catalyst": signal.get("cat"),
        "model": signal.get("model"),
        "source_families": list(metadata.get("source_families") or []),
        "source_present": metadata.get("source_present"),
        "narrative_class": metadata.get("narrative_class"),
        "selected_dol": metadata.get("selected_dol"),
        "dol_price": metadata.get("dol_price"),
        "dol_tier": metadata.get("dol_tier"),
        "dol_tier_class": metadata.get("dol_tier_class"),
        "dol_status": metadata.get("dol_status"),
        "dol_direction": metadata.get("dol_direction"),
        "stacked_constituents": list(metadata.get("stacked_constituents") or []),
        "accepted_by_DOL_DELIVERY_REVERSAL": accepted,
        "gate_reason": gate["reason"],
        "theoretical_entry": entry,
        "SL": sl,
        "TP": tp,
        "risk_points": abs(entry - sl),
        "bos_ms": int(signal["bos_ms"]),
        "entry_ms": int(signal["entry_ms"]) if signal.get("entry_ms") is not None else None,
        "current_trade_state": "SHADOW_ACTIVE" if accepted else "REJECTED",
        "final_shadow_outcome": None,
        "realized_R": None,
        "net_USD": None,
        "live_forward_observation": True,
        "historical_reference_included": False,
        "execution_enabled": False,
    }


def observe(signal: dict[str, Any], dol: dict[str, Any] | None = None, *, candidate_id: str | None = None) -> bool:
    try:
        if not is_canonical_reversal(signal):
            return False
        metadata = copy.deepcopy(dol if isinstance(dol, dict) else signal.get("_dol") or {})
        key = candidate_id or "%s|%s|%s|%s" % (
            signal.get("date", ""), signal.get("bos", signal.get("bos_ms", "")),
            signal.get("dir", ""), signal.get("cat", ""),
        )
        row = _record(signal, metadata, key)
        with _LOCK:
            rows = _load()
            if any(item.get("candidate_id") == row["candidate_id"] for item in rows):
                return False
            rows.append(row)
            _save(rows)
        # Separate broker-inert manager challenger. Only ACCEPTED strategy rows are observed.
        if row.get("accepted_by_DOL_DELIVERY_REVERSAL"):
            try:
                import dol_reversal_manager_shadow_v1 as manager_shadow
                manager_shadow.observe_candidate(row, signal)
            except Exception as exc:
                print("[DOL_REVERSAL_MANAGER] observe err", exc, flush=True)
        return True
    except Exception as exc:
        print("[DOL_DELIVERY_REVERSAL] observe err", exc, flush=True)
        return False


def _refresh_locked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [r for r in rows if r.get("accepted_by_DOL_DELIVERY_REVERSAL") and r.get("current_trade_state") == "SHADOW_ACTIVE"]
    if not active:
        return rows
    try:
        bars = shadow._bars(since_ms=min(int(r["bos_ms"]) for r in active))
    except Exception as exc:
        print("[DOL_DELIVERY_REVERSAL] shadow bars err", exc, flush=True)
        return rows
    changed = False
    for row in active:
        result = shadow.score(
            row["direction"], row["theoretical_entry"], row["SL"], row["TP"],
            row["bos_ms"], *bars, entry_ms=row.get("entry_ms"),
        )
        outcome = result.get("outcome")
        if outcome in {None, "open", "out_of_range"}:
            continue
        if outcome in {"no_fill", "missed"}:
            row["current_trade_state"] = outcome.upper()
            row["final_shadow_outcome"] = outcome.upper()
        else:
            row["current_trade_state"] = "CLOSED"
            row["final_shadow_outcome"] = str(outcome).upper()
            row["realized_R"] = result.get("R")
            row["net_USD"] = result.get("net")
            row["fill_ms"] = result.get("fill_ms")
            row["wait_min"] = result.get("wait_min")
        changed = True
    if changed:
        _save(rows)
    return rows


def refresh() -> list[dict[str, Any]]:
    with _LOCK:
        return _refresh_locked(_load())


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in rows if r.get("accepted_by_DOL_DELIVERY_REVERSAL") and r.get("final_shadow_outcome") in {"WIN", "LOSS", "TIMEOUT"} and r.get("realized_R") is not None]
    wins = sum(r["final_shadow_outcome"] == "WIN" for r in completed)
    losses = sum(r["final_shadow_outcome"] == "LOSS" for r in completed)
    gains = sum(max(0.0, float(r.get("net_USD") or 0.0)) for r in completed)
    draw = -sum(min(0.0, float(r.get("net_USD") or 0.0)) for r in completed)
    return {
        "trades": len(completed), "wins": wins, "losses": losses,
        "WR_pct": 100.0 * wins / len(completed) if completed else None,
        "PF": gains / draw if draw else (None if not gains else "inf"),
        "Net_R": sum(float(r["realized_R"]) for r in completed),
        "Net_USD": sum(float(r.get("net_USD") or 0.0) for r in completed),
    }


def snapshot() -> dict[str, Any]:
    rows = refresh()
    accepted_open = [r for r in rows if r.get("accepted_by_DOL_DELIVERY_REVERSAL") and r.get("current_trade_state") == "SHADOW_ACTIVE"]
    completed = [r for r in rows if r.get("final_shadow_outcome") in {"WIN", "LOSS", "TIMEOUT"}]
    return {
        "strategy": INTERNAL_NAME,
        "display_name": DISPLAY_NAME,
        "shadow_only": True,
        "execution_enabled": False,
        "status": "ACTIVE",
        "current_signal": rows[-1] if rows else None,
        "current_trade": accepted_open[-1] if accepted_open else None,
        "last_result": completed[-1] if completed else None,
        "stats": _stats(rows),
        "rows": list(reversed(rows[-250:])),
    }


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DOL Delivery Reversal</title>
<style>*{box-sizing:border-box}body{margin:0;padding:16px;background:#0b0e14;color:#e6e9ef;font:14px/1.45 system-ui}h2{margin:0}.mut{color:#8791a3}.warn{color:#fbbf24}.cards{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.card{min-width:135px;padding:11px 14px;background:#141a28;border:1px solid #1b2230;border-radius:10px}.label{font-size:11px;color:#8791a3;text-transform:uppercase}.value{font-size:19px;font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:14px}.panel{padding:12px;background:#101624;border:1px solid #1b2230;border-radius:10px}.k{font-size:11px;color:#8791a3;text-transform:uppercase}.v{font-weight:650;margin-top:3px}.yes,.long{color:#4ade80}.no,.short{color:#f87171}.wrap{overflow:auto;max-height:55vh;border:1px solid #1b2230;border-radius:10px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #1b2230;white-space:nowrap}th{position:sticky;top:0;background:#0b0e14;color:#8791a3}</style></head>
<body><h2>DOL Delivery Reversal <span class="mut">· shadow</span></h2><div class="mut">Liquidity delivered → causal Reversal → OPEN DOL in the new direction → fixed 2R. <span class="warn">SHADOW ONLY · NO BROKER EXECUTION</span> · <a href="/dol-reversal-manager" style="color:#60a5fa">Manager Shadow →</a></div><div class="cards" id="stats"></div><div class="grid" id="current"></div><div class="wrap"><table id="rows"></table></div>
<script>function e(v){return String(v==null?'—':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}function f(v,n){return typeof v==='number'?v.toFixed(n):e(v)}function card(k,v){return '<div class="card"><div class="label">'+k+'</div><div class="value">'+v+'</div></div>'}function cell(k,v){return '<div class="panel"><div class="k">'+k+'</div><div class="v">'+e(v)+'</div></div>'}
function render(d){let s=d.stats||{},r=d.current_signal||{},t=d.current_trade||r,l=d.last_result||{};stats.innerHTML=card('Status',d.status)+card('Trades',s.trades||0)+card('WR %',s.WR_pct==null?'—':f(s.WR_pct,2)+'%')+card('PF',s.PF==null?'—':f(s.PF,3))+card('Net R',f(s.Net_R||0,3))+card('Net $',f(s.Net_USD||0,0));current.innerHTML=cell('Candidate',r.candidate_id)+cell('Direction',r.direction)+cell('Catalyst',r.catalyst)+cell('Narrative',r.narrative_class)+cell('Source families',(r.source_families||[]).join(', '))+cell('Open DOL',r.selected_dol)+cell('DOL price',r.dol_price)+cell('DOL tier',r.dol_tier_class)+cell('Entry',r.theoretical_entry)+cell('SL',r.SL)+cell('Fixed 2R',r.TP)+cell('Take?',r.accepted_by_DOL_DELIVERY_REVERSAL===true?'YES':r.accepted_by_DOL_DELIVERY_REVERSAL===false?'NO':'—')+cell('Gate reason',r.gate_reason)+cell('State',t.current_trade_state)+cell('Last result',l.final_shadow_outcome?(l.final_shadow_outcome+' · '+e(l.realized_R)+'R'):'—');let h='<tr><th>Recorded</th><th>Dir</th><th>Catalyst</th><th>Narrative</th><th>DOL</th><th>DOL px</th><th>Tier</th><th>Take</th><th>Gate reason</th><th>Entry</th><th>SL</th><th>2R</th><th>State</th><th>Result</th><th>R</th></tr>';let b=(d.rows||[]).map(x=>'<tr><td>'+e(x.recorded_at)+'</td><td class="'+e((x.direction||'').toLowerCase())+'">'+e(x.direction)+'</td><td>'+e(x.catalyst)+'</td><td>'+e(x.narrative_class)+'</td><td>'+e(x.selected_dol)+'</td><td>'+e(x.dol_price)+'</td><td>'+e(x.dol_tier_class)+'</td><td class="'+(x.accepted_by_DOL_DELIVERY_REVERSAL?'yes':'no')+'">'+(x.accepted_by_DOL_DELIVERY_REVERSAL?'YES':'NO')+'</td><td>'+e(x.gate_reason)+'</td><td>'+e(x.theoretical_entry)+'</td><td>'+e(x.SL)+'</td><td>'+e(x.TP)+'</td><td>'+e(x.current_trade_state)+'</td><td>'+e(x.final_shadow_outcome)+'</td><td>'+e(x.realized_R)+'</td></tr>').join('');rows.innerHTML=h+(b||'<tr><td colspan="15" class="mut">No forward observations yet.</td></tr>')}
function load(){fetch('/dol-delivery-reversal/data',{cache:'no-store'}).then(r=>r.json()).then(render)}load();setInterval(load,30000);</script></body></html>'''


def register(app):
    from flask import Response, jsonify, send_file
    def page(): return Response(PAGE, mimetype="text/html")
    def data():
        response = jsonify(snapshot()); response.headers["Cache-Control"] = "no-store, max-age=0"; return response
    def pine():
        return send_file(os.path.join(HERE, "pine_dol_delivery_reversal.pine"), mimetype="text/plain", as_attachment=False)
    app.add_url_rule("/dol-delivery-reversal", "dol_delivery_reversal_shadow", page)
    app.add_url_rule("/dol-delivery-reversal/data", "dol_delivery_reversal_shadow_data", data)
    app.add_url_rule("/dol-delivery-reversal/pine", "dol_delivery_reversal_shadow_pine", pine)
    return app
