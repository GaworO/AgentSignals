#!/usr/bin/env python3
"""
dashboard.py - the unified home page served at the agent root "/".

ISOLATED add-on (same pattern as pnl.py / how_ab.py): adds ONE GET "/" route that returns a nav
shell. The shell FEDERATES the pages that already exist - it does not re-implement them:

  General   : P&L=/pnl · Regime=/regime · Monitor=/monitor · News (static) · Income (static)
  A/B       : Candidates=/candidates · Performance=/performance · How=/how · Settings (static)
  C         : Dashboard=/c · How=/c/how
  F  (ext)  : opens the F service · How
  ORB (ext) : Dashboard · How

Same-origin pages load in a frame (live). External services (F, ORB) load in a frame too, with an
"open in new tab" fallback in case they block framing. Touches nothing else.

Wire into agent.py (next to pnl.register / how_ab.register):

    import dashboard
    dashboard.register(app)            # serves the home page at "/"

Env (optional, defaults match agent.py): STRAT_F_URL, STRAT_ORB_URL.
"""
import os

_F   = os.environ.get('STRAT_F_URL',  'https://strategy-f-production.up.railway.app').rstrip('/')
_ORB = os.environ.get('STRAT_ORB_URL', 'https://strategy-orb-production.up.railway.app').rstrip('/')

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading desk</title>
<style>
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#0b0e14;color:#e6e9ef;font-family:system-ui,Segoe UI,Roboto,sans-serif}
.app{display:grid;grid-template-columns:212px 1fr;height:100vh}
aside{background:#0e1320;border-right:1px solid #1b2230;padding:14px 10px;overflow:auto}
.brand{font-size:15px;font-weight:700;padding:4px 10px 10px}
.grp{font-size:11px;color:#6b7688;text-transform:uppercase;letter-spacing:.05em;padding:12px 10px 4px}
.nav{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;color:#9aa3b5;cursor:pointer;font-size:14px}
.nav:hover{background:#141a28;color:#e6e9ef}
.nav.on{background:#12294a;color:#8ab4f8}
.dot{width:8px;height:8px;border-radius:50%;background:#3a4658}
.nav.live .dot{background:#4ade80}
main{display:flex;flex-direction:column;min-width:0}
header{padding:12px 20px;border-bottom:1px solid #1b2230;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0}.sub{color:#6b7688;font-size:13px}
.tabs{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
.tab{padding:6px 11px;border-radius:8px;color:#9aa3b5;cursor:pointer;font-size:13px;border:1px solid #1b2230}
.tab.on{background:#12294a;color:#8ab4f8;border-color:#22406e}
.open{color:#8ab4f8;font-size:12px;text-decoration:none;padding:5px 9px;border:1px solid #22406e;border-radius:8px}
.body{flex:1;min-height:0;position:relative}
iframe{width:100%;height:100%;border:0;background:#fff}
.static{padding:22px 26px;max-width:760px;overflow:auto;height:100%}
.card{background:#0e1320;border:1px solid #1b2230;border-radius:12px;padding:16px 18px;margin:12px 0}
.mut{color:#9aa3b5;font-size:13px;line-height:1.6}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #1b2230}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;background:#12294a;color:#8ab4f8}
h2{font-size:15px;margin:2px 0 8px}
</style></head><body>
<div class="app">
  <aside>
    <div class="brand">Trading desk</div>
    <div class="grp">General</div>
    <div class="nav" data-r="gen/pnl"><span class="dot"></span> P&amp;L</div>
    <div class="nav" data-r="gen/regime"><span class="dot"></span> Regime</div>
    <div class="nav" data-r="gen/monitor"><span class="dot"></span> Monitor</div>
    <div class="nav" data-r="gen/news"><span class="dot"></span> News</div>
    <div class="nav" data-r="gen/income"><span class="dot"></span> Income</div>
    <div class="grp">Strategies</div>
    <div class="nav live" data-r="ab"><span class="dot"></span> A/B</div>
    <div class="nav live" data-r="c"><span class="dot"></span> C</div>
    <div class="nav live" data-r="f"><span class="dot"></span> F</div>
    <div class="nav live" data-r="orb"><span class="dot"></span> ORB</div>
  </aside>
  <main>
    <header>
      <h1 id="ttl">A/B</h1><span class="sub" id="sub"></span>
      <div class="tabs" id="tabs"></div>
      <a class="open" id="open" href="#" target="_blank" rel="noopener" style="display:none">open &#8599;</a>
    </header>
    <div class="body"><iframe id="frame" title="view"></iframe><div class="static" id="static" style="display:none"></div></div>
  </main>
</div>
<script>
var F="__F__", ORB="__ORB__";
var GEN={
 'pnl':{t:'P&L',frame:'/pnl'}, 'regime':{t:'Regime',frame:'/regime'}, 'monitor':{t:'Monitor',frame:'/monitor'},
 'news':{t:'News',html:'<h2>News &amp; high-impact suppression</h2><div class="card mut">The agent pulls the ForexFactory high-impact calendar (CPI, NFP, FOMC, PCE, ISM, PPI, GDP, Powell). Trades within &plusmn;30 min are flagged. <b>NO_TRADE_SUPPRESS=1</b> mutes them entirely - recommended ON for funded accounts, off for research.</div>'},
 'income':{t:'Income',html:'<h2>Income &amp; scaling</h2><div class="card mut">Funded-account scaling toward the weekly target across multi-firm accounts. <b>Gate 0 for every strategy: prove &ge; +0.15R over 30-50 live trades before sizing up.</b> Keep BE for eval, 0.5% risk.</div>'}
};
var SETTINGS_AB='<h2>A/B settings (Railway env)</h2><div class="card"><table>'+
 [['DISPWIN','30','bars to find the impulse'],['MODE','confirm','confirm | sweep'],['DISP_MODE','chain','chain (&ge;3) | orig (1-3)'],['ALLOW_SINGLE','off','admit single big candle (tested = wash)'],['MAX_STOP_R','40','drop wider stops (pts)'],['NO_TRADE_SUPPRESS','1','mute &plusmn;30min around high-impact news (ON)']]
 .map(function(r){return '<tr><td><span class="pill">'+r[0]+'</span></td><td>'+r[1]+'</td><td class="mut">'+r[2]+'</td></tr>';}).join('')+'</table></div>';
var STRAT={
 ab:{name:'A/B',sub:'Displacement \\u2192 FVG \\u2192 50% hold \\u2192 BOS',tabs:[['performance','Performance','/performance'],['how','How it works','/how'],['candidates','Candidates','/candidates'],['settings','Settings',{html:SETTINGS_AB}]]},
 c:{name:'C',sub:'Staircase displacement \\u2192 rejection \\u2192 BOS',tabs:[['dash','Dashboard','/c'],['how','How it works','/c/how']]},
 f:{name:'F',sub:'First-presentation FVG \\u00b7 momentum',ext:F,tabs:[['cand','Candidates',F+'/candidates'],['log','Log',F+'/log'],['perf','Performance',F+'/performance_f']]},
 orb:{name:'ORB',sub:'Opening-range breakout \\u00b7 momentum',ext:ORB,tabs:[['dash','Dashboard',ORB+'/'],['how','How it works',ORB+'/how']]}
};
var frame=document.getElementById('frame'), stat=document.getElementById('static'),
    tabsEl=document.getElementById('tabs'), openEl=document.getElementById('open'),
    ttl=document.getElementById('ttl'), sub=document.getElementById('sub');
function showFrame(src){stat.style.display='none';frame.style.display='block';frame.src=src;openEl.style.display='inline-block';openEl.href=src;}
function showHtml(html){frame.style.display='none';frame.removeAttribute('src');stat.style.display='block';stat.innerHTML=html;openEl.style.display='none';}
function setActive(top){document.querySelectorAll('.nav').forEach(function(n){n.classList.toggle('on',n.dataset.r.split('/')[0]===top);});}
function render(){
 var h=(location.hash.replace(/^#\\//,'')||'gen/pnl'); var p=h.split('/'); var top=p[0]; setActive(top);
 tabsEl.innerHTML='';
 if(top==='gen'){
   var g=GEN[p[1]||'pnl']; ttl.textContent=g.t; sub.textContent='General';
   if(g.frame) showFrame(g.frame); else showHtml(g.html); return;
 }
 var s=STRAT[top]; if(!s){location.hash='#/ab';return;}
 ttl.textContent=s.name; sub.textContent=s.sub.replace(/\\\\u[0-9a-f]{4}/g,function(m){return String.fromCharCode(parseInt(m.slice(2),16));});
 var cur=p[1]||s.tabs[0][0];
 s.tabs.forEach(function(t){
   var d=document.createElement('div'); d.className='tab'+(t[0]===cur?' on':''); d.textContent=t[1];
   d.onclick=function(){location.hash='#/'+top+'/'+t[0];}; tabsEl.appendChild(d);
 });
 var tab=s.tabs.filter(function(t){return t[0]===cur;})[0]||s.tabs[0];
 var target=tab[2];
 if(typeof target==='object'&&target.html) showHtml(target.html); else showFrame(target);
}
document.querySelectorAll('.nav').forEach(function(n){n.onclick=function(){location.hash='#/'+n.dataset.r;};});
window.addEventListener('hashchange',render); render();
</script></body></html>"""


def render_home():
    return PAGE.replace('__F__', _F).replace('__ORB__', _ORB)


def register(app, path='/'):
    """Add the unified home page at `path` (default '/'). Isolated: adds one route only."""
    try:
        from flask import Response
    except Exception:
        return app
    def _dash_home():
        return Response(render_home(), mimetype='text/html')
    app.add_url_rule(path, 'dash_home', _dash_home)
    return app


if __name__ == '__main__':
    open('dashboard_preview.html', 'w').write(render_home())
    print('wrote dashboard_preview.html (', len(render_home()), 'bytes )')
