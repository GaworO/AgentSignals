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
CONTRACT = os.environ.get('CONTRACT','')   # jawny kontrakt do wpisywania zlecen, np. 'MNQU2026' (pusty = nie pokazuj)
OFFSET = float(os.environ.get('PRICE_OFFSET','0'))   # przelicz ceny MNQ1! -> twoj kontrakt (Tradovate). 0 poza rolowaniem.
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

def size_for(entry, sl, risk_pct=None):
    """Wielkosc pozycji: wskazane ryzyko % z ACCOUNT; domyslnie RISK_PCT. MNQ = $2/pkt."""
    try:
        acct  = float(os.environ.get('ACCOUNT', '100000'))
        riskp = float(risk_pct if risk_pct is not None else os.environ.get('RISK_PCT', '0.5'))
        ptval = float(os.environ.get('POINT_VALUE', '2'))   # MNQ = $2/pkt
        risk_usd = acct * riskp / 100.0
        slpts = abs(float(entry) - float(sl))
        if slpts <= 0 or ptval <= 0 or riskp <= 0: return None
        qty = int(risk_usd // (slpts * ptval))              # zaokraglenie w dol (nie przekrocz limitu)
        real = qty * slpts * ptval
        return qty, round(slpts,1), round(slpts*ptval), round(real), round(real/acct*100,2)
    except Exception:
        return None


def size_for_budget(entry, sl, risk_usd, round_trip_cost=None):
    """Size from an absolute dollar budget, including per-contract costs.

    This is the authoritative sizing path for a shared A/B setup group. It
    rounds down and therefore cannot spend more than ``risk_usd``.
    """
    try:
        acct = float(os.environ.get('ACCOUNT', '100000'))
        ptval = float(os.environ.get('POINT_VALUE', '2'))
        budget = float(risk_usd)
        rt_cost = float(
            round_trip_cost
            if round_trip_cost is not None
            else os.environ.get('SETUP_GROUP_RT_COST_USD', '2.24')
        )
        slpts = abs(float(entry) - float(sl))
        per_contract = slpts * ptval + max(0.0, rt_cost)
        if slpts <= 0 or ptval <= 0 or budget <= 0 or per_contract <= 0:
            return None
        qty = int(budget // per_contract)
        real = qty * per_contract
        return (
            qty,
            round(slpts, 2),
            round(per_contract, 2),
            round(real, 2),
            round(real / acct * 100, 3) if acct > 0 else 0.0,
        )
    except Exception:
        return None

def to_alert(x):
    emoji = '🟢' if x['dir']=='LONG' else '🔴'
    model = 'Reversal' if x['model']=='Reversal' else 'Cont'
    strat = x.get('_strat', 'A/B')
    slpts = abs(x['entry']-x['SL']); isL = x['dir']=='LONG'
    be = round((x['entry']+slpts) if isL else (x['entry']-slpts),1)
    g = grade(x); gtag = '🅰️ klasa A' if g=='A' else '🅱️ klasa B (DIB)'
    if x.get('bias_align')=='Y': gtag += ' ⭐bias'
    if x.get('brk',1)>=2: gtag += f" 🔁 re-test #{x['brk']}"
    rpts = round(slpts,1)
    side = 'BUY' if isL else 'SELL'
    e_d = round(x['entry']+OFFSET,1); sl_d = round(x['SL']+OFFSET,1)

    if strat == 'A/B-shallow':
        default_tp = (x['entry']+2*slpts) if isL else (x['entry']-2*slpts)
        try: tp = float(x.get('TP')) if x.get('TP') is not None else default_tp
        except Exception: tp = default_tp
        tp_r = abs(tp - x['entry']) / slpts if slpts else 0
        tppts = round(abs(tp-x['entry']),1); tp_d = round(tp+OFFSET,1)
        base = (f"📋 A/B-shallow · {side} LIMIT · POSTAW (BOS potwierdzony — płytsze wejście) · "
                f"{gtag} · {emoji} {x['dir']} | {model} · Kat: {catname(x)}")
        if CONTRACT or OFFSET:
            base += f"\n📄 Kontrakt: {CONTRACT or '—'}" + (f" (ceny +{round(OFFSET,1)} z MNQ1!)" if OFFSET else "")
        base += (f"\n🎯 {side} LIMIT {e_d} ({x.get('kind','A/B shallow')})"
                 f"\n🛑 SL {sl_d} · ryzyko {rpts} pkt"
                 f"\n🎯 TP {tp_d} · +{tppts} pkt ({tp_r:.2f}R · {x.get('tp_src') or 'shallow'})")
    else:
        tp = round((x['entry']+2*slpts) if isL else (x['entry']-2*slpts),1)
        tppts = round(2*slpts,1); tp_d = round(tp+OFFSET,1)
        tp3 = round((x['entry']+3*slpts) if isL else (x['entry']-3*slpts),1)
        tp3_d = round(tp3+OFFSET,1); tp3pts = round(3*slpts,1)
        base = (f"📋 {side} LIMIT · POSTAW (BOS potwierdzony — zlecenie oczekujące, fill na cofnięciu) · "
                f"{gtag} · {emoji} {x['dir']} | {model} · Kat: {catname(x)}")
        if CONTRACT or OFFSET:
            base += f"\n📄 Kontrakt: {CONTRACT or '—'}" + (f" (ceny +{round(OFFSET,1)} z MNQ1!)" if OFFSET else "")
        base += (f"\n🎯 {side} LIMIT {e_d} ({x.get('kind','FVG/OTE')})"
                 f"\n🛑 SL {sl_d} · ryzyko {rpts} pkt · BE po +{rpts} pkt (1R)"
                 f"\n🎯 TP2 {tp_d} · +{tppts} pkt (2R — cel systemu, zweryfikowany)"
                 f"\n🎯 TP3 {tp3_d} · +{tp3pts} pkt (3R — opcjonalny runner, niezweryfikowany)")

    rp = x.get('_risk_pct_override')
    budget = x.get('_risk_budget_usd')
    sf = (size_for_budget(x['entry'], x['SL'], budget)
          if budget is not None else size_for(x['entry'], x['SL'], rp))
    if sf:
        qty, slpts, perc, real, pct = sf
        label = 'SL+koszty' if budget is not None else 'SL'
        base += f"\n📐 Ryzyko: {qty} kontr. ({label} {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)"
    return base

def post_webhook(text,url):
    if requests is None: return 'requests-brak'
    try:
        r=requests.post(url,data=text.encode('utf-8'),headers={'Content-Type':'text/plain'},timeout=10); return r.status_code
    except Exception as e:
        return f'ERR {e}'

def key(x): return f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"

# (usunięte) key_pre / to_prealert — stary PRE-alert „czekaj na BOS — NIE wchodź" zniesiony.
# W v10 wejście to LIMIT stawiany PO potwierdzeniu BOS (to_alert powyżej) — osobny etap PRE niepotrzebny.

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
