#!/usr/bin/env python3
"""
dashboard.py - the unified home page served at the agent root "/".

ISOLATED add-on (same pattern as pnl.py / how_ab.py): adds ONE GET "/" route that returns a nav
shell FEDERATING the pages that already exist. It runs no detector / intake / journal code.

  General   : P&L=/pnl - Regime=/regime - Monitor=/monitor - News (static) - Income (static)
  A/B       : Candidates=/candidates - Journal=/journal - How=/how
  C         : Dashboard=/c - How=/c/how
  F  (ext)  : How (inline) - Candidates - Log - Performance
  ORB (ext) : Dashboard - How

Wire into agent.py (next to pnl.register):  import dashboard ; dashboard.register(app)
Env (optional): STRAT_F_URL, STRAT_ORB_URL.
"""
import os

_F   = os.environ.get('STRAT_F_URL',  'https://strategy-f-production.up.railway.app').rstrip('/')
_ORB = os.environ.get('STRAT_ORB_URL', 'https://strategy-orb-production.up.railway.app').rstrip('/')

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading desk</title>
<style>
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;background:#0b0e14;color:#e6e9ef;font-family:system-ui,Segoe UI,Roboto,sans-serif}
.app{display:grid;grid-template-columns:214px 1fr;height:100vh}
aside{background:#0e1320;border-right:1px solid #1b2230;padding:14px 10px;overflow:auto}
.brand{font-size:15px;font-weight:700;padding:4px 10px 8px;display:flex;align-items:center;gap:8px}
.grp{font-size:11px;color:#6b7688;text-transform:uppercase;letter-spacing:.05em;padding:12px 10px 4px}
.nav{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:#828a99;cursor:pointer;font-size:14px}
.nav:hover{background:#141a28;color:#e6e9ef}
.nav.on{background:#13251b;color:#4ade80;box-shadow:inset 3px 0 0 #4ade80}
html[data-framed] aside,html[data-framed] header{display:none}
html[data-framed] .app{grid-template-columns:1fr}
.menubtn{background:#141a28;color:#9aa3b5;border:1px solid #1b2230;border-radius:8px;padding:6px 11px;cursor:pointer;font-size:15px;line-height:1;display:inline-flex;align-items:center}
.menubtn:hover{color:#e6e9ef;border-color:#2a3550}
body.nomenu aside{display:none}
body.nomenu .app{grid-template-columns:1fr}
.ic{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;flex:0 0 16px}
.ic svg{width:16px;height:16px;display:block}
main{display:flex;flex-direction:column;min-width:0}
header{padding:12px 20px;border-bottom:1px solid #1b2230}
.hrow{display:flex;align-items:center;gap:10px}
h1{font-size:18px;margin:0}.sub{color:#6b7688;font-size:13px}
.open{margin-left:auto;color:#8ab4f8;font-size:12px;text-decoration:none;padding:5px 10px;border:1px solid #22406e;border-radius:8px;display:inline-flex;align-items:center;gap:6px}
.tabs{display:flex;gap:6px;margin-top:11px;flex-wrap:wrap}
.tab{display:inline-flex;align-items:center;gap:7px;padding:7px 12px;border-radius:8px;color:#9aa3b5;cursor:pointer;font-size:13px;border:1px solid #1b2230}
.tab:hover{color:#e6e9ef;border-color:#2a3550}
.tab.on{background:#13251b;color:#4ade80;border-color:#1f7a41}
.body{flex:1;min-height:0;position:relative}
iframe{width:100%;height:100%;border:0;background:#fff}
.static{padding:22px 26px;max-width:780px;overflow:auto;height:100%}
.card{background:#0e1320;border:1px solid #1b2230;border-radius:12px;padding:16px 18px;margin:12px 0}
.mut{color:#9aa3b5;font-size:13px;line-height:1.65}
h2{font-size:15px;margin:2px 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #1b2230}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;background:#12294a;color:#8ab4f8}
ol{line-height:1.7;padding-left:20px} ol li{margin:6px 0} b{color:#fff}
</style></head><body>
<div class="app">
  <aside>
    <div class="brand"><span class="ic" id="brandic"></span> Trading desk</div>
    <div id="sidenav"></div>
  </aside>
  <main>
    <header>
      <div class="hrow"><button class="menubtn" onclick="toggleMenu()" title="Hide/show menu" aria-label="Hide or show menu">&#9776;</button><h1 id="ttl">P&amp;L</h1><span class="sub" id="sub"></span>
        <a class="open" id="open" href="#" target="_blank" rel="noopener" style="display:none"></a></div>
      <div class="tabs" id="tabs"></div>
    </header>
    <div class="body"><iframe id="frame" title="view"></iframe><div class="static" id="static" style="display:none"></div></div>
  </main>
</div>
<script>
if(window.self!==window.top){document.documentElement.setAttribute('data-framed','1');}
function toggleMenu(){document.body.classList.toggle('nomenu');try{localStorage.setItem('deskmenu',document.body.classList.contains('nomenu')?'0':'1');}catch(e){}}
try{if(localStorage.getItem('deskmenu')==='0')document.body.classList.add('nomenu');}catch(e){}
var F="__F__", ORB="__ORB__";
var S='viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
var ICONS={
 pnl:'<svg '+S+'><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
 regime:'<svg '+S+'><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
 monitor:'<svg '+S+'><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
 news:'<svg '+S+'><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>',
 income:'<svg '+S+'><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>',
 ab:'<svg '+S+'><rect x="5" y="8" width="5" height="8"/><path d="M7.5 4v4M7.5 16v4"/><rect x="14" y="6" width="5" height="10"/><path d="M16.5 2v4M16.5 16v4"/></svg>',
 c:'<svg '+S+'><path d="M4 20h4v-4h4v-4h4V8h4"/></svg>',
 f:'<svg '+S+'><path d="M3 17l6-6 4 4 8-8"/></svg>',
 orb:'<svg '+S+'><rect x="3" y="3" width="18" height="18" rx="2"/></svg>',
 list:'<svg '+S+'><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>',
 book:'<svg '+S+'><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
 help:'<svg '+S+'><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>',
 chart:'<svg '+S+'><path d="M3 3v18h18"/><path d="M7 15l3-3 3 3 4-5"/></svg>',
 grid:'<svg '+S+'><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
 file:'<svg '+S+'><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8"/></svg>',
 cog:'<svg '+S+'><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.2V21a2 2 0 0 1-4 0a1.6 1.6 0 0 0-2.7-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3 1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8 1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4a1.6 1.6 0 0 0-1.5 1z"/></svg>'
};
function ic(n){return '<span class="ic">'+(ICONS[n]||'')+'</span>';}

var F_HOW='<h2>Strategy F - first-presentation FVG</h2>'+
 '<div class="card mut">A momentum-continuation read on the first clean fair-value gap of the session.<ol>'+
 '<li><b>First-presentation FVG.</b> The first clean FVG of the session in the trend direction.</li>'+
 '<li><b>First touch.</b> Price returns to that gap for the first time - the entry.</li>'+
 '<li><b>Order.</b> Limit at the gap, fixed R target.</li></ol></div>'+
 '<div class="card mut">Honest note (from the F audit): the FVG mechanics are right but F lacks bias / draw-on-liquidity / premium-discount - its edge is <b>momentum continuation, not textbook ICT</b>. Realistic additive &asymp; +0.045R. Its own service has the live candidates / log / performance.</div>';

var SETTINGS_AB='<h2>A/B settings (Railway env)</h2><div class="card"><table>'+
 [['DISPWIN','30','bars to find the impulse'],['MODE','confirm','confirm | sweep'],['DISP_MODE','chain','chain (&ge;3) | orig (1-3)'],['ALLOW_SINGLE','off','single big candle (tested = wash)'],['MAX_STOP_R','40','drop wider stops (pts)'],['NO_TRADE_SUPPRESS','1','mute &plusmn;30min around high-impact news (ON)']]
 .map(function(r){return '<tr><td><span class="pill">'+r[0]+'</span></td><td>'+r[1]+'</td><td class="mut">'+r[2]+'</td></tr>';}).join('')+'</table></div>';

var GEN={
 'pnl':{t:'P&L',frame:'/pnl'}, 'regime':{t:'Regime',frame:'/regime'}, 'monitor':{t:'Monitor',frame:'/monitor'},
 'news':{t:'News',html:'<h2>News &amp; high-impact suppression</h2><div class="card mut">The agent pulls the ForexFactory high-impact calendar (CPI, NFP, FOMC, PCE, ISM, PPI, GDP, Powell). Trades within &plusmn;30 min are flagged. <b>NO_TRADE_SUPPRESS=1</b> mutes them entirely - ON for funded accounts.</div>'},
 'income':{t:'Income',html:'<h2>Income &amp; scaling</h2><div class="card mut">Funded-account scaling toward the weekly target across multi-firm accounts. <b>Gate 0 for every strategy: prove &ge; +0.15R over 30-50 live trades before sizing up.</b></div>'}
};
var STRAT={
 ab:{name:'A/B',sub:'Displacement → FVG → 50% hold → BOS',tabs:[['candidates','Candidates','/candidates','list'],['journal','Journal','/journal','book'],['how','How it works','/how','help'],['settings','Settings',{html:SETTINGS_AB},'cog']]},
 c:{name:'C',sub:'Staircase displacement → rejection → BOS',tabs:[['dash','Dashboard','/c','grid'],['how','How it works','/c/how','help']]},
 f:{name:'F',sub:'Displacement → FVG → first touch · momentum',ext:F,tabs:[['how','How it works',F+'/how','help'],['cand','Candidates',F+'/candidates','list'],['log','Log',F+'/log','file'],['perf','Performance',F+'/performance_f','chart']]},
 orb:{name:'ORB',sub:'Opening-range breakout · momentum',ext:ORB,tabs:[['how','How it works',ORB+'/how','help'],['dash','Dashboard',ORB+'/','grid']]}
};
var NAV=[['General',[['gen/pnl','P&L','pnl'],['gen/regime','Regime','regime'],['gen/monitor','Monitor','monitor'],['gen/news','News','news'],['gen/income','Income','income']]],
         ['Strategies',[['ab','A/B','ab'],['c','C','c'],['f','F','f'],['orb','ORB','orb']]]];

var frame=document.getElementById('frame'), stat=document.getElementById('static'),
    tabsEl=document.getElementById('tabs'), openEl=document.getElementById('open'),
    ttl=document.getElementById('ttl'), sub=document.getElementById('sub');
document.getElementById('brandic').innerHTML=ICONS.chart;
function showFrame(src){stat.style.display='none';frame.style.display='block';frame.src=src;openEl.style.display='inline-flex';openEl.href=src;openEl.innerHTML='open '+'↗';}
function showHtml(html){frame.style.display='none';frame.removeAttribute('src');stat.style.display='block';stat.innerHTML=html;openEl.style.display='none';}
function setActive(top){document.querySelectorAll('.nav').forEach(function(n){n.classList.toggle('on',n.dataset.r.split('/')[0]===top);});}
function buildNav(){
 var h='';
 NAV.forEach(function(g){ h+='<div class="grp">'+g[0]+'</div>';
   g[1].forEach(function(n){ h+='<div class="nav'+(g[0]==='Strategies'?'':'')+'" data-r="'+n[0]+'">'+ic(n[2])+'<span>'+n[1]+'</span></div>'; }); });
 document.getElementById('sidenav').innerHTML=h;
 document.querySelectorAll('.nav').forEach(function(n){n.onclick=function(){location.hash='#/'+n.dataset.r;};});
}
function render(){
 var hh=(location.hash.replace(/^#\//,'')||'gen/pnl'); var p=hh.split('/'); var top=p[0]; setActive(top); tabsEl.innerHTML='';
 if(top==='gen'){ var g=GEN[p[1]||'pnl']; ttl.textContent=g.t; sub.textContent='General'; if(g.frame) showFrame(g.frame); else showHtml(g.html); return; }
 var s=STRAT[top]; if(!s){location.hash='#/ab';return;}
 ttl.textContent=s.name; sub.textContent=s.sub;
 var cur=p[1]||s.tabs[0][0];
 s.tabs.forEach(function(t){
   var d=document.createElement('div'); d.className='tab'+(t[0]===cur?' on':''); d.innerHTML=ic(t[3])+'<span>'+t[1]+'</span>';
   d.onclick=function(){location.hash='#/'+top+'/'+t[0];}; tabsEl.appendChild(d);
 });
 var tab=s.tabs.filter(function(t){return t[0]===cur;})[0]||s.tabs[0]; var target=tab[2];
 if(typeof target==='object'&&target.html) showHtml(target.html); else showFrame(target);
}
buildNav(); window.addEventListener('hashchange',render); render();
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
