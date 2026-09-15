"""Read-only dashboard for ranked DOL metadata attached to live A/B signals."""
from __future__ import annotations

import json
import sqlite3


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DOL narrative</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:16px;background:#0b0e14;color:#e6e9ef;font:14px/1.45 system-ui,Segoe UI,sans-serif}
h2{margin:0 0 3px}.sub,.mut{color:#8791a3}.sub{margin-bottom:14px}.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.card{min-width:145px;padding:11px 14px;background:#141a28;border:1px solid #1b2230;border-radius:10px}.label{font-size:11px;color:#8791a3;text-transform:uppercase}.value{font-size:20px;font-weight:700;margin-top:2px}
.wrap{overflow:auto;max-height:72vh;border:1px solid #1b2230;border-radius:10px}table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #1b2230;white-space:nowrap}th{position:sticky;top:0;background:#0b0e14;color:#8791a3;font-weight:600}.long,.yes,.open{color:#4ade80}.short,.no,.delivered{color:#f87171}.pill{display:inline-block;padding:2px 7px;border:1px solid #2a3550;border-radius:7px;font-size:11px}.empty{padding:24px;color:#8791a3;text-align:center}.const{white-space:normal;min-width:220px;max-width:380px}
</style></head><body>
<h2>Draw on Liquidity <span class="mut">· global market state</span></h2>
<div class="sub">Read-only HTF and execution-horizon DOL metadata. Only the separate A Continuation shadow evaluates BOTH_ALIGNED; existing strategies are unchanged.</div>
<div class="cards" id="cards"></div><div class="wrap"><table id="table"></table></div>
<script>
function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function card(k,v){return '<div class="card"><div class="label">'+k+'</div><div class="value">'+v+'</div></div>';}
function pool(v){if(!v)return '—';return esc((v.price==null?'—':v.price)+' · T'+(v.tier==null?'—':v.tier));}
function yn(v){return v===true?'<span class="yes">YES</span>':v===false?'<span class="no">NO</span>':'<span class="mut">UNKNOWN</span>';}
function constituents(a){return (a||[]).map(function(x){return esc(x.kind+' '+x.price);}).join('<br>')||'—';}
function render(d){var rows=d.rows||[], attached=rows.filter(function(r){return r.metadata_status==='ATTACHED';});
 var open=attached.filter(function(r){return r.dol_status==='OPEN';}).length;
 var complete=attached.filter(function(r){return r.narrative_class==='COMPLETE_DOL_NARRATIVE';}).length;
 var aligned=attached.filter(function(r){return r.direction_aligned_with_dol===true;}).length;
 var same=attached.filter(function(r){return r.multi_horizon_alignment==='BULLISH'||r.multi_horizon_alignment==='BEARISH';}).length;
 document.getElementById('cards').innerHTML=card('Tagged signals',attached.length)+card('Open HTF DOL',open)+card('Complete narrative',complete)+card('Trade / HTF aligned',aligned)+card('Horizons aligned',same);
 var head='<tr><th>Signal</th><th>Trade</th><th>Model / catalyst</th><th>HTF DOL</th><th>HTF dir</th><th>Tier / type</th><th>Status</th><th>Execution DOL</th><th>Exec dir</th><th>Source / TF</th><th>Multi-horizon</th><th>Source present</th><th>Narrative</th><th>Stacked constituents</th><th>Successor DOL</th></tr>';
 var body=rows.map(function(r){var cls=(r.dir||'').toLowerCase(), st=(r.dol_status||'').toLowerCase(), unavailable=r.metadata_status==='UNAVAILABLE';return '<tr><td>'+esc(r.date+' '+r.bos)+'</td><td class="'+cls+'"><b>'+esc(r.dir)+'</b></td><td>'+esc(r.model)+'<br><span class="mut">'+esc(r.cat)+'</span></td><td><b>'+esc(r.dol_price)+'</b><br><span class="mut">'+esc(r.selected_dol)+'</span></td><td>'+esc(r.dol_direction)+'</td><td><span class="pill">T'+esc(r.dol_tier)+'</span><br><span class="mut">'+esc(r.dol_tier_class)+'</span></td><td class="'+st+'"><b>'+esc(r.dol_status)+'</b>'+(unavailable?'<br><span class="no">'+esc(r.error||'DOL calculation failed')+'</span>':'')+'</td><td><b>'+esc(r.execution_dol_price)+'</b><br><span class="mut">'+esc(r.execution_dol)+'</span></td><td>'+esc(r.execution_dol_direction)+'</td><td>'+esc(r.execution_dol_source)+'<br><span class="mut">'+esc(r.execution_dol_timeframe)+'</span></td><td><b>'+esc(r.multi_horizon_alignment)+'</b><br><span class="mut">'+esc(r.alignment_classification)+'</span></td><td>'+yn(r.source_present)+'</td><td>'+(unavailable?'<span class="mut">UNKNOWN</span>':esc(r.narrative_class))+'</td><td class="const">'+constituents(r.stacked_constituents)+'</td><td>'+pool(r.successor_dol)+'</td></tr>';}).join('');
 if(!rows.length)body='<tr><td colspan="15" class="empty">No DOL-tagged live A/B signals yet. New signals will appear here automatically.</td></tr>';
 document.getElementById('table').innerHTML=head+body;}
function load(){fetch('/dol/data?limit=200',{cache:'no-store'}).then(function(r){return r.json();}).then(render).catch(function(e){document.getElementById('table').innerHTML='<tr><td class="empty">DOL data unavailable</td></tr>';});}
load();setInterval(load,30000);
</script></body></html>"""


def _rows(database_path: str, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute(
            """SELECT logged_at,date,bos,dir,model,cat,dol_json
               FROM signals WHERE dol_json IS NOT NULL
               ORDER BY logged_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    out = []
    for record in records:
        try:
            dol = json.loads(record["dol_json"] or "{}")
        except (TypeError, ValueError):
            dol = {}
        out.append({
            "logged_at": record["logged_at"], "date": record["date"],
            "bos": record["bos"], "dir": record["dir"],
            "model": record["model"], "cat": record["cat"], **dol,
        })
    return out


def register(app, database_path: str):
    """Register display-only DOL routes; no detector or order dependency."""
    from flask import Response, jsonify, request

    def page():
        return Response(PAGE, mimetype="text/html")

    def data():
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        rows = _rows(database_path, limit)
        response = jsonify(rows=rows, count=len(rows), execution_effect="none")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    app.add_url_rule("/dol", "dol_dashboard", page)
    app.add_url_rule("/dol/data", "dol_dashboard_data", data)
    return app
