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

def grade(x):
    """A = setup forward (body trzyma 50%); B = DIB (displacement zlamal poziom). Zawsze wykrywane oba."""
    return 'B' if 'DIB' in str(x.get('cat','')) else 'A'

def catname(x):
    return str(x.get('cat','')).replace('+DIB','')

def to_signal(x):
    isL = x['dir']=='LONG'
    slpts = abs(x['entry']-x['SL']) or 25
    t1 = round((x['entry']+slpts) if isL else (x['entry']-slpts),2)      # 1R: przesun SL na BE (NIE zamykaj)
    t2 = round((x['entry']+slpts*2) if isL else (x['entry']-slpts*2),2)  # 2R: TP calosc
    t3 = ''                                                              # nieuzywane (koniec scale-out)
    wk = 'BULL' if str(x['bias']).startswith('LONG') else ('BEAR' if str(x['bias']).startswith('SHORT') else '')
    trail=' | '.join(f'{a}-{b}' for a,b,_ in x['trail'][:3])
    return {
        'DateTime': f"{x['date']} {x['bos']}",
        'Type': f"{x['dir']} OTE",
        'Direction': x['dir'],
        'Quality': 'OTE',
        'Strategy': 'REV' if x['model']=='Reversal' else 'CONT',
        'Catalyst': catname(x),
        'Session': session_of(x['bos']),
        'Entry': x['entry'], 'SL': x['SL'], 'T1': t1, 'T2': t2, 'T3': t3,
        'Result': '', 'PnL': '',
        'Note': f"klasa {grade(x)} | bias {x['bias']}({x['bias_align']}) | trailing FVG: {trail}",
        'Weekly': wk,
    }

def size_for(entry, sl):
    """Wielkosc pozycji: ryzyko RISK_PCT% z ACCOUNT, w zaleznosci od SL. MNQ = $2/pkt."""
    try:
        acct  = float(os.environ.get('ACCOUNT', '100000'))
        riskp = float(os.environ.get('RISK_PCT', '0.5'))
        ptval = float(os.environ.get('POINT_VALUE', '2'))   # MNQ = $2/pkt
        risk_usd = acct * riskp / 100.0
        slpts = abs(float(entry) - float(sl))
        if slpts <= 0 or ptval <= 0: return None
        qty = int(risk_usd // (slpts * ptval))              # zaokraglenie w dol (nie przekrocz limitu)
        real = qty * slpts * ptval
        return qty, round(slpts,1), round(slpts*ptval), round(real), round(real/acct*100,2)
    except Exception:
        return None

def to_alert(x):
    emoji = '🟢' if x['dir']=='LONG' else '🔴'
    model = 'Reversal' if x['model']=='Reversal' else 'Cont'
    slpts = abs(x['entry']-x['SL']); isL = x['dir']=='LONG'
    be = round((x['entry']+slpts) if isL else (x['entry']-slpts),1)      # 1R: SL na BE
    tp = round((x['entry']+2*slpts) if isL else (x['entry']-2*slpts),1)  # 2R: TP calosc
    g = grade(x); gtag = '🅰️ klasa A' if g=='A' else '🅱️ klasa B (DIB)'
    base = (f"{gtag} · {emoji} {x['dir']} | {model} · Kat: {catname(x)} | Entry {x['entry']} | SL {x['SL']}"
            f"\n🎯 TP całość @ {tp} (2R) | przy 1R ({be}) przesuń SL na BE — NIE zamykaj części")
    s = size_for(x['entry'], x['SL'])
    if s:
        qty, slpts, perc, real, pct = s
        base += f"\n📐 Ryzyko: {qty} kontr. (SL {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)"
    return base

def post_webhook(text,url):
    if requests is None: return 'requests-brak'
    try:
        r=requests.post(url,data=text.encode('utf-8'),headers={'Content-Type':'text/plain'},timeout=10); return r.status_code
    except Exception as e:
        return f'ERR {e}'

def key(x): return f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"
def key_pre(x): return f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|PRE|{x['bounce']}"

def to_prealert(x):
    """PRE-alert: etap odbicia od CE, BOS jeszcze nie. NIE jest wejsciem — 'badz gotowa'."""
    emoji = '🟢' if x['dir']=='LONG' else '🔴'
    model = 'Reversal' if x['model']=='Reversal' else 'Cont'
    base = (f"⏳ PRZYGOTUJ SIĘ (czekaj na BOS — NIE wchodź) {emoji} {x['dir']} | {model} · Kat: {x['cat']}"
            f" | Entry~{x['entry']} | SL~{x['SL']}")
    sp = abs(x['entry']-x['SL']); isL = x['dir']=='LONG'
    pbe = round((x['entry']+sp) if isL else (x['entry']-sp),1)
    ptp = round((x['entry']+2*sp) if isL else (x['entry']-2*sp),1)
    base += f" | TP@{ptp} (2R) · BE przy 1R ({pbe})"
    s = size_for(x['entry'], x['SL'])
    if s:
        qty, slpts, perc, real, pct = s
        base += f"\n📐 Orientacyjnie: {qty} kontr. (SL {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)"
    base += "\n— to NIE wejście. Potwierdzenie BOS przyjdzie osobnym alertem (lub nie — ~28% odbić nie dochodzi)."
    return base

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
