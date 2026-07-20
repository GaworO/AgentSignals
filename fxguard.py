#!/usr/bin/env python3
"""
fxguard.py — JOINED forex Auto-Executor view (EUR/USD + USD/JPY), read-only aggregator.
Isolated add-on (same pattern as forex_pnl.py): TWO routes on the MAIN agent:
  /fxguard       — the joined dashboard page (cards per pair + merged decision book)
  /fxguard/data  — server-side aggregate of each FX service's /guard/data (no CORS)
Read-only by design: ARM/HALT/mode live on each service's own /guard?t=TOKEN (linked from the page).
Env (main agent): FX_EUR_URL, FX_JPY_URL (same ones forex_pnl uses).
"""
import os, json
try:
    import requests
except Exception:
    requests = None

PAIRS = [('EUR/USD', 'FX_EUR_URL'), ('USD/JPY', 'FX_JPY_URL')]


def _fetch(base):
    try:
        r = requests.get(base.rstrip('/') + '/guard/data', timeout=6)
        if getattr(r, 'status_code', 0) == 200:
            return r.json()
    except Exception as e:
        print('[fxguard]', base, e, flush=True)
    return None


def register(app, path='/fxguard'):
    try:
        from flask import jsonify, Response
    except Exception:
        return app

    def _data():
        out = []
        for name, env in PAIRS:
            base = os.environ.get(env, '')
            d = _fetch(base) if (base and requests is not None) else None
            out.append(dict(pair=name, url=base, ok=d is not None, data=d))
        return jsonify(pairs=out)

    def _page():
        return Response(_HTML, mimetype='text/html')

    app.add_url_rule(path, 'fxguard_page', _page)
    app.add_url_rule(path + '/data', 'fxguard_data', _data)
    return app


_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Forex — Auto-Executor (joined)</title><style>
*{box-sizing:border-box;margin:0;font-family:system-ui,-apple-system,sans-serif}
body{background:#0d0d0d;color:#eee;padding:14px;font-size:13px}
h1{font-size:15px;margin-bottom:2px}.sub{color:#888;font-size:11px;margin-bottom:12px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:10px;margin-bottom:14px}
.pc{background:#1a1a19;border:1px solid #ffffff1a;border-radius:10px;padding:12px 14px}
.pc h2{font-size:14px;margin-bottom:6px;display:flex;gap:8px;align-items:center}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:10.5px;font-weight:700}
.on{background:#0ca30c22;color:#3ecb3e}.off{background:#89878122;color:#aaa}.kill{background:#d03b3b22;color:#e66}
.warnp{background:#e0a93b22;color:#e0a93b}.manual{background:#3987e522;color:#7ab8f5}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.kv div{background:#232322;border-radius:6px;padding:6px 8px}
.kv .l{color:#888;font-size:9.5px;text-transform:uppercase}.kv .v{font-size:14px;font-weight:700;margin-top:1px}
a.go{color:#22d3ee;font-size:11px;text-decoration:none;margin-left:auto}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th{color:#888;text-align:left;font-weight:500;padding:5px;border-bottom:1px solid #333;font-size:10px;text-transform:uppercase}
td{padding:5px;border-bottom:1px solid #232322}
.sent{color:#3ecb3e;font-weight:600}.blk{color:#e88}.win{color:#3ecb3e}.loss{color:#e66}.open{color:#e0a93b}.g{color:#888}
.err{color:#e66;font-size:12px;padding:8px}
</style></head><body>
<h1>Forex — Auto-Executor <span class=g style="font-weight:400">(joined · read-only)</span></h1>
<div class=sub>EUR/USD + USD/JPY · each pair has its OWN guard, counters and kill-latch · arm/halt on the pair's page (links →)</div>
<div class=row id=cards></div>
<table><thead><tr><th>Pair</th><th>Time ET</th><th>Sess</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Qty</th><th>Decision</th><th>Outcome</th><th>R</th><th>Net$</th></tr></thead><tbody id=tb></tbody></table>
<script>
async function load(){
 let d;
 try{ d=await (await fetch('/fxguard/data',{cache:'no-store'})).json(); }catch(e){ return; }
 let cards='', rows=[];
 (d.pairs||[]).forEach(function(p){
  if(!p.ok||!p.data){ cards+='<div class=pc><h2>'+p.pair+'</h2><div class=err>service unreachable — '+(p.url||'FX URL env not set')+'</div></div>'; return; }
  let g=p.data, h=g.health||{}, e=g.eval||{};
  let mp=g.mode==='auto'?'on':(g.mode==='manual'?'manual':'off');
  let hp=h.status==='ok'?'on':(h.status==='critical'?'kill':(h.status==='paused'?'off':'warnp'));
  cards+='<div class=pc><h2>'+p.pair+' <span class="pill '+mp+'">'+(g.mode||'?').toUpperCase()+'</span>'
   +'<span class="pill '+(g.kill?'kill':'on')+'">'+(g.kill?('HALTED: '+(g.kill_reason||'')):'armed')+'</span>'
   +'<span class="pill '+hp+'">HEALTH '+(h.status||'?').toUpperCase()+'</span>'
   +'<a class=go href="'+p.url+'/guard" target=_blank>open guard ↗</a></h2>'
   +'<div class=kv>'
   +'<div><div class=l>Equity</div><div class=v>$'+((e.equity||0).toLocaleString())+'</div></div>'
   +'<div><div class=l>DD buffer</div><div class=v>$'+((e.buffer||0).toLocaleString())+'</div></div>'
   +'<div><div class=l>Trades</div><div class=v>'+(g.trades||0)+' / '+(g.max_trades||0)+'</div></div>'
   +'<div><div class=l>Losses</div><div class=v>'+(g.losses||0)+' / '+(g.loss_n||0)+'</div></div>'
   +'</div></div>';
  (g.book||[]).forEach(function(x){ x._pair=p.pair; rows.push(x); });
 });
 document.getElementById('cards').innerHTML=cards;
 rows.sort(function(a,b){return (b.ts||0)-(a.ts||0);});
 let dec=function(x){return x.decision==='sent'?('<span class=sent>SENT'+(x.qty?(' ×'+x.qty):'')+'</span>'):('<span class=blk>BLOCK: '+(x.reason||'')+'</span>');};
 let oc=function(x){var o=x.outcome||'';var c=o==='win'?'win':o==='loss'?'loss':o==='open'?'open':'g';return '<span class='+c+'>'+o+'</span>';};
 document.getElementById('tb').innerHTML=rows.slice(0,80).map(function(x){
  return '<tr><td>'+x._pair+'</td><td>'+(x.et||'')+'</td><td>'+(x.sess||'')+'</td><td>'+(x.dir||'')+'</td><td>'+(x.entry||'')
   +'</td><td>'+(x.sl||'')+'</td><td>'+(x.tp||'')+'</td><td>'+(x.qty||'')+'</td><td>'+dec(x)+'</td><td>'+oc(x)
   +'</td><td>'+(x.R!=null?x.R:'')+'</td><td>'+(x.net!=null?x.net:'')+'</td></tr>';}).join('');
}
load();setInterval(load,30000);
</script></body></html>"""
