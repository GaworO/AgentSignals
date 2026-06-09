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
from flask import Flask, request, jsonify
import live_emit   # to_alert, post_webhook, key

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
BUF  = os.path.join(HERE, 'buffer.csv')
OUT  = os.path.join(HERE, 'agent_out.pkl')
SENT = os.path.join(HERE, 'agent_sent.json')
DB   = os.path.join(HERE, 'journal.db')
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

def _process_new():
    global _primed
    setups=_detect()
    sent=_load_sent()
    keys=[live_emit.key(x) for x in setups]
    if not _primed:                       # pierwszy przebieg: oznacz wszystko jako widziane, NIE alarmuj
        _save_sent(set(keys)); _primed=True
        return {'primed': len(keys)}
    fresh=[x for x,k in zip(setups,keys) if k not in sent]
    sentn=set(sent)
    for x in fresh:
        txt=live_emit.to_alert(x)
        code=live_emit.post_webhook(txt, WEBHOOK_URL) if WEBHOOK_URL else 'no-url'
        print('ALERT', code, txt, flush=True)
        _save_db(x, txt, code)                       # zapis do SQLite (wszystkie pola)
        if WEBHOOK_URL and str(code).startswith('2'): sentn.add(live_emit.key(x))
        elif not WEBHOOK_URL: sentn.add(live_emit.key(x))
    _save_sent(sentn)
    return {'nowe': len(fresh)}

@app.route('/bars', methods=['POST'])
def bars():
    b=request.get_json(force=True, silent=True) or {}
    if 'close' not in b: return jsonify(error='brak OHLC'), 400
    with _lock:
        _append_bar(b)
        res=_process_new()
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

@app.route('/journal')
def journal():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    rows=[dict(r) for r in c.execute('SELECT * FROM signals ORDER BY bos DESC LIMIT 200')]
    c.close(); return jsonify(signals=rows)

_init_db(); _seed_buffer()

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8000')))
