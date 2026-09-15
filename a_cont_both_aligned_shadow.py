#!/usr/bin/env python3
"""Forward-only shadow book for canonical A Continuation + BOTH_ALIGNED.

This module consumes already-emitted canonical A/B records after their normal
execution decision.  It owns no detector, guard, webhook, or broker adapter.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
from typing import Any

import shadow


INTERNAL_NAME = "A_CONT_BOTH_ALIGNED"
DISPLAY_NAME = "A Continuation — Both Aligned"
SCHEMA = "A_CONT_BOTH_ALIGNED_SHADOW_V1"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
LOG = os.path.join(DATA_DIR, "a_cont_both_aligned_shadow.json")
_LOCK = threading.RLock()


def is_canonical_a_continuation(signal: dict[str, Any]) -> bool:
    """Recognize the existing canonical A Continuation output without changing it."""
    model = str(signal.get("model") or "").strip().lower()
    family = str(signal.get("cls") or "A").strip().upper()
    return (
        signal.get("_strat", "A/B") == "A/B"
        and family == "A"
        and model in {"cont", "continuation"}
    )


def eligibility(signal: dict[str, Any], dol: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure frozen gate: canonical A Continuation and exactly BOTH_ALIGNED."""
    metadata = dol if isinstance(dol, dict) else signal.get("_dol") or {}
    state = str(metadata.get("alignment_classification") or "AMBIGUOUS").upper()
    canonical = is_canonical_a_continuation(signal)
    accepted = bool(canonical and state == "BOTH_ALIGNED")
    return {
        "canonical_a_continuation": canonical,
        "alignment_classification": state,
        "accepted": accepted,
        "reason": (
            "BOTH_ALIGNED"
            if accepted
            else "not canonical A Continuation"
            if not canonical
            else state
        ),
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
    temporary = LOG + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, separators=(",", ":"), default=str)
    os.replace(temporary, LOG)


def _timestamp(signal: dict[str, Any], metadata: dict[str, Any]) -> int:
    if metadata.get("evaluated_at_ms") is not None:
        return int(metadata["evaluated_at_ms"])
    if signal.get("entry_ms") is not None:
        return int(signal["entry_ms"])
    return int(signal["bos_ms"]) + 60_000


def _record(signal: dict[str, Any], metadata: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    gate = eligibility(signal, metadata)
    timestamp_ms = _timestamp(signal, metadata)
    accepted = gate["accepted"]
    return {
        "schema": SCHEMA,
        "strategy": INTERNAL_NAME,
        "display_name": DISPLAY_NAME,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "timestamp_ms": timestamp_ms,
        "candidate_id": str(candidate_id),
        "direction": str(signal.get("dir") or "").upper(),
        "catalyst": signal.get("cat"),
        "htf_dol": metadata.get("selected_dol"),
        "htf_dol_price": metadata.get("dol_price"),
        "htf_dol_tier": metadata.get("dol_tier"),
        "htf_dol_status": metadata.get("dol_status"),
        "htf_dol_direction": metadata.get("dol_direction"),
        "execution_dol": metadata.get("execution_dol"),
        "execution_dol_price": metadata.get("execution_dol_price"),
        "execution_dol_direction": metadata.get("execution_dol_direction"),
        "execution_dol_source": metadata.get("execution_dol_source"),
        "execution_dol_timeframe": metadata.get("execution_dol_timeframe"),
        "alignment_classification": gate["alignment_classification"],
        "alignment_reason": metadata.get("alignment_reason") or gate["reason"],
        "accepted_by_A_CONT_BOTH_ALIGNED": accepted,
        "theoretical_entry": float(signal["entry"]),
        "SL": float(signal["SL"]),
        "TP": float(signal["TP"]),
        "bos_ms": int(signal["bos_ms"]),
        "entry_ms": int(signal["entry_ms"]) if signal.get("entry_ms") is not None else None,
        "current_trade_state": "SHADOW_ACTIVE" if accepted else "REJECTED",
        "final_shadow_outcome": None,
        "realized_R": None,
        "net_USD": None,
        "live_forward_observation": True,
        "historical_reference_included": False,
    }


def observe(
    signal: dict[str, Any], dol: dict[str, Any] | None = None, *, candidate_id: str | None = None
) -> bool:
    """Persist one emitted A Continuation decision; never mutate ``signal``."""
    try:
        if not is_canonical_a_continuation(signal):
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
        return True
    except Exception as exc:
        print("[A_CONT_BOTH_ALIGNED] observe err", exc, flush=True)
        return False


def _refresh_locked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [
        row for row in rows
        if row.get("accepted_by_A_CONT_BOTH_ALIGNED")
        and row.get("current_trade_state") == "SHADOW_ACTIVE"
    ]
    if not active:
        return rows
    try:
        bars = shadow._bars(since_ms=min(int(row["bos_ms"]) for row in active))
    except Exception as exc:
        print("[A_CONT_BOTH_ALIGNED] shadow bars err", exc, flush=True)
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
    completed = [
        row for row in rows
        if row.get("accepted_by_A_CONT_BOTH_ALIGNED")
        and row.get("final_shadow_outcome") in {"WIN", "LOSS", "TIMEOUT"}
        and row.get("realized_R") is not None
    ]
    wins = sum(row["final_shadow_outcome"] == "WIN" for row in completed)
    losses = sum(row["final_shadow_outcome"] == "LOSS" for row in completed)
    gains = sum(max(0.0, float(row.get("net_USD") or 0.0)) for row in completed)
    draw = -sum(min(0.0, float(row.get("net_USD") or 0.0)) for row in completed)
    return {
        "trades": len(completed),
        "wins": wins,
        "losses": losses,
        "WR_pct": 100.0 * wins / len(completed) if completed else None,
        "PF": gains / draw if draw else (None if not gains else "inf"),
        "Net_R": sum(float(row["realized_R"]) for row in completed),
    }


def snapshot() -> dict[str, Any]:
    rows = refresh()
    accepted_open = [
        row for row in rows
        if row.get("accepted_by_A_CONT_BOTH_ALIGNED")
        and row.get("current_trade_state") == "SHADOW_ACTIVE"
    ]
    completed = [row for row in rows if row.get("final_shadow_outcome") in {"WIN", "LOSS", "TIMEOUT"}]
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
        "rows": list(reversed(rows[-200:])),
    }


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>A Continuation — Both Aligned</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:16px;background:#0b0e14;color:#e6e9ef;font:14px/1.45 system-ui,Segoe UI,sans-serif}
h2{margin:0 0 3px}.sub,.mut{color:#8791a3}.sub{margin-bottom:14px}.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}.card{min-width:135px;padding:11px 14px;background:#141a28;border:1px solid #1b2230;border-radius:10px}.label{font-size:11px;color:#8791a3;text-transform:uppercase}.value{font-size:19px;font-weight:700;margin-top:2px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:14px}.panel{padding:12px;background:#101624;border:1px solid #1b2230;border-radius:10px}.k{font-size:11px;color:#8791a3;text-transform:uppercase}.v{font-weight:650;margin-top:3px}.yes,.long{color:#4ade80}.no,.short{color:#f87171}.wrap{overflow:auto;max-height:54vh;border:1px solid #1b2230;border-radius:10px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #1b2230;white-space:nowrap}th{position:sticky;top:0;background:#0b0e14;color:#8791a3}.empty{text-align:center;padding:25px;color:#8791a3}
</style></head><body><h2>A Continuation — Both Aligned <span class="mut">· shadow</span></h2>
<div class="sub">Forward-only theoretical execution. Broker submission is disabled.</div><div class="cards" id="stats"></div><div class="grid" id="current"></div><div class="wrap"><table id="rows"></table></div>
<script>
function e(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function f(v,n){return typeof v==='number'?v.toFixed(n):e(v)} function card(k,v){return '<div class="card"><div class="label">'+k+'</div><div class="value">'+v+'</div></div>'} function cell(k,v){return '<div class="panel"><div class="k">'+k+'</div><div class="v">'+e(v)+'</div></div>'}
function render(d){var s=d.stats||{},r=d.current_signal||{},t=d.current_trade||r,last=d.last_result||{};document.getElementById('stats').innerHTML=card('Status',d.status)+card('Trades',s.trades||0)+card('WR %',s.WR_pct==null?'—':f(s.WR_pct,2)+'%')+card('PF',s.PF==null?'—':f(s.PF,3))+card('Net R',f(s.Net_R||0,3))+card('Wins / losses',(s.wins||0)+' / '+(s.losses||0));
document.getElementById('current').innerHTML=cell('Current signal',r.candidate_id)+cell('Direction',r.direction)+cell('Catalyst',r.catalyst)+cell('HTF DOL',r.htf_dol)+cell('HTF DOL price',r.htf_dol_price)+cell('HTF direction',r.htf_dol_direction)+cell('Execution DOL',r.execution_dol)+cell('Execution DOL price',r.execution_dol_price)+cell('Execution direction',r.execution_dol_direction)+cell('BOTH_ALIGNED',r.accepted_by_A_CONT_BOTH_ALIGNED===true?'YES':r.accepted_by_A_CONT_BOTH_ALIGNED===false?'NO':'—')+cell('Entry',r.theoretical_entry)+cell('SL',r.SL)+cell('TP',r.TP)+cell('Current trade state',t.current_trade_state)+cell('Last result',last.final_shadow_outcome?(last.final_shadow_outcome+' · '+e(last.realized_R)+'R'):'—');
var h='<tr><th>Timestamp</th><th>Candidate</th><th>Dir</th><th>Catalyst</th><th>HTF DOL</th><th>HTF dir</th><th>Execution DOL</th><th>Exec dir</th><th>Alignment</th><th>Take?</th><th>Entry</th><th>SL</th><th>TP</th><th>State</th><th>Result</th><th>R</th></tr>';var b=(d.rows||[]).map(function(x){return '<tr><td>'+e(x.recorded_at)+'</td><td>'+e(x.candidate_id)+'</td><td class="'+e((x.direction||'').toLowerCase())+'">'+e(x.direction)+'</td><td>'+e(x.catalyst)+'</td><td>'+e(x.htf_dol)+'</td><td>'+e(x.htf_dol_direction)+'</td><td>'+e(x.execution_dol)+'</td><td>'+e(x.execution_dol_direction)+'</td><td>'+e(x.alignment_classification)+'</td><td class="'+(x.accepted_by_A_CONT_BOTH_ALIGNED?'yes':'no')+'">'+(x.accepted_by_A_CONT_BOTH_ALIGNED?'YES':'NO')+'</td><td>'+e(x.theoretical_entry)+'</td><td>'+e(x.SL)+'</td><td>'+e(x.TP)+'</td><td>'+e(x.current_trade_state)+'</td><td>'+e(x.final_shadow_outcome)+'</td><td>'+e(x.realized_R)+'</td></tr>'}).join('');if(!b)b='<tr><td colspan="16" class="empty">No live A Continuation candidates yet. Forward statistics start with the next live observation.</td></tr>';document.getElementById('rows').innerHTML=h+b}
function load(){fetch('/a-cont-both-aligned/data',{cache:'no-store'}).then(function(r){return r.json()}).then(render)}load();setInterval(load,30000);
</script></body></html>"""


def register(app):
    """Register read-only dashboard routes.  There is deliberately no POST route."""
    from flask import Response, jsonify

    def page():
        return Response(PAGE, mimetype="text/html")

    def data():
        response = jsonify(snapshot())
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    app.add_url_rule("/a-cont-both-aligned", "a_cont_both_aligned_shadow", page)
    app.add_url_rule("/a-cont-both-aligned/data", "a_cont_both_aligned_shadow_data", data)
    return app
