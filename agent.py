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
import os, csv, json, subprocess, threading
from flask import Flask, request, jsonify
import live_emit   # to_alert, post_webhook, key

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
BUF  = os.path.join(HERE, 'buffer.csv')
OUT  = os.path.join(HERE, 'agent_out.pkl')
SENT = os.path.join(HERE, 'agent_sent.json')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL','')
BUFFER_BARS = int(os.environ.get('BUFFER_BARS','14000'))
COLS = ['ts_event','open','high','low','close','volume']
_lock = threading.Lock()
_primed = os.path.exists(SENT)   # jak plik sent istnieje -> juz po starcie

def _load_sent():
    try: return set(json.load(open(SENT)))
    except Exception: return set()
def _save_sent(s): json.dump(sorted(s), open(SENT,'w'))

def _append_bar(b):
    new = not os.path.exists(BUF)
    with open(BUF,'a',newline='') as f:
        w=csv.writer(f)
        if new: w.writerow(COLS)
        w.writerow([b['ts_event'],b['open'],b['high'],b['low'],b['close'],b.get('volume',0)])
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
    return jsonify(ok=True, **res)

@app.route('/health')
def health(): return jsonify(ok=True, primed=_primed, webhook=bool(WEBHOOK_URL))

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8000')))
