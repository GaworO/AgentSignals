"""
AGENT live (osobny serwis — NIE wrzucac do NQsignals).
- TV (alert co domkniety bar 1m) -> POST /bars  [CICHO, bez Telegrama]
- agent trzyma bufor, liczy det_new.py, i TYLKO nowe potwierdzone setupy -> POST na WEBHOOK_URL (Telegram)
- na starcie oznacza istniejace setupy jako 'widziane' (zero zalewania historia)

ENV:
  WEBHOOK_URL  = https://nqsignals-production.up.railway.app/webhook?secret=nqscout2024
  PORT         = 8000 (Railway ustawia sam)
  BUFFER_BARS  = 14000 (~10 dni 1m)
Uruchom: python3 agent.py    (lokalnie/VPS/osobny serwis Railway)
"""
import os, csv, json, subprocess, threading, sqlite3, shutil, datetime as dt
from zoneinfo import ZoneInfo
try: import requests
except Exception: requests=None
from flask import Flask, request, jsonify
import live_emit   # to_alert, post_webhook, key

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
NY = ZoneInfo('Etc/GMT+4')   # sztywne UTC-4 (jak TFO/wykres), bez DST
PUBLIC_URL = os.environ.get('PUBLIC_URL','').rstrip('/')   # np. https://agentsignals-production.up.railway.app
NO_TRADE_SUPPRESS = os.environ.get('NO_TRADE_SUPPRESS','') == '1'   # 1 = twarde wyciszenie przy high-impact
DATA_DIR = os.environ.get('DATA_DIR', HERE)   # ustaw na /data (Railway Volume) by przetrwac restart
try: os.makedirs(DATA_DIR, exist_ok=True)
except Exception: DATA_DIR = HERE
BUF  = os.path.join(DATA_DIR, 'buffer.csv')
OUT  = os.path.join(DATA_DIR, 'agent_out.pkl')
SENT = os.path.join(DATA_DIR, 'agent_sent.json')
DB   = os.path.join(DATA_DIR, 'journal.db')
SEED_CSV    = os.environ.get('SEED_CSV', os.path.join(HERE,'seed.csv'))  # najswiezszy Databento CSV
WEBHOOK_URL = os.environ.get('WEBHOOK_URL','')
BUFFER_BARS = int(os.environ.get('BUFFER_BARS','14000'))
COLS = ['ts_event','open','high','low','close','volume']
_lock = threading.Lock()
_primed = os.path.exists(SENT)
_last = {'last_bar': None, 'bars_in_buffer': 0, 'setups_seen': None, 'processed_at': None}

def _init_db():
    c=sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS signals(
        key TEXT PRIMARY KEY, logged_at TEXT, date TEXT, model TEXT, cat TEXT, dir TEXT,
        trig TEXT, disp_end TEXT, bounce TEXT, bos TEXT,
        entry REAL, ote62 REAL, ote79 REAL, SL REAL, TP REAL,
        fvg_lo REAL, fvg_hi REAL, bias TEXT, bias_align TEXT,
        trail TEXT, alert TEXT, posted TEXT, result TEXT, pnl REAL)''')
    c.commit(); c.close()

def _save_db(x, alert_text, code):
    c=sqlite3.connect(DB)
    c.execute('''INSERT OR IGNORE INTO signals
        (key,logged_at,date,model,cat,dir,trig,disp_end,bounce,bos,entry,ote62,ote79,SL,TP,fvg_lo,fvg_hi,bias,bias_align,trail,alert,posted,result,pnl)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (live_emit.key(x), dt.datetime.utcnow().isoformat(timespec='seconds'),
         x['date'],x['model'],x['cat'],x['dir'],x['trig'],x['disp_end'],x['bounce'],x['bos'],
         x['entry'],x['ote62'],x['ote79'],x['SL'],x['TP'],x['fvg_lo'],x['fvg_hi'],
         x['bias'],x['bias_align'], json.dumps(x['trail']), alert_text, str(code), '', None))
    c.commit(); c.close()

def _seed_buffer():
    if os.path.exists(BUF) or not os.path.exists(SEED_CSV): return
    import pandas as pd
    d=pd.read_csv(SEED_CSV)
    for col in COLS:
        if col not in d.columns: d[col]=0
    d[COLS].tail(BUFFER_BARS).to_csv(BUF,index=False)

def _load_sent():
    try: return set(json.load(open(SENT)))
    except Exception: return set()
def _save_sent(s): json.dump(sorted(s), open(SENT,'w'))

def _append_bar(b):
    ts=str(b['ts_event']).strip()
    if '+' not in ts and 'Z' not in ts: ts=ts+'+00:00'   # spojny format z seedem (UTC, +00:00)
    new = not os.path.exists(BUF)
    with open(BUF,'a',newline='') as f:
        w=csv.writer(f)
        if new: w.writerow(COLS)
        w.writerow([ts,b['open'],b['high'],b['low'],b['close'],b.get('volume',0)])
    with open(BUF) as f: rows=f.readlines()
    if len(rows) > BUFFER_BARS+1:
        with open(BUF,'w') as f: f.write(rows[0]+''.join(rows[-BUFFER_BARS:]))
    # przytnij bufor do ostatnich BUFFER_BARS
    with open(BUF) as f: rows=f.readlines()
    if len(rows) > BUFFER_BARS+1:
        with open(BUF,'w') as f: f.write(rows[0]+''.join(rows[-BUFFER_BARS:]))

def _detect():
    env=dict(os.environ, DATA_CSV=BUF, OUT_PKL=OUT, CUTOFF='')   # CUTOFF pusty = bez filtra dat
    subprocess.run(['python3', os.path.join(HERE,'det_new.py')], env=env,
                   capture_output=True, timeout=120)
    import pickle
    try: return pickle.load(open(OUT,'rb'))
    except Exception: return []

# ====== KALENDARZ NEWSOW (ForexFactory weekly) + FLAGI NO-TRADE ======
HIGH = {'CPI','Core CPI','Non-Farm','NFP','PPI','GDP','Core PCE','PCE','ISM','FOMC','Federal Funds','Powell'}
_cal = {'at': None, 'events': []}   # cache eventow high-impact: lista (epoch_utc, title)
def _load_calendar():
    if requests is None: return
    if _cal['at'] and (dt.datetime.utcnow()-_cal['at']).total_seconds() < 6*3600: return
    try:
        r=requests.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=15)
        evs=[]
        for e in r.json():
            if str(e.get('impact','')).lower()!='high': continue
            t=dt.datetime.fromisoformat(e['date']).timestamp()
            evs.append((t, e.get('title','event')))
        _cal['events']=evs; _cal['at']=dt.datetime.utcnow()
    except Exception as ex:
        print('[cal] blad pobierania:', ex, flush=True)   # fail-safe: brak flag eventow

def flags_for(x):
    """zwraca (lista_flag, czy_high_impact). FLAGI nie filtry (chyba ze NO_TRADE_SUPPRESS)."""
    fl=[]; hard=False
    t_utc = x['bos_ms']/1000.0
    ny = dt.datetime.fromtimestamp(t_utc, tz=NY); m = ny.hour*60+ny.minute
    in_kz = (120<=m<300) or (570<=m<660) or (810<=m<960)   # London / NYAM / NYPM
    if not in_kz: fl.append('poza KZ')
    if ny.weekday()==0 and m<720: fl.append('PON rano')
    _load_calendar()
    for et,title in _cal['events']:
        if abs(t_utc-et) <= 30*60:                          # +/- 30 min wokol high-impact
            fl.append(f'event: {title}'); hard=True
    return fl, hard

def _process_new(now_ms=None):
    global _primed
    setups=_detect()
    sent=_load_sent()
    keys=[live_emit.key(x) for x in setups]
    if not _primed:                       # pierwszy przebieg: oznacz wszystko jako widziane, NIE alarmuj
        _save_sent(set(keys)); _primed=True
        return {'primed': len(keys)}
    fresh=[x for x,k in zip(setups,keys) if k not in sent]
    sentn=set(sent)
    fresh_ms = int(os.environ.get('FRESH_MIN','15'))*60*1000   # strażnik świeżości: alarmuj tylko swieze
    for x in fresh:
        if now_ms and x.get('bos_ms') and (now_ms - x['bos_ms']) > fresh_ms:
            print('STALE skip (stary setup, nie alarmuje):', live_emit.key(x), flush=True)
            sentn.add(live_emit.key(x)); continue
        fl, hard = flags_for(x)
        txt=live_emit.to_alert(x)
        if fl: txt += '  ⚠ ' + ', '.join(fl)
        if PUBLIC_URL: txt += '  📊 ' + PUBLIC_URL.rstrip('/') + '/chart?key=' + live_emit.key(x).replace('|','%7C').replace(' ','%20').replace(':','%3A')
        if hard and NO_TRADE_SUPPRESS:                       # twarde wyciszenie tylko jak wlaczone
            print('SUPPRESS (high-impact)', txt, flush=True)
            _save_db(x, txt+' [SUPPRESSED]', 'suppressed'); sentn.add(live_emit.key(x)); continue
        code=live_emit.post_webhook(txt, WEBHOOK_URL) if WEBHOOK_URL else 'no-url'
        print('ALERT', code, txt, flush=True)
        _save_db(x, txt, code)
        if WEBHOOK_URL and str(code).startswith('2'): sentn.add(live_emit.key(x))
        elif not WEBHOOK_URL: sentn.add(live_emit.key(x))
    _save_sent(sentn)
    return {'nowe': len(fresh)}

@app.route('/bars', methods=['POST'])
def bars():
    b=request.get_json(force=True, silent=True) or {}
    if 'close' not in b: return jsonify(error='brak OHLC'), 400
    ts=str(b.get('ts_event','')).strip()
    try: now_ms=int(dt.datetime.fromisoformat(ts if ('+' in ts or 'Z' in ts) else ts+'+00:00').timestamp()*1000)
    except Exception: now_ms=int(dt.datetime.utcnow().timestamp()*1000)   # fail-safe: zawsze "teraz", strażnik nigdy nie wyłączony
    with _lock:
        _append_bar(b)
        res=_process_new(now_ms)
        nb=(sum(1 for _ in open(BUF))-1) if os.path.exists(BUF) else 0
        _last.update(last_bar=str(b.get('ts_event')), bars_in_buffer=nb,
                     setups_seen=res.get('nowe', res.get('primed')),
                     processed_at=dt.datetime.utcnow().isoformat(timespec='seconds'))
        print(f"[bars] {b.get('ts_event')} buf={nb} -> {res}", flush=True)
    return jsonify(ok=True, **res)

@app.route('/status')
def status():
    nb=(sum(1 for _ in open(BUF))-1) if os.path.exists(BUF) else 0
    _last['bars_in_buffer']=nb
    return jsonify(primed=_primed, **_last)

@app.route('/health')
def health(): return jsonify(ok=True, primed=_primed, webhook=bool(WEBHOOK_URL), buffer=os.path.exists(BUF))

def _bars_json(n=200):
    if not os.path.exists(BUF): return []
    out=[]
    with open(BUF) as f:
        r=csv.DictReader(f)
        rows=list(r)[-n:]
    for x in rows:
        try:
            ts=int(dt.datetime.fromisoformat(x['ts_event']).timestamp())
            out.append({'time':ts,'open':float(x['open']),'high':float(x['high']),
                        'low':float(x['low']),'close':float(x['close'])})
        except Exception: pass
    return out

@app.route('/chart-data')
def chart_data():
    key=request.args.get('key','')
    lv=None
    try:
        c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
        row=c.execute('SELECT * FROM signals WHERE key=?',(key,)).fetchone()
        if row is None: row=c.execute('SELECT * FROM signals ORDER BY logged_at DESC LIMIT 1').fetchone()
        c.close()
        if row: lv=dict(row)
    except Exception: pass
    return jsonify(bars=_bars_json(), setup=lv)

CHART_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ICT chart</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>body{margin:0;background:#0a0a0a;color:#ddd;font-family:sans-serif}#h{padding:8px 12px;font-size:14px}#c{height:88vh}</style>
</head><body><div id=h>ladowanie...</div><div id=c></div><script>
const key=new URLSearchParams(location.search).get('key')||'';
fetch('/chart-data?key='+encodeURIComponent(key)).then(r=>r.json()).then(d=>{
 d.bars.forEach(b=>b.time-=4*3600);   // wyswietlaj w UTC-4 (jak TFO)
 const ch=LightweightCharts.createChart(document.getElementById('c'),{layout:{background:{color:'#0a0a0a'},textColor:'#ddd'},grid:{vertLines:{color:'#1a1a1a'},horzLines:{color:'#1a1a1a'}},timeScale:{timeVisible:true,secondsVisible:false}});
 const s=ch.addCandlestickSeries({upColor:'#4ade80',downColor:'#f87171',wickUpColor:'#4ade80',wickDownColor:'#f87171',borderVisible:false});
 s.setData(d.bars);
 const u=d.setup;
 if(u){
  document.getElementById('h').textContent=u.dir+' | '+u.model+' · '+u.cat+' @ '+u.entry+'   (BOS '+u.bos+')';
  const L=(p,c,t)=>{if(p!=null)s.createPriceLine({price:p,color:c,lineWidth:2,title:t});};
  L(u.entry,'#3b82f6','ENTRY');L(u.SL,'#f87171','SL');L(u.TP,'#4ade80','TP');
  L(u.fvg_lo,'#f59e0b','FVG');L(u.fvg_hi,'#f59e0b','FVG');
 } else {document.getElementById('h').textContent='Brak setupu w bazie — same swieczki.';}
 ch.timeScale().fitContent();
});
</script></body></html>"""

@app.route('/chart')
def chart(): return CHART_HTML

@app.route('/journal')
def journal():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    rows=[dict(r) for r in c.execute('SELECT * FROM signals ORDER BY bos DESC LIMIT 200')]
    c.close(); return jsonify(signals=rows)

_init_db(); _seed_buffer()

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8000')))
