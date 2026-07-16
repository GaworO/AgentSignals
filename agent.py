"""
AGENT live (osobny serwis — NIE wrzucac do NQsignals).
- TV (alert co domkniety bar 1m) -> POST /bars  [CICHO, bez Telegrama]
- agent trzyma bufor, liczy det_v10.py (LIVE), i TYLKO nowe potwierdzone setupy -> POST na WEBHOOK_URL (Telegram)
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
from flask import Flask, request, jsonify, send_file
import live_emit   # to_alert, post_webhook, key
import manage      # sledzenie 1R/3R (alert partial+BE) — izolowane, nie rusza intake'u
import regime_gate # v12: regime-gated EOD on/off + Telegram przy zmianie stanu
import pnl         # UNIFIED P&L JOURNAL — izolowane: nowa tabela `fills` + trasy /pnl; nie rusza intake'u/detektora
import how_ab      # A/B "how it works" page at /how — izolowany dodatek (ORB /how style), nie rusza detektora
import cme_calendar  # v22: kalendarz CME (swieta/early close) dla heartbeat — koniec falszywych STALE w swieta
import dashboard   # / — unified home shell (federuje istniejące strony; izolowany dodatek)
import shadow      # /shadow/data + /shadow/log — LIVE shadow-executor log (hands-off, no money; isolated add-on)
import forex_pnl   # forexpnl - joined forex-only P&L (isolated add-on)
import allview     # /all/trades + /all/candidates - joined view across A/B/C/F/ORB (isolated add-on)

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
TRADES = os.path.join(DATA_DIR, 'trades.json')   # otwarte trady do sledzenia 1R/3R
ARCHIVE = os.path.join(DATA_DIR, 'archive.csv')  # pelna historia barow — NIGDY nie przycinana (backtesty / odswiezenie seed.csv)
OUTCOMES = os.path.join(DATA_DIR, 'outcomes.json')  # realized R per zamkniety trade -> /performance
SEED_CSV    = os.environ.get('SEED_CSV', os.path.join(HERE,'seed.csv'))  # najswiezszy Databento CSV
WEBHOOK_URL = os.environ.get('WEBHOOK_URL','')
BUFFER_BARS = int(os.environ.get('BUFFER_BARS','14000'))
VERSION = 'v22'   # marker wersji — widoczny w /status i /health, by potwierdzić deploy
COLS = ['ts_event','open','high','low','close','volume']
_lock = threading.Lock()
_primed = os.path.exists(SENT)
_last = {'last_bar': None, 'bars_in_buffer': 0, 'setups_seen': None, 'processed_at': None}

# ====== v20: SERVER-SIDE FEED HEARTBEAT ======
# Intake is event-driven: if TradingView stops POSTing /bars, NO request handler runs, so the
# silence is invisible from inside the app (exactly what happened 06-23: 3 days unnoticed). This
# background thread is the one thing that runs WITHOUT an inbound bar — so it is what notices the
# feed died and pings Telegram. Opt out with HEARTBEAT=0.
import time as _time
_START = dt.datetime.utcnow()
_hb = {'alerted': False}
HEARTBEAT       = os.environ.get('HEARTBEAT', '1') != '0'                 # default ON
STALE_MIN       = float(os.environ.get('STALE_MIN', '20'))               # min w/o a new bar = stale (market hours)
HEARTBEAT_EVERY = float(os.environ.get('HEARTBEAT_EVERY_SEC', '300'))    # how often to check (seconds)

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
         x['date'],x['model'],x['cat'],x['dir'],x.get('trig',''),x.get('disp_end',''),x.get('bounce',''),x['bos'],
         x['entry'],x.get('ote62'),x.get('ote79'),x['SL'],x['TP'],x['fvg_lo'],x['fvg_hi'],
         x['bias'],x['bias_align'], json.dumps(x.get('trail',[])), alert_text, str(code), '', None))
    c.commit(); c.close()

def _exec_order(x, text=None):
    """Route 2 (semi-auto): wyślij PRE-STAGED zlecenie bracket do TradersPost (EXEC_WEBHOOK).
    Z auto-submit OFF w TradersPost zlecenie czeka na Twoje 1-klik zatwierdzenie (MFF: nadzór nad
    każdym wejściem). NIE rusza strategii — to tylko dodatkowe wyjście.
    EXEC_QTY='auto' (domyślnie) = ryzyko jak w alercie (size_for); liczba = sztywno; EXEC_MAX_QTY = limit."""
    url = os.environ.get('EXEC_WEBHOOK', '')
    if not url or requests is None: return {"sent": False, "reason": ("EXEC_WEBHOOK not set" if not url else "requests missing")}
    try:
        off = float(os.environ.get('PRICE_OFFSET', '0'))
        bull = x['dir'] == 'LONG'; e = float(x['entry']); sl = float(x['SL']); R = abs(e - sl)
        tp = (e + 2*R) if bull else (e - 2*R)
        # Wielkosc: 'auto' (domyslnie) = ryzyko jak w alercie (size_for: RISK_PCT% z ACCOUNT, MNQ $2/pkt),
        # ta sama liczba kontraktow co w linii "Ryzyko: N kontr.". Liczba w EXEC_QTY = sztywno.
        # EXEC_MAX_QTY = opcjonalny limit (np. regula max kontraktow MFF / eval).
        _q = os.environ.get('EXEC_QTY', 'auto').strip().lower()
        if _q.isdigit() and int(_q) > 0:
            qty = int(_q)
        else:
            try:
                _sf = live_emit.size_for(e, sl); qty = int(_sf[0]) if _sf else 1
            except Exception:
                qty = 1
        _cap = os.environ.get('EXEC_MAX_QTY', '').strip()
        if _cap.isdigit() and int(_cap) > 0:
            qty = min(qty, int(_cap))
        qty = max(1, int(qty))
        payload = {
            "ticker": os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!')),
            "action": "buy" if bull else "sell",
            "orderType": "limit",
            "limitPrice": round(e + off, 2),
            "quantity": qty,
            "takeProfit": {"limitPrice": round(tp + off, 2)},
            "stopLoss": {"type": "stop", "stopPrice": round(sl + off, 2)},
            "timeInForce": "gtc",
        }
        if text: payload["text"] = text   # relay /stage użyje jako treść -> JEDNA wiadomość zamiast dwóch
        r = requests.post(url, json=payload, timeout=10)
        st = getattr(r, 'status_code', None)
        body = ''
        try: body = (r.text or '')[:200]
        except Exception: pass
        print('EXEC', st, payload, flush=True)
        return {"sent": True, "status": st, "resp": body,
                "has_secret_q": ("secret=" in url), "path_tail": url.split('?')[0][-16:], "qty": payload.get("quantity")}
    except Exception as ex:
        print('EXEC err', ex, flush=True)
        return {"sent": False, "error": str(ex), "has_secret_q": ("secret=" in url), "path_tail": url.split('?')[0][-16:]}

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
    row=[ts,b['open'],b['high'],b['low'],b['close'],b.get('volume',0)]
    # --- ARCHIWUM: zasiej z istniejącego bufora PRZED dopisaniem nowego bara (bez duplikatu) ---
    arch_new = not os.path.exists(ARCHIVE)
    if arch_new and os.path.exists(BUF):
        try:
            with open(BUF) as src, open(ARCHIVE,'w') as dst: dst.write(src.read())
            arch_new=False
        except Exception: pass
    new = not os.path.exists(BUF)
    with open(BUF,'a',newline='') as f:
        w=csv.writer(f)
        if new: w.writerow(COLS)
        w.writerow(row)
    with open(ARCHIVE,'a',newline='') as f:          # pelna historia — NIGDY nie przycinana
        w=csv.writer(f)
        if arch_new: w.writerow(COLS)
        w.writerow(row)
    with open(BUF) as f: rows=f.readlines()
    if len(rows) > BUFFER_BARS+1:
        with open(BUF,'w') as f: f.write(rows[0]+''.join(rows[-BUFFER_BARS:]))
    # przytnij bufor do ostatnich BUFFER_BARS
    with open(BUF) as f: rows=f.readlines()
    if len(rows) > BUFFER_BARS+1:
        with open(BUF,'w') as f: f.write(rows[0]+''.join(rows[-BUFFER_BARS:]))

_gate = {'at': 0.0, 'eod_on': False, 'reg': None}
def _regime_now():
    """Policz rezim max raz na REGIME_TTL_SEC (regime_stats odpala detektor jako subprocess — drogie).
    Cache w _gate['reg']; zasila i EOD gate (v12) i size gate (v16). EOD Telegram tylko przy zmianie."""
    import time
    now = time.time()
    if _gate.get('reg') is None or now - _gate['at'] > float(os.environ.get('REGIME_TTL_SEC', '3600')):
        try:
            import regime as _regime
            reg = _regime.regime_stats(BUF, HERE); _gate['reg'] = reg; _gate['at'] = now
            if os.environ.get('REGIME_GATE', '') == '1':     # EOD notify tylko gdy gate wlaczony
                eod_on, lab, code = regime_gate.notify_if_changed(reg, WEBHOOK_URL, DATA_DIR, live_emit.post_webhook)
                _gate['eod_on'] = eod_on
                if code != 'unchanged': print('[regime_gate] EOD', lab, code, flush=True)
        except Exception as e:
            print('[regime] err', e, flush=True)   # fail-safe: zostaw poprzedni stan
    return _gate.get('reg')
def _eod_flag():
    _regime_now(); return _gate['eod_on']

def _detect():
    gated = os.environ.get('REGIME_GATE', '') == '1'            # REGIME_GATE=1 -> wlacza EOD_INTRADAY; detektor i tak = v11
    det_file = os.environ.get('DET_FILE', 'det_v11.py')   # v20: v11 (detcore) = live detector; DET_FILE nadpisuje
    env=dict(os.environ, DATA_CSV=BUF, OUT_PKL=OUT, CUTOFF='')   # CUTOFF pusty = bez filtra dat
    if gated:
        env['EOD_INTRADAY'] = '1' if _eod_flag() else ''        # regime-gated: ON w choppy, OFF w trend
    subprocess.run(['python3', os.path.join(HERE, det_file)], env=env,
                   capture_output=True, timeout=180)
    import pickle
    try: conf=pickle.load(open(OUT,'rb'))
    except Exception: conf=[]
    return conf, []                                              # wejscie = LIMIT po BOS

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
    setups, _ = _detect()
    sent=_load_sent()
    keys=[live_emit.key(x) for x in setups]
    if not _primed:                       # pierwszy przebieg: oznacz wszystko jako widziane
        allk=set(keys)
        _save_sent(allk); _primed=True
        return {'primed': len(allk)}
    def _tkey(x):                         # tożsamość TRADE'a (bez katalizatora) — do scalania duplikatów
        return "T|%s|%s|%s|%s|%.1f|%.1f" % (x['date'], x['model'], x['dir'], x['bos'],
                                            float(x['entry']), float(x['SL']))
    fresh=[x for x,k in zip(setups,keys) if k not in sent and _tkey(x) not in sent]
    sentn=set(sent)
    # v21: GAP-AWARE RE-PRIME — po przerwie w feedzie (outage LUB okno redeployu) pomin katch-up batch.
    # Po dziurze poziomy (PDH / H sesji) sa liczone W POPRZEK dziury -> stale. Oznacz wszystko widziane,
    # NIE alarmuj; swieze setupy ida od nastepnego (juz ciaglego) bara.
    _gap = _feed_gap_min()
    if _gap is not None and _gap > float(os.environ.get('GAP_REPRIME_MIN','30')):
        for x in setups: sentn.add(live_emit.key(x)); sentn.add(_tkey(x))
        _save_sent(sentn)
        print('GAP RE-PRIME: feed wrocil po %.0f min — pomijam %d katch-up setupow (stale poziomy)' % (_gap, len(setups)), flush=True)
        if WEBHOOK_URL:
            try: live_emit.post_webhook(f"♻️ Feed wrócił po przerwie ~{_gap:.0f} min — pomijam katch-up (poziomy policzone w poprzek dziury). Świeże setupy od następnego bara.", WEBHOOK_URL)
            except Exception as e: print('[gap-reprime] post err', e, flush=True)
        return {'gap_reprime': len(setups), 'gap_min': round(_gap,1)}
    fresh_ms = int(os.environ.get('FRESH_MIN','15'))*60*1000   # strażnik świeżości: alarmuj tylko swieze
    max_retest = int(os.environ.get('MAX_RETEST','0'))         # 0 = bez limitu; np. 4 = nie alarmuj po 4. re-teście
    live=[]                               # po filtrze świeżości
    for x in fresh:
        if now_ms and x.get('bos_ms') and (now_ms - x['bos_ms']) > fresh_ms:
            print('STALE skip (stary setup, nie alarmuje):', live_emit.key(x), flush=True)
            sentn.add(live_emit.key(x)); continue
        live.append(x)
    # --- SCAL DUPLIKATY: ten sam trade (entry/SL/BOS) z wielu katalizatorów = JEDNA wiadomość ---
    groups={}
    for x in live:
        groups.setdefault(_tkey(x), []).append(x)
    nfired=0
    # --- v16 REGIME SIZE GATE (opt-in): adaptacja rozmiaru / pomijanie w choppy ---
    _rsg = os.environ.get('REGIME_SIZE_GATE','')=='1'; _rskip = os.environ.get('REGIME_SKIP_CHOP','')=='1'
    _rcolor=None; _rlabel=''; _rfac=1.0
    if _rsg or _rskip:
        _reg=_regime_now() or {}
        _rcolor=_reg.get('market_color') or _reg.get('state')
        _rlabel=_reg.get('market_type','?'); _rfac={'green':1.0,'amber':0.5,'red':0.25}.get(_rcolor,1.0)
    for tk, members in groups.items():
        rep=sorted(members, key=lambda m:(live_emit.grade(m)=='A', m.get('bias_align')=='Y',
                                          int(m.get('brk',1))), reverse=True)[0]
        allkeys=[live_emit.key(m) for m in members] + [tk]
        if max_retest and min(int(m.get('brk',1)) for m in members) > max_retest:   # filtr re-testów
            print('RETEST skip (za duzo re-testow, min brk>%d):' % max_retest, tk, flush=True)
            for kk in allkeys: sentn.add(kk)
            continue
        cats=[]
        for m in members:
            c=live_emit.catname(m)
            if c not in cats: cats.append(c)
        merged=' + '.join(cats) + ('+DIB' if live_emit.grade(rep)=='B' else '')
        repx=dict(rep); repx['cat']=merged
        fl, hard = flags_for(rep)
        txt=live_emit.to_alert(repx)
        _age=(now_ms-rep['bos_ms'])/60000.0 if (now_ms and rep.get('bos_ms')) else None   # v20: stempel swiezosci
        _hdr='🕒 '+dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')+((f' · setup sprzed {_age:.0f} min'+(' ⚠️ STARY!' if _age>20 else '')) if _age is not None else '')
        txt=_hdr+'\n'+txt                                                                   # pierwsza linia = KIEDY -> stary alert widac na pierwszy rzut oka
        try:                                                                                # v22: ⭐ SELECT tag (tier T4, AB_AUDIT_6K_2026-07) — tylko oznaczenie, zero zmian logiki
            import select_tag as _sel
            _st=_sel.tagline(repx, members)
            if _st: txt=_st+txt
        except Exception as _se: print('select_tag err', _se, flush=True)
        if len(members)>1:
            txt += f"\n🔗 Konfluencja {len(members)}× ({' + '.join(cats)}) — jeden trade, nie {len(members)} osobne"
        if PUBLIC_URL: txt += '  📊 ' + PUBLIC_URL.rstrip('/') + '/chart?key=' + live_emit.key(rep).replace('|','%7C').replace(' ','%20').replace(':','%3A')
        if fl: txt += '  ⚠ ' + ', '.join(fl)
        if _rskip and _rcolor=='red':                        # regime gate: w choppy nie alarmuj (edge ~0 po kosztach)
            print('CHOP-SKIP', txt, flush=True); _save_db(repx, txt+' [CHOP-SKIP]', 'chop-skip')
            for kk in allkeys: sentn.add(kk)
            continue
        if _rsg: txt += f"\n🌡️ Reżim: {_rlabel} — sugerowany rozmiar {_rfac}× (chop = mniejszy/odpuść)"
        if hard and NO_TRADE_SUPPRESS:                       # twarde wyciszenie tylko jak wlaczone
            print('SUPPRESS (high-impact)', txt, flush=True)
            _save_db(repx, txt+' [SUPPRESSED]', 'suppressed')
            for kk in allkeys: sentn.add(kk)
            continue
        if os.environ.get('EXEC_WEBHOOK'):
            _exec_order(repx, txt)   # route 2: JEDNA wiadomość (alert + przyciski) przez relay /stage
            code='exec'
        else:
            code=live_emit.post_webhook(txt, WEBHOOK_URL) if WEBHOOK_URL else 'no-url'
        print('ALERT', code, txt, flush=True)
        _save_db(repx, txt, code)
        try: manage.register(repx, TRADES)
        except Exception as e: print('manage.register err', e, flush=True)
        try: shadow.record('A/B', repx.get('dir'), repx.get('entry'), repx.get('SL'), repx.get('TP'), repx.get('bos_ms'))  # LIVE shadow log (hands-off, no money)
        except Exception as e: print('shadow.record err', e, flush=True)
        if code=='exec' or (WEBHOOK_URL and str(code).startswith('2')) or not WEBHOOK_URL:
            for kk in allkeys: sentn.add(kk)
        nfired+=1
    # ====== (usunięte) PRE-ALERTY — stary etap odbicia od CE „czekaj na BOS" zniesiony.
    # v10: wejście to LIMIT stawiany PO potwierdzeniu BOS, wysyłany przez to_alert powyżej.
    _save_sent(sentn)
    return {'nowe': nfired}

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
        try:                                              # sledzenie 1R/3R — nie moze ruszyc intake'u
            _hi=float(b['high']); _lo=float(b['low'])
            def _msend(m):
                print('MANAGE', m, flush=True)
                if WEBHOOK_URL: live_emit.post_webhook(m, WEBHOOK_URL)
            manage.check(_hi, _lo, now_ms, _msend, TRADES, outcomes_path=OUTCOMES)
        except Exception as e:
            print('manage.check err', e, flush=True)
        try: shadow.refresh()                       # resolve shadow trades on every bar (not only when tab open)
        except Exception as e: print('shadow.refresh err', e, flush=True)
        nb=(sum(1 for _ in open(BUF))-1) if os.path.exists(BUF) else 0
        _last.update(last_bar=str(b.get('ts_event')), bars_in_buffer=nb,
                     setups_seen=res.get('nowe', res.get('primed')),
                     processed_at=dt.datetime.utcnow().isoformat(timespec='seconds'))
        print(f"[bars] {b.get('ts_event')} buf={nb} -> {res}", flush=True)
    # --- Strategy F: przekaz bar do serwisu F (fire-and-forget; POZA lockiem; NIE wplywa na A/B) ---
    _furl = os.environ.get('STRAT_F_FORWARD_URL', '')
    if _furl and requests is not None:
        try: requests.post(_furl, json=b, timeout=3)
        except Exception: pass
    # --- Strategy C: DOKŁADNIE jak F — przekaz bar do serwisu C (fire-and-forget; NIE wplywa na A/B) ---
    _curl = os.environ.get('STRAT_C_FORWARD_URL', '')
    if _curl and requests is not None:
        try: requests.post(_curl, json=b, timeout=3)
        except Exception: pass
    # --- Strategy AMD: DOKŁADNIE jak F/C — przekaz bar do serwisu AMD (fire-and-forget; NIE wplywa na A/B) ---
    _amdurl = os.environ.get('STRAT_AMD_FORWARD_URL', '')
    if _amdurl and requests is not None:
        try: requests.post(_amdurl, json=b, timeout=3)
        except Exception: pass
    return jsonify(ok=True, **res)

def _wants_html():
    return 'text/html' in request.headers.get('Accept', '')

_VIEW_CSS = ("<style>body{background:#0a0a0a;color:#ebebeb;font-family:system-ui,sans-serif;margin:0;padding:16px}"
 "h1{font-size:18px;margin:0 0 2px}.sub{color:#555;font:11px monospace;margin-bottom:10px}"
 ".nav{margin-bottom:12px;font:11px monospace}.nav a{color:#22d3ee;text-decoration:none;margin-right:14px}"
 ".sum{color:#8a8a8a;font:12px monospace;margin-bottom:8px}"
 ".wrap{overflow-x:auto;border:1px solid #262626;border-radius:6px}"
 "table{border-collapse:collapse;width:100%;font:12px monospace}"
 "th{position:sticky;top:0;background:#1c1c1c;color:#666;text-align:left;padding:7px 9px;"
 "font:9px monospace;letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid #2a2a2a;white-space:nowrap}"
 "td{padding:6px 9px;border-bottom:1px solid #1a1a1a;white-space:nowrap}"
 "tr:hover td{background:#161616}tr.new td{background:#102a1a}tr.new td:first-child{border-left:2px solid #4ade80}"
 ".bdg{background:#4ade80;color:#04210f;font:8px monospace;padding:1px 5px;border-radius:3px;margin-right:6px;text-transform:uppercase}"
 ".empty{padding:20px;color:#555;font:12px monospace}</style>")
_F_URL = os.environ.get('STRAT_F_URL', 'https://strategy-f-production.up.railway.app').rstrip('/')
_ORB_URL = os.environ.get('STRAT_ORB_URL', 'https://strategy-orb-production.up.railway.app').rstrip('/')
_VIEW_NAV = ("<div class='nav'><a href='/'>home</a><a href='/pnl'>P&amp;L</a><a href='/journal'>journal</a><a href='/candidates'>candidates</a><a href='/how'>how</a><a href='/all/trades'>all·trades</a><a href='/all/reconcile'>reconcile</a>"
 "<a href='/regime'>regime</a><a href='/status'>status</a><a href='/monitor'>monitor</a>"
 "<span style='color:#444'>&nbsp;|&nbsp;F:</span>"
 f"<a href='{_F_URL}/candidates'>F·candidates</a><a href='{_F_URL}/log'>F·log</a>"
 f"<a href='{_F_URL}/performance_f'>F·perf</a>"
 "<span style='color:#444'>&nbsp;|&nbsp;ORB:</span>"
 f"<a href='{_ORB_URL}/'>ORB·dashboard</a><a href='{_ORB_URL}/how'>ORB·how</a>"
 "<span style='color:#444'>&nbsp;|&nbsp;C:</span>"
 "<a href='/c'>C·dashboard</a><a href='/c/candidates'>C·candidates</a><a href='/c/performance'>C·perf</a></div>")
_TIMEKEYS = ('bos_ms','entry_ms','trig_ms','bos','ts','date','id')
_PREF = ['date','bos','time','dir','cat','model','entry','SL','T1','T2','T3','TP','stage','result','pnl','rr']

def _page(title, body):
    return ("<!DOCTYPE html><html lang='pl'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" + _VIEW_CSS +
            "</head><body><h1>" + title + "</h1><div class='sub'>odśwież stronę, by zaktualizować</div>"
            + _VIEW_NAV + body + "</body></html>")

def _table(rows):
    import html as _h
    if not rows: return "<div class='empty'>brak rekordów</div>"
    def tv(r):
        for k in _TIMEKEYS:
            if r.get(k) is not None:
                try: return float(r[k])
                except Exception: return 0.0
        return 0.0
    rows = sorted(rows, key=tv, reverse=True)
    allk = []
    for r in rows:
        for k in r:
            if k not in allk: allk.append(k)
    keys = [k for k in _PREF if k in allk] + [k for k in allk if k not in _PREF]
    th = ''.join("<th>%s</th>" % _h.escape(str(k)) for k in keys)
    trs = ''
    for i, r in enumerate(rows):
        tds = ''
        for j, k in enumerate(keys):
            v = _h.escape(str(r.get(k, '')))
            if i == 0 and j == 0: v = "<span class='bdg'>najnowszy</span>" + v
            tds += "<td>%s</td>" % v
        trs += "<tr class='%s'>%s</tr>" % ('new' if i == 0 else '', tds)
    return "<div class='wrap'><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (th, trs)

def _kv_page(title, d):
    import html as _h
    body = "<div class='wrap'><table><tbody>"
    for k, v in d.items():
        val = json.dumps(v) if isinstance(v, (dict, list)) else v
        body += "<tr><th style='width:170px'>%s</th><td>%s</td></tr>" % (_h.escape(str(k)), _h.escape(str(val)))
    return _page(title, body + "</tbody></table></div>")

@app.route('/performance')
def performance():
    outs=[]
    try: outs=json.load(open(OUTCOMES))
    except Exception: outs=[]
    res=[o for o in outs if o.get('r') is not None]
    rs=[float(o['r']) for o in res]
    def _st(a):
        if not a: return dict(n=0)
        w=sum(1 for x in a if x>0)
        return dict(n=len(a), exp_R=round(sum(a)/len(a),3), win_pct=round(100*w/len(a),1), total_R=round(sum(a),1))
    timeouts=sum(1 for o in outs if o.get('reason')=='timeout')
    body=dict(live_all=_st(rs), live_last20=_st(rs[-20:]), live_last50=_st(rs[-50:]),
              recorded=len(outs), timeouts=timeouts,
              backtest_ref={'favorable_R':0.29,'weak_R':0.10},
              note='LIVE modeled R (agent fills na dotk. ceny). Porownaj exp_R do backtest_ref.')
    if _wants_html(): return _kv_page('Performance (LIVE)', body)
    return jsonify(**body)

@app.route('/outcomes')
def outcomes():
    import datetime as _dt, html as _html
    from urllib.parse import quote as _q
    try: outs = json.load(open(OUTCOMES))
    except Exception: outs = []
    outs = list(reversed(outs))              # newest first
    if not _wants_html():
        return jsonify(n=len(outs), outcomes=outs)
    def _tm(ms):
        try: return _dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M')
        except Exception: return ''
    if not outs:
        return _page('Trades (0)', "<div class='empty'>brak trade'ow jeszcze - czekaj na zamkniecie setupu.</div>")
    rws = ''
    for i, o in enumerate(outs):
        r = float(o.get('r', 0) or 0)
        rc = '#4ade80' if r > 0 else ('#f87171' if r < 0 else '#8a8a8a')
        lk = '/chart?key=' + _q(str(o.get('key', '')))
        snip = _html.escape(_pine_wrap(_pine_trade_lines(o) or [], 'Trade %s' % o.get('cat', '')))
        pine_cell = ("<button onclick=\"navigator.clipboard.writeText(document.getElementById('p%d').value);this.textContent='copied'\" "
                     "style='padding:3px 9px;background:#22d3ee;color:#04202a;border:0;border-radius:5px;font-weight:700;cursor:pointer'>Pine</button>"
                     "<textarea id='p%d' style='display:none'>%s</textarea>") % (i, i, snip)
        rws += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td style='color:%s;font-weight:700'>%+.0fR</td><td>%s</td>"
                "<td><a href='%s' target='_blank' style='color:#22d3ee'>chart</a></td><td>%s</td></tr>") % (
                _tm(o.get('closed_ms')), o.get('dir', ''), o.get('cat', ''), o.get('entry', ''),
                o.get('sl', ''), rc, r, o.get('reason', ''), lk, pine_cell)
    tbl = ("<p class='mut'>Each row: <b>Pine</b> copies a standalone one-trade script; <b>chart</b> opens the built-in candle view. "
           "For all trades on one TradingView chart, use the <b>Pine for TV</b> tab.</p>"
           "<table><thead><tr><th>Closed</th><th>Dir</th><th>Catalyst</th><th>Entry</th><th>SL</th>"
           "<th>Result</th><th>Reason</th><th>Chart</th><th>Pine</th></tr></thead><tbody>" + rws + "</tbody></table>")
    return _page('Trades (%d)' % len(outs), tbl)


def _pine_trade_lines(o):
    try:
        e = float(o.get('entry')); sl = float(o.get('sl'))
    except Exception:
        return None
    bos = int(o.get('bos_ms') or 0)
    if not bos:
        return None
    cl = int(o.get('closed_ms') or bos) or bos
    r = float(o.get('r', 0) or 0)
    gc = 'color.green' if r > 0 else ('color.red' if r < 0 else 'color.gray')
    top = max(e, sl); bot = min(e, sl)
    tp = e + 2.0 * (e - sl)                       # sign puts TP on the correct side
    side = 'LONG' if e > sl else 'SHORT'
    txt = ('%s %s %+.0fR %s' % (o.get('cat', ''), side, r, o.get('reason', ''))).replace('"', '').replace(chr(10), ' ').strip()
    return ['    box.new(%d, %.5f, %d, %.5f, xloc=xloc.bar_time, border_color=%s, bgcolor=color.new(%s, 88))' % (bos, top, cl, bot, gc, gc),
            '    line.new(%d, %.5f, %d, %.5f, xloc=xloc.bar_time, color=%s, width=2)' % (bos, e, cl, e, gc),
            '    line.new(%d, %.5f, %d, %.5f, xloc=xloc.bar_time, color=color.new(color.teal, 0), style=line.style_dotted)' % (bos, tp, cl, tp),
            '    label.new(%d, %.5f, "%s", xloc=xloc.bar_time, style=label.style_label_down, color=%s, textcolor=color.white, size=size.small)' % (bos, top, txt, gc)]


def _pine_wrap(bodylines, title):
    head = ['//@version=5',
            'indicator("%s", overlay=true, max_boxes_count=500, max_labels_count=500, max_lines_count=500)' % title,
            'if barstate.islast']
    body = bodylines if bodylines else ['    label.new(bar_index, high, "no closed trades yet", style=label.style_label_down)']
    return '\n'.join(head + body)


def _pine_src(outs, title):
    body = []
    for o in outs:
        ln = _pine_trade_lines(o)
        if ln:
            body += ln
    return _pine_wrap(body, title)


@app.route('/pine')
def pine():
    import html as _html
    from flask import Response
    try: outs = json.load(open(OUTCOMES))
    except Exception: outs = []
    title = 'Forex trades - ' + (os.environ.get('FOREX_INSTRUMENT', '') or 'agent').upper()
    src = _pine_src(outs, title)
    if request.args.get('raw'):
        return Response(src, mimetype='text/plain')
    body = ("<p class='mut'>Copy this &rarr; TradingView &rarr; <b>Pine Editor</b> &rarr; paste &rarr; <b>Add to chart</b> "
            "(matching pair, any intraday timeframe). Your %d trades draw as boxes (entry&rarr;stop), an entry line, "
            "a dotted 2R target, and a label. Green = win, red = loss, gray = break-even.</p>"
            "<button onclick=\"navigator.clipboard.writeText(document.getElementById('psrc').value);this.textContent='Copied'\" "
            "style='margin:6px 0;padding:8px 14px;background:#22d3ee;color:#04202a;border:0;border-radius:6px;font-weight:700;cursor:pointer'>Copy script</button>"
            " <a href='/pine?raw=1' target='_blank' style='color:#22d3ee;margin-left:8px'>raw</a>"
            "<textarea id='psrc' readonly style='width:100%%;height:60vh;background:#0d0d0d;color:#d6d6d6;"
            "border:1px solid #222;border-radius:8px;padding:10px;font:12px/1.45 monospace;box-sizing:border-box'>%s</textarea>"
            ) % (len(outs), _html.escape(src))
    return _page('Pine script - %d trades' % len(outs), body)


@app.route('/lastalert')
def lastalert():
    import html as _h
    nlim=int(request.args.get('n','3'))
    rows=[]
    try:
        c=sqlite3.connect(DB)
        for r in c.execute("SELECT logged_at,dir,cat,entry,SL,TP,alert FROM signals ORDER BY logged_at DESC LIMIT ?",(nlim,)):
            rows.append(dict(logged_at=r[0],dir=r[1],cat=r[2],entry=r[3],SL=r[4],TP=r[5],alert=r[6]))
        c.close()
    except Exception as e:
        return jsonify(error=str(e)), 500
    if _wants_html():
        body="".join("<pre style='white-space:pre-wrap'>%s</pre><hr>"%_h.escape(str(x.get('alert') or '')) for x in rows)
        return _page('Ostatnie alerty (pelne SL/TP)', "<div class='wrap'>"+body+"</div>")
    return jsonify(n=len(rows), alerts=rows)

@app.route('/status')
def status():
    nb=(sum(1 for _ in open(BUF))-1) if os.path.exists(BUF) else 0
    na=(sum(1 for _ in open(ARCHIVE))-1) if os.path.exists(ARCHIVE) else 0
    _last['bars_in_buffer']=nb
    _age=_feed_age_min(); _mkt=_market_open_now()                  # v21: zdrowie feedu wprost w /status
    try: _cme=cme_calendar.status().get('note','')                 # v22: DLACZEGO rynek zamkniety (swieto/early close)
    except Exception: _cme=''
    _body=dict(version=VERSION, primed=_primed, archive_bars=na, **_last,
               feed_age_min=round(_age,1), market_open=_mkt, cme_note=_cme,
               feed_ok=bool(_age<=STALE_MIN or not _mkt),          # OK = swiezy LUB rynek zamkniety
               heartbeat=HEARTBEAT, healthcheck=bool(os.environ.get('HEALTHCHECK_URL')))
    if _wants_html(): return _kv_page('Status', _body)
    return jsonify(_body)

@app.route('/archive')
def archive():
    if not os.path.exists(ARCHIVE): return jsonify(error='brak archiwum jeszcze'), 404
    return send_file(ARCHIVE, mimetype='text/csv', as_attachment=True, download_name='archive.csv')

@app.route('/exectest')
def exectest():
    """Route 2 test: wyślij PRZYKŁADOWE zlecenie do TradersPost (EXEC_WEBHOOK) + ping Telegram.
    Wymaga EXEC_TEST_SECRET (env) i ?secret=. Bezpieczne: TradersPost (manual submit) trzyma je jako
    oczekujące dopóki nie zatwierdzisz. Param: ?dir=LONG&entry=29700&sl=29690"""
    sec = os.environ.get('EXEC_TEST_SECRET', '')
    if not sec or request.args.get('secret', '') != sec:
        return jsonify(error='ustaw EXEC_TEST_SECRET (env) i podaj ?secret=...'), 401
    side = request.args.get('dir', 'LONG').upper()
    entry = float(request.args.get('entry', '29700'))
    sl = float(request.args.get('sl', str(entry - 10 if side == 'LONG' else entry + 10)))
    sample = {'dir': side, 'entry': entry, 'SL': sl}
    relay = _exec_order(sample)   # -> relay /stage -> JEDNA wiadomość z przyciskami
    return jsonify(ok=True, exec_webhook_set=bool(os.environ.get('EXEC_WEBHOOK')), relay=relay,
                   ticker=os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!')),
                   sample=sample, note='relay.status 200 = relay przyjął (sprawdź Telegram); 401 = zły secret w EXEC_WEBHOOK; sent:false = zły URL/sieć')

@app.route('/health')
def health(): return jsonify(ok=True, version=VERSION, primed=_primed, webhook=bool(WEBHOOK_URL), buffer=os.path.exists(BUF))

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
    c.close()
    if _wants_html(): return _page('Journal', _table(rows))
    return jsonify(signals=rows)

@app.route('/candidates')
def candidates():
    import json as _json
    from collections import Counter
    hours=float(request.args.get('hours','12'))
    tout='/tmp/cand_trace.json'   # v22-fix: /tmp is always writable — a stuck/locked /data file can no longer freeze this page
    gated = os.environ.get('REGIME_GATE','')=='1'
    det_file = os.environ.get('DET_FILE', 'det_v11.py')   # v20: v11 (detcore) = live detector; DET_FILE nadpisuje
    env=dict(os.environ, DATA_CSV=BUF, OUT_PKL='/tmp/cand_out.pkl',
             CUTOFF='', DEBUG_TRACE='1', TRACE_OUT=tout)
    if gated: env['EOD_INTRADAY']='1' if _eod_flag() else ''
    # /candidates = DIAGNOSTYKA: ten SAM detektor co live (det_v11 gdy REGIME_GATE=1), z DEBUG_TRACE. Nie dotyka alertow.
    try:                                                   # v22-fix: bigger timeout + surface the error instead of swallowing it
        _r = subprocess.run(['python3', os.path.join(HERE,det_file)], env=env, capture_output=True, timeout=600)
        if _r.returncode != 0:
            print('[candidates] det rc=%s STDERR:\n%s' % (_r.returncode, (_r.stderr or b'').decode('utf-8','replace')[-3000:]), flush=True)
    except subprocess.TimeoutExpired:
        print('[candidates] det TIMEOUT >600s — trace not refreshed', flush=True)
    except Exception as _e:
        print('[candidates] det EXC:', _e, flush=True)
    try: tr=_json.load(open(tout))
    except Exception: tr=[]
    cut=int((dt.datetime.utcnow().timestamp()-hours*3600)*1000)
    rec=[r for r in tr if r.get('trig_ms',0)>=cut]
    rec.sort(key=lambda r:r.get('trig_ms',0), reverse=True)   # newest first
    if _wants_html():
        _summ=' · '.join("%s: %s"%(k,v) for k,v in Counter(r['stage'] for r in rec).items())
        return _page('Candidates (%gh)'%hours, "<div class='sum'>etapy: %s</div>"%_summ + _table(rec))
    return jsonify(hours=hours, liczba=len(rec),
                   podsumowanie=dict(Counter(r['stage'] for r in rec)), kandydaci=rec)

# ====== MONITOR REŻIMU (logika w regime.py — rdzen det_v10.py nietkniety) ======
@app.route('/regime')
def regime():
    try: w=int(request.args.get('window','20'))
    except Exception: w=20
    import regime as _regime
    _st=_regime.regime_stats(BUF, HERE, window=w)
    if _wants_html(): return _kv_page('Reżim', _st)
    return jsonify(_st)

@app.route('/monitor')
def monitor():
    from flask import send_from_directory
    return send_from_directory(HERE, 'regime_monitor.html')

def _feed_gap_min():
    """Minuty miedzy dwoma ostatnimi barami w buforze (czytane z dysku -> przezywa restart/redeploy).
    Wykrywa dziure w feedzie: outage (dni) albo okno redeployu (sekundy/minuty)."""
    try:
        with open(BUF) as f: rows = f.readlines()
        if len(rows) < 3: return None
        def _ms(line):
            ts = line.split(',')[0].strip()
            if '+' not in ts and 'Z' not in ts: ts = ts + '+00:00'
            return dt.datetime.fromisoformat(ts).timestamp() * 1000
        return (_ms(rows[-1]) - _ms(rows[-2])) / 60000.0
    except Exception: return None


def _market_open_now():
    """True when CME Globex MNQ should be delivering bars.
    v22: deleguje do cme_calendar (tygodniowy schedule + swieta/early-close, zegar gieldy America/Chicago
    — poprawny w DST, w przeciwienstwie do starego sztywnego UTC-4 ktory zima rozjezdzal sie o 1h).
    Fallback do starej logiki weekly-only gdyby modul kiedykolwiek rzucil — watchdog nie moze umrzec."""
    try:
        return cme_calendar.market_open()
    except Exception as e:
        print('[heartbeat] cme_calendar err (fallback weekly-only):', e, flush=True)
        t = dt.datetime.now(NY); wd = t.weekday(); m = t.hour * 60 + t.minute
        if wd == 5: return False                       # Saturday
        if wd == 6 and m < 18 * 60: return False       # Sunday before 18:00 ET
        if wd == 4 and m >= 17 * 60: return False      # Friday after 17:00 ET
        if 17 * 60 <= m < 18 * 60: return False        # daily maintenance halt
        return True

def _feed_age_min():
    """Minutes since the last /bars was processed (or since process start if none yet)."""
    last = _last.get('processed_at'); ref = None
    if last:
        try: ref = dt.datetime.fromisoformat(last)
        except Exception: ref = None
    if ref is None: ref = _START
    return (dt.datetime.utcnow() - ref).total_seconds() / 60.0

def _heartbeat_loop():
    """Alert Telegram ONCE when the feed goes stale during market hours, and once when it recovers.
    Never raises — a watchdog that can crash is worse than none."""
    while True:
        try:
            _time.sleep(HEARTBEAT_EVERY)
            _hc = os.environ.get('HEALTHCHECK_URL', '')   # v21: zewnetrzny dead-man's switch (np. Healthchecks.io)
            if _hc and requests is not None:               # ping co cykl = dowod ze AGENT zyje; gdy padnie, brak pingu -> zewn. alarm
                try: requests.get(_hc, timeout=4)
                except Exception: pass
            if not (HEARTBEAT and WEBHOOK_URL and requests is not None): continue
            age = _feed_age_min()
            stale = age > STALE_MIN and _market_open_now()
            if stale and not _hb['alerted']:
                msg = (f"⚠️ AGENT FEED STALE — brak nowego bara od {age:.0f} min "
                       f"(ostatni: {_last.get('last_bar')}). Detektor NIE dostaje danych — zero alertów do naprawy.\n"
                       f"Sprawdź: (1) alert TradingView → /bars (najczęstsza przyczyna), (2) Railway nie śpi / redeploy.")
                if PUBLIC_URL: msg += f"\n{PUBLIC_URL}/status"
                try: live_emit.post_webhook(msg, WEBHOOK_URL)
                except Exception as e: print('[heartbeat] post err', e, flush=True)
                _hb['alerted'] = True
                print('[heartbeat] STALE alert sent, age=%.0f min' % age, flush=True)
            elif (not stale) and _hb['alerted'] and age <= STALE_MIN:
                try: live_emit.post_webhook(f"✅ AGENT FEED WRÓCIŁ — bary znowu spływają (ostatni: {_last.get('last_bar')}).", WEBHOOK_URL)
                except Exception as e: print('[heartbeat] post err', e, flush=True)
                _hb['alerted'] = False
                print('[heartbeat] RECOVERED', flush=True)
        except Exception as e:
            print('[heartbeat] loop err', e, flush=True)   # never die

_init_db(); _seed_buffer()
pnl.register(app, DB, render_page=_page, wants_html=_wants_html)   # /pnl unified journal (isolated add-on)
how_ab.register(app)                        # /how — A/B explainer page (ORB /how style, isolated add-on)
dashboard.register(app)                     # /    — unified home shell (federates existing pages, isolated add-on)
shadow.register(app)                        # /shadow/data + /shadow/log — live shadow-executor log (isolated add-on)
forex_pnl.register(app)                     # /forexpnl - joined forex P&L (isolated add-on)
allview.register(app)                       # /all/trades + /all/candidates - joined view (isolated add-on)
if HEARTBEAT:
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print(f'[heartbeat] on — co {HEARTBEAT_EVERY:.0f}s, stale po {STALE_MIN:.0f} min (godziny rynkowe)', flush=True)

# ── Strategy C — OSOBNY serwis (własny proces + detekcja). Agent tylko PROXY-uje jego stronę pod /c
#    przez sieć WEWNĘTRZNĄ Railway (C_URL, np. http://strategy-c.railway.internal:8080). C nie ma
#    publicznej domeny — wchodzisz do niego przez agenta. A/B (kod, /bars, detektor) — nietknięte.
@app.route('/c', defaults={'_p': ''})
@app.route('/c/<path:_p>')
def _c_proxy(_p):
    from flask import Response, request as _rq
    base = os.environ.get('C_URL', '')
    if not base: return ('Ustaw C_URL = wewnętrzny adres serwisu C (np. http://strategy-c.railway.internal:8080)', 503)
    if requests is None: return ('requests missing', 503)
    try:
        r = requests.get(base.rstrip('/') + '/' + _p, params=_rq.args, timeout=15)
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get('content-type', 'text/html; charset=utf-8'))
    except Exception as _e:
        return ('Strategy C nieosiągalny (' + str(_e) + ')', 502)

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8000')))
