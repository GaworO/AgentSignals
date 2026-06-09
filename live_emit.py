"""
Emiter forward-test: mapuje POTWIERDZONY setup z det_new -> schemat dziennika (signals.html)
i POST-uje do Railway. Dedup: kazdy setup wysylany raz (po kluczu).
Uzycie:
  python3 live_emit.py                     # dry-run: pokaz JSON ktory poleci
  python3 live_emit.py --send <URL>        # faktyczny POST (odpal tam gdzie jest siec do Railway)
Domyslny cel logowania (znany kontrakt z signals.html): .../signals
Alert Telegram idzie przez .../webhook?secret=... (payload do potwierdzenia z Ola).
"""
import pickle, json, sys, os
try: import requests
except Exception: requests=None

try:
    SETUPS = pickle.load(open(os.path.join(os.path.dirname(__file__),'det_new.pkl'),'rb'))
except Exception:
    SETUPS = []   # agent importuje tylko funkcje (to_alert/post_webhook/key); pkl niepotrzebny
RISK = 580   # jak w signals.html
SENT_FILE = '/home/claude/sent_signals.json'   # dedup miedzy uruchomieniami

def session_of(hhmm):
    h,m=map(int,hhmm.split(':')); t=h*60+m
    if 570<=t<=660: return 'NY AM SB'
    if 810<=t<=960: return 'NY PM SB'
    if 120<=t<=300: return 'London SB'
    return 'pozaKZ'

def to_signal(x):
    isL = x['dir']=='LONG'
    slpts = abs(x['entry']-x['SL']) or 25
    t1 = x['TP']
    t2 = round((x['entry']+slpts*3) if isL else (x['entry']-slpts*3),2)
    t3 = round((x['entry']+slpts*5) if isL else (x['entry']-slpts*5),2)
    wk = 'BULL' if str(x['bias']).startswith('LONG') else ('BEAR' if str(x['bias']).startswith('SHORT') else '')
    trail=' | '.join(f'{a}-{b}' for a,b,_ in x['trail'][:3])
    return {
        'DateTime': f"{x['date']} {x['bos']}",
        'Type': f"{x['dir']} OTE",
        'Direction': x['dir'],
        'Quality': 'OTE',
        'Strategy': 'REV' if x['model']=='Reversal' else 'CONT',
        'Catalyst': x['cat'],
        'Session': session_of(x['bos']),
        'Entry': x['entry'], 'SL': x['SL'], 'T1': t1, 'T2': t2, 'T3': t3,
        'Result': '', 'PnL': '',
        'Note': f"bias {x['bias']}({x['bias_align']}) | trailing FVG: {trail}",
        'Weekly': wk,
    }

def to_alert(x):
    emoji = '🟢' if x['dir']=='LONG' else '🔴'
    model = 'Reversal' if x['model']=='Reversal' else 'Cont'
    return f"{emoji} {x['dir']} | {model} · Kat: {x['cat']} | Entry {x['entry']} | SL {x['SL']} | TP {x['TP']}"

def post_webhook(text,url):
    if requests is None: return 'requests-brak'
    try:
        r=requests.post(url,data=text.encode('utf-8'),headers={'Content-Type':'text/plain'},timeout=10); return r.status_code
    except Exception as e:
        return f'ERR {e}'

def key(x): return f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"

def load_sent():
    try: return set(json.load(open(SENT_FILE)))
    except Exception: return set()

def post(sig,url):
    if requests is None: return 'requests-brak'
    try:
        r=requests.post(url,json=sig,timeout=10); return r.status_code
    except Exception as e:
        return f'ERR {e}'

if __name__=='__main__':
    sigs=[(key(x),x) for x in SETUPS]
    print('=== ALERTY (tekst -> Telegram przez /webhook) ===')
    for k,x in sigs: print(to_alert(x))
    print(f'... razem {len(sigs)} alertow')
    if '--webhook' in sys.argv:                       # wyslij teksty na /webhook (Telegram)
        url=sys.argv[sys.argv.index('--webhook')+1]
        sent=load_sent(); newsent=set(sent)
        for k,x in sigs:
            if k in sent: print('pominieto:',k); continue
            code=post_webhook(to_alert(x),url); print(code,k)
            if str(code).startswith('2'): newsent.add(k)
        json.dump(sorted(newsent),open(SENT_FILE,'w'))
    if '--signals' in sys.argv:                        # log do dziennika /signals (JSON)
        url=sys.argv[sys.argv.index('--signals')+1]
        for k,x in sigs: print(post(to_signal(x),url),k)
