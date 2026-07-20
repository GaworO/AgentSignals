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
_AMD = os.environ.get('STRAT_AMD_URL', 'https://strategy-amd-production.up.railway.app').rstrip('/')
# --- Forex observe-only services (public URLs of forex-eur / forex-jpy) ---
_EUR = os.environ.get('FX_EUR_URL', 'https://forex-eur-production.up.railway.app').rstrip('/')
_JPY = os.environ.get('FX_JPY_URL', 'https://forex-jpy-production.up.railway.app').rstrip('/')

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
.autocard{display:block;margin:14px 8px 6px;padding:11px 12px;border-radius:10px;background:#0f1626;border:1px solid #1b2230;text-decoration:none;color:#e6e9ef}
.autocard:hover{border-color:#2a3550}
.autocard .ah{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:700;margin-bottom:7px}
.autocard .dot{width:8px;height:8px;border-radius:50%}
.autocard .mode{font-size:10px;padding:1px 7px;border-radius:8px;font-weight:700;margin-left:auto}
.autocard .track{height:7px;background:#1b2230;border-radius:4px;overflow:hidden;margin:5px 0 7px}
.autocard .fill{height:100%;border-radius:4px}
.autocard .kv{display:flex;justify-content:space-between;font-size:11px;color:#9aa3b5;margin:2px 0}
.autocard .kv b{color:#e6e9ef;font-variant-numeric:tabular-nums}
</style></head><body>
<div class="app">
  <aside>
    <div class="brand"><span class="ic" id="brandic"></span> Trading desk</div>
    <div id="sidenav"></div>
    <a id="autocard" class="autocard" href="#/gen/guard" title="Open the Auto-Executor guard"></a>
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
var F="__F__", ORB="__ORB__", EUR="__EUR__", JPY="__JPY__", AMD="__AMD__";
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
 amd:'<svg '+S+'><circle cx="12" cy="12" r="9"/><line x1="12" y1="1" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="1" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="23" y2="12"/></svg>',
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

var AMD_HOW='<h2>Strategy AMD - Accumulation → Manipulation → Distribution</h2>'+
 '<div class="card mut">NY-PM short only, on days whose morning was a genuine accumulation.<ol>'+
 '<li><b>Accumulation.</b> Morning (08:00-12:00 ET) range &le; 1.2&times; its own 20-day average.</li>'+
 '<li><b>Manipulation.</b> In NY-PM, a sweep of a significant-swing / session high (equal-highs excluded).</li>'+
 '<li><b>Distribution.</b> Aligned HTF FVG &rarr; 1m IFVG &rarr; CISD &rarr; SHORT. 2R target, BE@1R.</li></ol></div>'+
 '<div class="card mut">Backtest 4yr: <b>+0.44R, PF 2.39, t=3.21, ~21 trades/yr, 5/5 positive years</b>, maxDD -5.2R. A reversal engine that only works in a reversal session - NY-AM / London ports were tested and dead. Own service = live candidates / journal / Gate-0.</div>';

var COMPARE_HTML='<h2>Strategy scorecard - timing, win rate &amp; frequency</h2>'+
 '<div class="card mut">Real trade logs, 4yr MNQ. Your strategies <b>tile the trading day</b>; AMD owns the NY-PM slot with the #2 per-trade edge. Sorted by expectancy. (A/B: log=6,569 signals, ~1,181 finals/yr traded.)</div>'+
 '<div class="card"><table>'+
 '<tr><th>Strategy</th><th>When / session</th><th>Win%</th><th>Exp R</th><th>PF</th><th>/yr</th><th>/mo</th><th>/wk</th></tr>'+
 '<tr><td><b>Model C</b></td><td>NY-AM + PREM</td><td>50%</td><td><b>+0.68</b></td><td>3.30</td><td>14</td><td>1.2</td><td>0.27</td></tr>'+
 '<tr><td><b>AMD</b></td><td>NY-PM 13:30-16:00</td><td>47%</td><td><b>+0.44</b></td><td>2.39</td><td>20</td><td>1.7</td><td>0.39</td></tr>'+
 '<tr><td>S2 Up-Gap Fade</td><td>Gap days (RTH open)</td><td>54%</td><td>+0.32</td><td>2.58</td><td>9</td><td>0.7</td><td>0.17</td></tr>'+
 '<tr><td>ORB</td><td>RTH open 09:30</td><td>46%</td><td>+0.23</td><td>1.46</td><td>122</td><td>10.2</td><td>2.34</td></tr>'+
 '<tr><td>S1 Monday-Rebuy</td><td>Mondays (RTH)</td><td>62%</td><td>+0.22</td><td>2.68</td><td>16</td><td>1.3</td><td>0.31</td></tr>'+
 '<tr><td>A/B</td><td>All sessions</td><td>31%</td><td>+0.20</td><td>1.50</td><td>293</td><td>24.4</td><td>5.64</td></tr>'+
 '</table></div>'+
 '<div class="card mut"><b>Best session per strategy:</b> Model C &rarr; NY-AM (+0.84) &middot; A/B &rarr; London (+0.28) / PREM (+0.25) &middot; AMD &rarr; NY-PM (+0.44) &middot; ORB/S2 &rarr; the open &middot; S1 &rarr; Mondays.</div>'+'<h2>A/B forward &mdash; $100k @ 0.5% risk, last 12 months (ATRMULT 1.0)</h2>'+'<div class="card mut">Modeled net R per calendar month, $500 = 1R. Broad detector (all catalysts); realistically executable (~1/day) is a fraction of this. 2025-06 &amp; 2026-06 partial.</div>'+'<div class="card"><table>'+'<tr><th>Month</th><th>Net R</th><th>Net $</th></tr>'+'<tr><td>2025-06 (part)</td><td style=\"color:#17864a\">+36.4</td><td style=\"color:#17864a\">+$18,217</td></tr>'+'<tr><td>2025-07</td><td style=\"color:#c0392b\">-19.3</td><td style=\"color:#c0392b\">-$9,661</td></tr>'+'<tr><td>2025-08</td><td style=\"color:#17864a\">+45.3</td><td style=\"color:#17864a\">+$22,635</td></tr>'+'<tr><td>2025-09</td><td style=\"color:#c0392b\">-7.8</td><td style=\"color:#c0392b\">-$3,917</td></tr>'+'<tr><td>2025-10</td><td style=\"color:#17864a\">+72.3</td><td style=\"color:#17864a\">+$36,151</td></tr>'+'<tr><td>2025-11</td><td style=\"color:#17864a\">+33.9</td><td style=\"color:#17864a\">+$16,947</td></tr>'+'<tr><td>2025-12</td><td style=\"color:#17864a\">+56.8</td><td style=\"color:#17864a\">+$28,381</td></tr>'+'<tr><td>2026-01</td><td style=\"color:#17864a\">+39.3</td><td style=\"color:#17864a\">+$19,634</td></tr>'+'<tr><td>2026-02</td><td style=\"color:#17864a\">+8.7</td><td style=\"color:#17864a\">+$4,347</td></tr>'+'<tr><td>2026-03</td><td style=\"color:#c0392b\">-12.4</td><td style=\"color:#c0392b\">-$6,222</td></tr>'+'<tr><td>2026-04</td><td style=\"color:#17864a\">+21.2</td><td style=\"color:#17864a\">+$10,619</td></tr>'+'<tr><td>2026-05</td><td style=\"color:#17864a\">+40.9</td><td style=\"color:#17864a\">+$20,443</td></tr>'+'<tr><td>2026-06 (part)</td><td style=\"color:#17864a\">+8.7</td><td style=\"color:#17864a\">+$4,348</td></tr>'+'<tr><td><b>TOTAL</b></td><td style="color:#17864a"><b>+323.8</b></td><td style="color:#17864a"><b>+$161,921</b></td></tr>'+'</table></div>';

var SETTINGS_AB='<h2>A/B settings (Railway env)</h2><div class="card"><table>'+
 [['DISPWIN','30','bars to find the impulse'],['MODE','confirm','confirm | sweep'],['DISP_MODE','chain','chain (&ge;3) | orig (1-3)'],['ALLOW_SINGLE','off','single big candle (tested = wash)'],['MAX_STOP_R','40','drop wider stops (pts)'],['NO_TRADE_SUPPRESS','1','mute &plusmn;30min around high-impact news (ON)']]
 .map(function(r){return '<tr><td><span class="pill">'+r[0]+'</span></td><td>'+r[1]+'</td><td class="mut">'+r[2]+'</td></tr>';}).join('')+'</table></div>';

var GEN={
 'alltrades':{t:'All trades',frame:'/all/trades'}, 'allcands':{t:'All candidates',frame:'/all/candidates'}, 'reconcile':{t:'Reconcile',frame:'/all/reconcile'},
 'pnl':{t:'P&L',frame:'/pnl'}, 'regime':{t:'Regime',frame:'/regime'}, 'monitor':{t:'Monitor',frame:'/monitor'},
 'news':{t:'News',html:'<h2>News &amp; high-impact suppression</h2><div class="card mut">The agent pulls the ForexFactory high-impact calendar (CPI, NFP, FOMC, PCE, ISM, PPI, GDP, Powell). Trades within &plusmn;30 min are flagged. <b>NO_TRADE_SUPPRESS=1</b> mutes them entirely - ON for funded accounts.</div>'},
 'income':{t:'Income',html:'<h2>Income &amp; scaling</h2><div class="card mut">Funded-account scaling toward the weekly target across multi-firm accounts. <b>Gate 0 for every strategy: prove &ge; +0.15R over 30-50 live trades before sizing up.</b></div>'},
 'compare':{t:'Compare',html:COMPARE_HTML},
 'shadow':{t:'Shadow Executor',frame:'/shadow'}, 'guard':{t:'Auto-Executor Guard',frame:'/guard'}
};
var FX_NOTE='<h2>Forex - observe only</h2>'+
 '<div class="card mut">Same detector as A/B (displacement &rarr; FVG &rarr; 50% hold &rarr; BOS), volatility-recalibrated per instrument. <b>Alert-only</b> - no execution, no contact with the MNQ agent. Fed live 1-min bars from TradingView.</div>'+
 '<div class="card mut"><b>Summary = would-be results.</b> Every setup the detector confirms is modeled to its outcome (win +2R / breakeven 0 / loss -1R): <b>n</b> = how many trades would have been taken, <b>total_R</b> = would-be P&L in R (&times; your risk-per-trade = money), <b>exp_R</b> = per-trade edge, <b>win_pct</b> = hit rate. Backtest reference: EUR/USD +0.23R, USD/JPY +0.18R net.</div>'+
 '<div class="card mut"><b>Candidates &amp; setups</b> = the live funnel right now (which levels armed, which displaced, which confirmed). In-sample backtest; live fills are the open question.</div>';
var STRAT={
 ab:{name:'A/B',sub:'Displacement → FVG → 50% hold → BOS',tabs:[['candidates','Candidates','/candidates','list'],['trades','Trades','/outcomes','book'],['pine','Pine for TV','/pine','file'],['journal','Journal','/journal','book'],['how','How it works','/how','help'],['settings','Settings',{html:SETTINGS_AB},'cog']]},
 c:{name:'C',sub:'Staircase displacement → rejection → BOS',tabs:[['dash','Dashboard','/c','grid'],['how','How it works','/c/how','help']]},
 f:{name:'F',sub:'Displacement → FVG → first touch · momentum',ext:F,tabs:[['how','How it works',F+'/how','help'],['cand','Candidates',F+'/candidates','list'],['log','Log',F+'/log','file'],['perf','Performance',F+'/performance_f','chart']]},
 orb:{name:'ORB',sub:'Opening-range breakout · momentum',ext:ORB,tabs:[['how','How it works',ORB+'/how','help'],['dash','Dashboard',ORB+'/','grid']]},
 amd:{name:'AMD',sub:'Accumulation → Manipulation → Distribution · NY-PM short',ext:AMD,tabs:[['how','How it works',{html:AMD_HOW},'help'],['dash','Dashboard',AMD+'/','grid'],['stats','Gate 0',AMD+'/stats','chart']]},
 eur:{name:'EUR/USD',sub:'Forex · observe only · EURUSD-calibrated',ext:EUR,tabs:[['sum','Summary',EUR+'/performance','chart'],['trades','Trades',EUR+'/outcomes','book'],['pine','Pine for TV',EUR+'/pine','file'],['cand','Candidates & setups',EUR+'/candidates','list'],['status','Status',EUR+'/status','grid'],['about','About',{html:FX_NOTE},'help']]},
 jpy:{name:'USD/JPY',sub:'Forex · observe only · JPY-calibrated (×100)',ext:JPY,tabs:[['sum','Summary',JPY+'/performance','chart'],['trades','Trades',JPY+'/outcomes','book'],['pine','Pine for TV',JPY+'/pine','file'],['cand','Candidates & setups',JPY+'/candidates','list'],['status','Status',JPY+'/status','grid'],['about','About',{html:FX_NOTE},'help']]},
 fx:{name:'Forex - joined P&L',sub:'EUR/USD + USD/JPY combined · observe only · separate from MNQ',tabs:[['pnl','Joined P&L','/forexpnl','chart'],['eurp','EUR/USD perf',EUR+'/performance','chart'],['jpyp','USD/JPY perf',JPY+'/performance','chart']]},
 fxg:{name:'Forex - Auto-Executor',sub:'EUR/USD + USD/JPY joined guard · each pair has its own counters & kill-latch',tabs:[['joined','Joined','/fxguard','grid'],['eurg','EUR/USD guard',EUR+'/guard','grid'],['jpyg','USD/JPY guard',JPY+'/guard','grid'],['eurh','EUR health',EUR+'/guard/health?format=txt','help'],['jpyh','JPY health',JPY+'/guard/health?format=txt','help']]}
};
var NAV=[['General',[['gen/alltrades','All trades','book'],['gen/allcands','All candidates','list'],['gen/reconcile','Reconcile','book'],['gen/pnl','P&L','pnl'],['gen/regime','Regime','regime'],['gen/monitor','Monitor','monitor'],/* hidden: ['gen/news','News','news'],['gen/income','Income','income'], */['gen/guard','Auto-Executor','grid']]],
         ['Strategies',[['ab','A/B','ab'],['c','C','c'],['f','F','f'],['orb','ORB','orb'],['amd','AMD','amd']]],
         ['Forex (observe)',[['fx','P&L (joined)','pnl'],['fxg','Auto-Executor','grid'],['eur','EUR/USD','chart'],['jpy','USD/JPY','chart']]]];

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
(function(){
  function col(m){return m=='auto'?'#4ade80':m=='manual'?'#7ab8f5':'#e0a93b';}
  function loadAuto(){fetch('/guard/data',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    var e=d.eval||{},m=d.mode||'off',c=col(m),ac=document.getElementById('autocard');if(!ac)return;
    var pct=Math.max(3,Math.min(100,e.pct||0)),bc=e.breached?'#f87171':e.passed?'#4ade80':((e.pnl||0)<0?'#e0a93b':'#7ab8f5');
    var st=e.passed?'PASS ✓':e.breached?'BREACH ✕':(d.kill?'HALTED':'armed');
    ac.innerHTML='<div class=ah><span class=dot style="background:'+c+'"></span>Auto-Executor'+
      '<span class=mode style="background:'+c+'22;color:'+c+'">'+String(m).toUpperCase()+'</span></div>'+
      '<div class=track><div class=fill style="width:'+pct+'%;background:'+bc+'"></div></div>'+
      '<div class=kv><span>Eval &rarr; $106k</span><b>'+(e.pct||0)+'% &middot; '+st+'</b></div>'+
      '<div class=kv><span>P&amp;L</span><b>'+((e.pnl||0)>=0?'+$':'-$')+Math.abs(e.pnl||0).toLocaleString()+'</b></div>'+
      '<div class=kv><span>Today</span><b>'+(d.trades||0)+'/'+(d.max_trades||3)+' &middot; '+(d.losses||0)+'L</b></div>';
  }).catch(function(){});}
  loadAuto(); setInterval(loadAuto,30000);
})();
</script></body></html>"""


SHADOW_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shadow executor</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b0e14;color:#e6e9ef;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;padding:14px}
h2{margin:0 0 3px}.mut{color:#828a99}.sub{color:#828a99;font-size:12.5px;margin-bottom:12px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.kpi{background:#141a28;border:1px solid #1b2230;border-radius:10px;padding:11px 15px;min-width:150px}
.kl{color:#828a99;font-size:12px;margin-bottom:3px}.kv{font-size:21px;font-weight:700}
.kv.up{color:#4ade80}.kv.down{color:#f87171}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:#141a28;border:1px solid #1b2230;border-radius:10px;padding:10px 13px;margin-bottom:12px}
.bar .grp{display:flex;gap:9px;align-items:center}.bar b{font-size:12px;color:#9aa3b5;margin-right:2px}
label.cb{display:inline-flex;gap:5px;align-items:center;cursor:pointer;padding:4px 9px;border:1px solid #2a3550;border-radius:7px;font-size:13px}
label.cb input{accent-color:#4ade80}
select,input[type=date]{background:#0b0e14;color:#e6e9ef;border:1px solid #2a3550;border-radius:7px;padding:6px 9px;font-size:13px}
input[type=date]::-webkit-calendar-picker-indicator{filter:invert(.7)}
button{background:#13251b;color:#4ade80;border:1px solid #1f7a41;border-radius:7px;padding:7px 13px;font-size:13px;cursor:pointer;font-weight:600}
button:hover{background:#173021}#cpstat{color:#4ade80;font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #1b2230;white-space:nowrap}
th{color:#828a99;font-weight:600;position:sticky;top:0;background:#0b0e14}
.win{color:#4ade80}.loss{color:#f87171}.be{color:#fbbf24}.sAB{color:#60a5fa}.sC{color:#fb923c}.sF{color:#4ade80}
tr.seed td{opacity:.60}.tag{font-size:10px;color:#6b7280;border:1px solid #2a3550;border-radius:4px;padding:0 5px}
.wrap{max-height:52vh;overflow:auto;border:1px solid #1b2230;border-radius:10px}
.note{background:#141a28;border:1px solid #1b2230;border-left:3px solid #f59e0b;border-radius:8px;padding:9px 12px;color:#9aa3b5;font-size:12.5px;margin-bottom:12px}
</style></head><body>
<h2>Shadow executor <span class="mut" style="font-size:14px">&middot; no money, proving the edge</span></h2>
<div class="sub">Every fired signal logged <b>hands-off</b>, no money &middot; $100,000 @ 0.5% ($500 = 1R) &middot; resting limits, <b>fixed stop, target 2R</b> &middot; London &amp; Asia excluded.</div>
<div class="note"><b>The automation test.</b> Every A/B signal is logged the instant it fires &mdash; <b>whether or not you take it</b> &mdash; and resolved on your live bars: fill the resting limit &rarr; <b>fixed stop at SL / target 2R</b> &rarr; <b>WIN +2R</b> / <b>LOSS &minus;1R</b>. Starts empty, fills forward. The <b>Auto vs You</b> panel compares taking <i>every</i> signal hands-off against the trades you <i>actually</i> took &mdash; the answer to "would full-auto beat my manual trading?". Gate-0: prove &ge; +0.15R over 30-50 fills before real size.</div>
<div style="font-size:12.5px;color:#9aa3b5;margin:2px 0 6px"><b>Auto vs You</b> &mdash; taking every signal hands-off (fixed 2R) vs the trades you actually took (from /pnl):</div>
<div class="kpis" id="cmp"></div>
<div style="font-size:12.5px;color:#9aa3b5;margin:10px 0 6px"><b>Auto book</b> &mdash; the shadow log (all signals, hands-off):</div>
<div class="kpis" id="sum"></div>
<div class="bar">
  <div class="grp"><b>strategies</b>
    <label class="cb"><input type="checkbox" class="stratcb" value="A/B" checked> A/B</label>
    <label class="cb"><input type="checkbox" class="stratcb" value="C" checked> C</label>
    <label class="cb"><input type="checkbox" class="stratcb" value="F" checked> F</label>
  </div>
  <div class="grp"><b>week</b><select id="wk"></select></div>
  <div class="grp"><b>dates</b><input type="date" id="dfrom" title="from"><span class="mut">&ndash;</span><input type="date" id="dto" title="to"><span class="mut" id="dclear" style="cursor:pointer;padding:0 4px" title="clear date range">&times;</span></div>
  <div class="grp"><button id="cp">Copy Pine for selection</button><span id="cpstat"></span></div>
</div>
<div class="wrap"><table id="tbl"></table></div>
<script>
var DATA = []; var PNL = null;
function selStrats(){return Array.prototype.slice.call(document.querySelectorAll('.stratcb:checked')).map(function(c){return c.value;});}
function curWeek(){return document.getElementById('wk').value;}
function filt(){var ss=selStrats(),wk=curWeek();
 var df=document.getElementById('dfrom').value,dtt=document.getElementById('dto').value;
 return DATA.filter(function(t){return ss.indexOf(t.strategy)>=0&&(wk==='all'||t.week===wk)
   &&(!df||(t.date||'')>=df)&&(!dtt||(t.date||'')<=dtt);});}
function money(v){return (v<0?'-$':'+$')+Math.abs(Math.round(v)).toLocaleString();}
function card(l,v,c,s){return '<div class="kpi"><div class="kl">'+l+'</div><div class="kv '+(c||'')+'">'+v+'</div>'+(s?('<div class="kl" style="margin:3px 0 0">'+s+'</div>'):'')+'</div>';}
function meanR(a,f){var v=a.map(f).filter(function(x){return x!==null&&x!==undefined;});return v.length?v.reduce(function(s,x){return s+x;},0)/v.length:null;}
function sgn(x){return x===null?'&mdash;':((x>=0?'+':'')+x.toFixed(2)+'R');}
function render(){
 var d=filt();
 var FILLED={win:1,loss:1,timeout:1};                   // filled & resolved
 var res=d.filter(function(t){return FILLED[t.outcome];});
 var w=res.filter(function(t){return t.outcome==='win';}).length;
 var l=res.filter(function(t){return t.outcome==='loss';}).length;
 var scr=res.filter(function(t){return t.outcome==='timeout';}).length;
 var nf=d.filter(function(t){return t.outcome==='no_fill'||t.outcome==='missed';}).length;
 var op=d.filter(function(t){return t.outcome==='open';}).length;
 var net=res.reduce(function(a,t){return a+(t.net||0);},0);
 var expR=meanR(res,function(t){return t.R;});
 var wks={};d.forEach(function(t){wks[t.week]=1;});var nw=Object.keys(wks).length;
 document.getElementById('sum').innerHTML=
   card('Net P&amp;L (on $100k)',money(net),net>=0?'up':'down','$500 = 1R')+
   card('Win rate',(w+l)?Math.round(w/(w+l)*100)+'%':'&mdash;','',w+'W / '+l+'L'+(scr?(' / '+scr+' scratch'):''))+
   card('Trades',res.length,'',(op?(op+' open'):'')+((op&&nf)?' &middot; ':'')+(nf?(nf+' no-fill'):'')||'&nbsp;')+
   card('Exp / trade',sgn(expR),(expR!==null&&expR>=0.15)?'up':'down','Gate-0: &ge; +0.15R')+
   card('Per week',nw?(res.length/nw).toFixed(1):'&mdash;','');
 var LBL={win:'WIN',loss:'LOSS',timeout:'SCRATCH',no_fill:'NO-FILL',missed:'MISSED',expired:'EXPIRED',open:'OPEN',out_of_range:'&mdash;'};
 var CLS={win:'win',loss:'loss'};
 var rows=d.slice().reverse().map(function(t){var sc='s'+t.strategy.replace('/','');
  var rescls=CLS[t.outcome]||'mut';
  var reslbl=LBL[t.outcome]||(t.outcome||'').toUpperCase();
  var Rc=(t.R===null||t.R===undefined)?'<span class=mut>&mdash;</span>':('<span class="'+rescls+'">'+(t.R>=0?'+':'')+t.R+'R</span>');
  var Nc=(t.net===null||t.net===undefined)?'<span class=mut>&mdash;</span>':('<span class="'+(t.net>=0?'win':'loss')+'">'+money(t.net)+'</span>');
  return '<tr><td>'+t.et+'</td><td>'+t.dow+'</td><td class="'+sc+'"><b>'+t.strategy+'</b></td><td>'+(t.dir==='LONG'?'&#9650;':'&#9660;')+'</td><td class=mut>'+t.sess+'</td><td>'+t.entry+'</td><td>'+t.sl+'</td><td>'+t.tp+'</td><td class="'+rescls+'">'+reslbl+'</td><td>'+Rc+'</td><td>'+Nc+'</td></tr>';}).join('');
 if(!d.length){rows='<tr><td colspan="11" class="mut" style="padding:18px">No trades yet &mdash; they appear here automatically as signals fire (London &amp; Asia excluded).</td></tr>';}
 document.getElementById('tbl').innerHTML='<tr><th>time (ET)</th><th>day</th><th>strat</th><th>dir</th><th>session</th><th>entry</th><th>SL</th><th>TP</th><th>result</th><th>R</th><th>$</th></tr>'+rows;
 renderCmp();
}
function renderCmp(){
 var el=document.getElementById('cmp'); if(!el) return;
 var ss=selStrats();
 // AUTO = shadow book (selected strategies, resolved)
 var a=DATA.filter(function(t){return ss.indexOf(t.strategy)>=0&&(t.outcome==='win'||t.outcome==='loss'||t.outcome==='timeout');});
 var aw=a.filter(function(t){return t.outcome==='win';}).length,al=a.filter(function(t){return t.outcome==='loss';}).length;
 var aExp=meanR(a,function(t){return t.R;}),aNet=a.reduce(function(s,t){return s+(t.net||0);},0);
 // YOU = /pnl trades you actually took (selected strategies, resolved)
 var yt=((PNL&&PNL.trades)||[]).filter(function(t){return t.taken&&ss.indexOf(t.strategy)>=0&&(t.result==='win'||t.result==='loss');});
 var yw=yt.filter(function(t){return t.result==='win';}).length,yl=yt.filter(function(t){return t.result==='loss';}).length;
 var yExp=meanR(yt,function(t){return t.pnl_r;}),yUsd=yt.reduce(function(s,t){return s+(t.pnl_usd||0);},0);
 var ab=(PNL&&PNL.alerts_by_strategy)||{};var fired=0;ss.forEach(function(s){fired+=ab[s]||0;});
 function pct(a,b){return (a+b)?Math.round(a/(a+b)*100)+'%':'&mdash;';}
 function panel(title,n,wp,exp,net,note){return '<div class="kpi" style="min-width:200px"><div class="kl">'+title+'</div><div class="kv '+(exp===null?'':(exp>=0.15?'up':'down'))+'">'+sgn(exp)+'</div><div class="kl" style="margin-top:3px">'+n+' trades &middot; '+wp+' win &middot; '+net+'</div><div class="kl">'+(note||'&nbsp;')+'</div></div>';}
 var html=
   panel('AUTO &mdash; every signal',a.length,pct(aw,al),aExp,money(aNet),fired?('would take all '+fired+' fired'):'hands-off, fixed 2R')+
   panel('YOU &mdash; actually took',yt.length,pct(yw,yl),yExp,money(yUsd),fired?(yt.length+' of '+fired+' fired taken ('+Math.round(yt.length/fired*100)+'%)'):'from /pnl');
 if(aExp!==null&&yExp!==null){var dR=aExp-yExp;
   html+='<div class="kpi" style="min-width:200px;border-color:'+(dR>=0?'#1f7a41':'#7a1f1f')+'"><div class="kl">Auto &minus; You</div><div class="kv '+(dR>=0?'up':'down')+'">'+(dR>=0?'+':'')+dR.toFixed(2)+'R</div><div class="kl" style="margin-top:3px">per trade edge from automating</div><div class="kl">'+(dR>=0?'auto ahead':'you ahead')+'</div></div>';}
 else{html+='<div class="kpi" style="min-width:200px"><div class="kl">Auto &minus; You</div><div class="kv">&mdash;</div><div class="kl" style="margin-top:3px">fills in as the shadow book grows</div><div class="kl">&nbsp;</div></div>';}
 el.innerHTML=html;
}
function initWeeks(){var wks={};DATA.forEach(function(t){wks[t.week]=1;});var arr=Object.keys(wks).sort();
 document.getElementById('wk').innerHTML='<option value="all">All weeks ('+arr.length+')</option>'+arr.map(function(w){return '<option value="'+w+'">week of '+w+'</option>';}).join('');}
function copyPine(){var d=filt();if(!d.length){document.getElementById('cpstat').textContent='nothing selected';return;}
 var SC={'A/B':0,'C':1,'F':2};var wk=curWeek();
 function A(f){return 'array.from('+d.map(f).join(',')+')';}
 var pine='//@version=5\n'+
  'indicator("Shadow '+(wk==='all'?'all weeks':'week '+wk)+' ('+d.length+' trades)", overlay=true, max_labels_count=500, max_lines_count=500, max_boxes_count=500)\n'+
  '// Entry line: A/B blue / C orange / F green. Red box=risk, green box=reward (2R). $100k@0.5%.\n'+
  'var int[]   T  = '+A(function(t){return t.ms;})+'\n'+
  'var bool[]  L  = '+A(function(t){return t.dir==="LONG"?"true":"false";})+'\n'+
  'var float[] EN = '+A(function(t){return t.entry;})+'\n'+
  'var float[] SLA= '+A(function(t){return t.sl;})+'\n'+
  'var float[] TPA= '+A(function(t){return t.tp;})+'\n'+
  'var int[]   STR= '+A(function(t){return SC[t.strategy];})+'\n'+
  'var int[]   W  = '+A(function(t){return t.outcome==="win"?1:0;})+'\n'+
  'ext=input.int(10,"bracket bars")\n'+
  'f_col(si)=> si==0?color.blue: si==1?color.orange: color.green\n'+
  'f_nm(si)=> si==0?"A/B": si==1?"C":"F"\n'+
  'for i=0 to array.size(T)-1\n'+
  '    if time==array.get(T,i)\n'+
  '        e=array.get(EN,i), s=array.get(SLA,i), t=array.get(TPA,i)\n'+
  '        lng=array.get(L,i), w=array.get(W,i), si=array.get(STR,i)\n'+
  '        box.new(bar_index,math.max(e,s),bar_index+ext,math.min(e,s),border_color=color.new(color.red,70),bgcolor=color.new(color.red,90))\n'+
  '        box.new(bar_index,math.max(e,t),bar_index+ext,math.min(e,t),border_color=color.new(color.green,70),bgcolor=color.new(color.green,90))\n'+
  '        line.new(bar_index,e,bar_index+ext,e,color=f_col(si),width=2)\n'+
  '        label.new(bar_index,e,f_nm(si)+(lng?" up ":" dn ")+(w==1?"WIN":"LOSS"),style=lng?label.style_label_up:label.style_label_down,color=color.new(f_col(si),20),textcolor=color.white,size=size.tiny)\n';
 function done(){document.getElementById('cpstat').textContent='copied '+d.length+' trades - paste into TradingView Pine editor';}
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(pine).then(done).catch(function(){fb();});}else{fb();}
 function fb(){var ta=document.createElement('textarea');ta.value=pine;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}ta.remove();done();}
}
Array.prototype.slice.call(document.querySelectorAll('.stratcb')).forEach(function(c){c.onchange=render;});
document.getElementById('wk').onchange=render;
document.getElementById('dfrom').onchange=render;
document.getElementById('dto').onchange=render;
document.getElementById('dclear').onclick=function(){document.getElementById('dfrom').value='';document.getElementById('dto').value='';render();};
document.getElementById('cp').onclick=copyPine;
function load(){
 function shadow(){fetch('/shadow/data?t='+Date.now(),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){DATA=d||[];initWeeks();render();}).catch(function(){DATA=[];initWeeks();render();});}
 fetch('/pnl?t='+Date.now(),{cache:'no-store',headers:{'Accept':'application/json'}}).then(function(r){return r.json();}).then(function(p){PNL=p;}).catch(function(){}).then(shadow);
}
load();
</script></body></html>"""


def render_home():
    return (PAGE.replace('__F__', _F).replace('__ORB__', _ORB)
                .replace('__EUR__', _EUR).replace('__JPY__', _JPY).replace('__AMD__', _AMD))


def _shadow_html():
    return SHADOW_PAGE   # live: the page fetches /shadow/data (served by shadow.py)


def register(app, path='/'):
    """Add the unified home page at `path` (default '/'). Isolated: adds one route only."""
    try:
        from flask import Response
    except Exception:
        return app
    def _dash_home():
        return Response(render_home(), mimetype='text/html')
    def _dash_shadow():
        return Response(_shadow_html(), mimetype='text/html')
    app.add_url_rule(path, 'dash_home', _dash_home)
    app.add_url_rule('/shadow', 'dash_shadow', _dash_shadow)
    return app


if __name__ == '__main__':
    open('dashboard_preview.html', 'w').write(render_home())
    print('wrote dashboard_preview.html (', len(render_home()), 'bytes )')
