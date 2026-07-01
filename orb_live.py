#!/usr/bin/env python3
"""
orb_live.py — LIVE signal generator for STRATEGY ORB (Trend-Day Opening-Range Breakout).

A MOMENTUM strategy, structurally the OPPOSITE of A/B/C/F (no FVG, no retrace). It is NOT ICT.
Independent research (4yr MNQ) — see MNQ_DEEP_RESEARCH.md:
  * edge lives on trend-from-open days; positive every year 2022–2026, robust across targets;
  * SURVIVES realistic fills (unlike F): stop-limit at the range edge = +0.176R;
  * daily-P&L correlation with A/B = +0.10 → a genuine diversifier.

SAFE BY DESIGN — does NOT touch A/B, C or F (built exactly like model_c_live.py):
  * Runs as its OWN process. Reads the shared bar buffer READ-ONLY (pandas; detcore NOT required).
  * Own dedup file (SENT_ORB_FILE), own Telegram webhook (STRAT_ORB_WEBHOOK), own TradersPost (EXEC_WEBHOOK_ORB).
  * With STRAT_ORB_ENABLED unset it does nothing (dry-run prints only).

WHAT IT DOES each poll (1-min cron or --loop):
  1. Read the agent's bar buffer, convert to America/New_York (real cash-open time, DST-aware).
  2. Build TODAY's opening range = high/low of 09:30–09:44 ET (ORB_MIN default 15).
  3. After 09:45, watch for the FIRST 1-min CLOSE beyond the range (long above, short below).
  4. Filters (default): only breakouts by ORB_LATE_CUTOFF (10:30 ET); trade WITH the 20-day regime.
     Optional boosters: ORB_FRIDAY_ONLY, ORB_REQUIRE_GAP (trend-day tells).
  5. Order = STOP-LIMIT at the range edge (NOT a plain limit — a breakout is momentum):
     stopPrice=edge, limitPrice=edge±slip, SL=opposite edge (=1R), TP=2R, GTC. (BE@1R optional.)
  6. Alert (distinct 🅾 wording) + optionally stage a TradersPost bracket (agent._exec_order schema).

ENV (nothing fires unless STRAT_ORB_ENABLED=1 and a webhook is set):
  STRAT_ORB_ENABLED=1
  STRAT_ORB_BUF or BUF              path to the agent bar buffer CSV (read-only)
  STRAT_ORB_WEBHOOK or WEBHOOK_URL  Telegram /webhook for ORB alerts
  EXEC_WEBHOOK_ORB                  TradersPost relay for ORB (own strategy recommended)
  EXEC_TICKER_ORB (def EXEC_TICKER/CONTRACT/'MNQ1!') · EXEC_MAX_QTY_ORB · PRICE_OFFSET
  SENT_ORB_FILE (def /home/claude/sent_signals_ORB.json) · ORB_TRADES_FILE
  ACCOUNT / RISK_PCT / POINT_VALUE  (position sizing, shared with the rest of your stack)
  Detector/filters (defaults are the researched values):
    ORB_MIN=15  ORB_LATE_CUTOFF=10:30  ORB_TARGET_R=2.0  ORB_SLIP_TICKS=2  ORB_NOBE=0
    ORB_REQUIRE_BIAS=1  ORB_FRIDAY_ONLY=0  ORB_REQUIRE_GAP=0  ORB_MIN_OR_PTS=4
    ORB_FRESH_MIN=5   (only alert a break detected within the last N minutes -> no stale alerts on restart)
    ORDER_TYPE=stopLimit  (stopLimit | stop | limit)   MA_DAYS=20
"""
import os, sys, json, time, math
import datetime as dt
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)          # import live_emit (sizing + telegram) read-only, like strategy_f
import pandas as pd, numpy as np
try:
    import live_emit
except Exception:
    live_emit = None
try:
    import requests
except Exception:
    requests = None

ET = ZoneInfo('America/New_York')

# ---------- self-contained fallbacks (work even if live_emit / parent repo is absent) ----------
def _size_for(entry, sl):
    if live_emit is not None:
        try: return live_emit.size_for(entry, sl)
        except Exception: pass
    try:
        acct = float(os.environ.get('ACCOUNT', '100000')); riskp = float(os.environ.get('RISK_PCT', '0.5'))
        ptval = float(os.environ.get('POINT_VALUE', '2')); risk_usd = acct * riskp / 100.0
        slpts = abs(float(entry) - float(sl))
        if slpts <= 0: return None
        qty = int(risk_usd // (slpts * ptval)); real = qty * slpts * ptval
        return qty, round(slpts, 1), round(slpts * ptval), round(real), round(real / acct * 100, 2)
    except Exception:
        return None

def _post_webhook(text, url):
    if live_emit is not None:
        try: return live_emit.post_webhook(text, url)
        except Exception: pass
    if requests is None: return 'no-requests'
    try:
        r = requests.post(url, data=text.encode('utf-8'), headers={'Content-Type': 'text/plain'}, timeout=10)
        return getattr(r, 'status_code', None)
    except Exception as e:
        return f'ERR {e}'

# ---------- config ----------
BUF        = os.environ.get('STRAT_ORB_BUF') or os.environ.get('BUF') or os.path.join(PARENT, 'buffer.csv')
WEBHOOK    = os.environ.get('STRAT_ORB_WEBHOOK') or os.environ.get('WEBHOOK_URL', '')
EXEC_ORB   = os.environ.get('EXEC_WEBHOOK_ORB', '')
SENT_ORB   = os.environ.get('SENT_ORB_FILE', '/home/claude/sent_signals_ORB.json')
ORB_TRADES = os.environ.get('ORB_TRADES_FILE') or os.path.join(os.path.dirname(SENT_ORB) or '.', 'orb_trades.json')
OFFSET     = float(os.environ.get('PRICE_OFFSET', '0'))
ENABLED    = os.environ.get('STRAT_ORB_ENABLED', '') == '1'

ORB_MIN      = int(os.environ.get('ORB_MIN', '15'))                 # opening-range length in minutes
LATE_CUTOFF  = os.environ.get('ORB_LATE_CUTOFF', '10:30')           # skip breakouts after this ET time
TARGET_R     = float(os.environ.get('ORB_TARGET_R', '2.5'))    # research: 2.5R > 2R (flat 2.5-3R plateau)
SLIP_TICKS   = float(os.environ.get('ORB_SLIP_TICKS', '2'))
TICK         = float(os.environ.get('TICK', '0.25'))
NOBE         = os.environ.get('ORB_NOBE', '1') == '1'          # research: BE@1R HURTS this strategy -> off by default
REQUIRE_BIAS = os.environ.get('ORB_REQUIRE_BIAS', '1') == '1'
FRIDAY_ONLY  = os.environ.get('ORB_FRIDAY_ONLY', '0') == '1'
REQUIRE_GAP  = os.environ.get('ORB_REQUIRE_GAP', '0') == '1'
MIN_OR_PTS   = float(os.environ.get('ORB_MIN_OR_PTS', '4'))
FRESH_MIN    = int(os.environ.get('ORB_FRESH_MIN', '5'))
ORDER_TYPE   = os.environ.get('ORDER_TYPE', 'stopLimit')            # stopLimit | stop | limit
MA_DAYS      = int(os.environ.get('MA_DAYS', '20'))

def _hhmm_to_min(s):
    h, m = s.split(':'); return int(h) * 60 + int(m)
CUT = _hhmm_to_min(LATE_CUTOFF)
OPEN_MIN = 9 * 60 + 30          # 09:30
ORB_END  = OPEN_MIN + ORB_MIN - 1

# ---------- buffer ----------
def load_buffer(path=BUF):
    df = pd.read_csv(path)
    tcol = 'ts_event' if 'ts_event' in df.columns else ('ts' if 'ts' in df.columns else df.columns[0])
    ts = pd.to_datetime(df[tcol], utc=True)
    df = df.assign(ts=ts).sort_values('ts').reset_index(drop=True)
    df['et'] = df['ts'].dt.tz_convert(ET)
    df['date'] = df['et'].dt.date
    df['mn'] = df['et'].dt.hour * 60 + df['et'].dt.minute
    for c in ('open', 'high', 'low', 'close'):
        df[c] = df[c].astype(float)
    return df

def regime_bias(df, today):
    """20-day SMA of prior RTH closes vs today's RTH open. Returns 'BULL'/'BEAR'/'?'."""
    rth = df[(df.mn >= OPEN_MIN) & (df.mn <= 15 * 60 + 59)]
    closes = rth.groupby('date')['close'].last()
    opens = rth.groupby('date')['open'].first()
    prior = closes[closes.index < today]
    if len(prior) < MA_DAYS or today not in opens.index:
        return '?', np.nan
    ma = prior.tail(MA_DAYS).mean()
    o = opens.loc[today]
    return ('BULL' if o > ma else 'BEAR'), round(float(ma), 2)

# ---------- signal ----------
def orb_signal(path=BUF):
    df = load_buffer(path)
    if df.empty: return None
    today = df['date'].iloc[-1]
    now_mod = int(df['mn'].iloc[-1])
    day = df[df.date == today].sort_values('mn')
    orw = day[(day.mn >= OPEN_MIN) & (day.mn <= ORB_END)]
    if len(orw) < max(5, ORB_MIN - 3):            # opening range not fully formed yet
        return None
    orh = float(orw['high'].max()); orl = float(orw['low'].min())
    if orh - orl < MIN_OR_PTS: return None
    post = day[day.mn > ORB_END].sort_values('mn')
    if post.empty: return None
    # first CLOSE beyond the range
    brk = None
    for _, b in post.iterrows():
        if b['close'] > orh: brk = ('LONG', b); break
        if b['close'] < orl: brk = ('SHORT', b); break
    if brk is None: return None
    side, bar = brk
    et_min = int(bar['mn'])
    # ---- filters ----
    if et_min > CUT: return None                                  # late breakout (loses in-sample)
    if FRIDAY_ONLY and pd.Timestamp(today).dayofweek != 4: return None
    gap = np.nan
    rthc = df[(df.mn >= OPEN_MIN) & (df.mn <= 15 * 60 + 59)].groupby('date')['close'].last()
    pc = rthc[rthc.index < today]
    if len(pc):
        gap = float(day['open'].iloc[0] - pc.iloc[-1])
    bias, ma = regime_bias(df, today)
    long = side == 'LONG'
    align = 'Y' if ((bias == 'BULL' and long) or (bias == 'BEAR' and not long)) else ('?' if bias == '?' else 'N')
    if REQUIRE_BIAS and align == 'N': return None                 # counter-regime -> skip
    if REQUIRE_GAP and (np.isnan(gap) or (gap > 0) != long or abs(gap) < 1): return None
    # ---- geometry ----
    edge = orh if long else orl
    stop = orl if long else orh
    R = abs(edge - stop)
    if R <= 0: return None
    entry = edge                                                   # stop-limit triggers here
    tp = entry + TARGET_R * R if long else entry - TARGET_R * R
    # freshness: only alert if the break bar is recent (avoid stale alerts on restart / backfill)
    fresh = (now_mod - et_min) <= FRESH_MIN
    return dict(date=str(today), dir=side, entry=round(entry, 2), SL=round(stop, 2), TP=round(tp, 2),
                risk=round(R, 2), or_hi=round(orh, 2), or_lo=round(orl, 2), or_pts=round(orh - orl, 1),
                brk_time=f"{et_min // 60:02d}:{et_min % 60:02d}", bias=bias, bias_align=align,
                gap=(None if np.isnan(gap) else round(gap, 1)), fresh=bool(fresh), strategy='ORB')

# ---------- alert ----------
def to_alert(x):
    isL = x['dir'] == 'LONG'; emoji = '🟢' if isL else '🔴'; rp = round(x['risk'], 1)
    trig = 'BUY STOP' if isL else 'SELL STOP'
    lim = round(x['entry'] + (SLIP_TICKS if isL else -SLIP_TICKS) * TICK + OFFSET, 2)
    be_line = "" if NOBE else f" · BE po +{rp} (1R)"
    gap_line = f" · gap {x['gap']:+.0f} pkt" if x.get('gap') is not None else ""
    base = (f"🅾 STRATEGY ORB · wybicie zakresu otwarcia (momentum, NIE ICT) · {emoji} {x['dir']} · NYAM"
            f"\n📋 {trig}-LIMIT: trigger {round(x['entry']+OFFSET,1)} → limit {lim}  (POSTAW TERAZ, GTC)"
            f"\n   ⤷ to NIE jest zwykły limit — wejście na WYBICIU krawędzi zakresu (stop-limit)"
            f"\n🛑 SL {round(x['SL']+OFFSET,1)} = przeciwna krawędź · ryzyko {rp} pkt ({rp*4:.0f} ticks){be_line}"
            f"\n🎯 TP {round(x['TP']+OFFSET,1)} · +{round(TARGET_R*x['risk'],1)} pkt ({TARGET_R:.0f}R)"
            f"\n📐 zakres 09:30–09:{29+ORB_MIN} = {x['or_pts']:.0f} pkt · wybicie {x['brk_time']}{gap_line}"
            f"\n🧭 reżim 20D: {x['bias']} → {('ZGODNY' if x['bias_align']=='Y' else x['bias_align'])}")
    s = _size_for(x['entry'], x['SL'])
    if s:
        qty, slpts, perc, real, pct = s
        base += f"\n📏 {qty} kontr. (SL {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)  ⟵ mniejszy rozmiar gdy zakres szeroki"
    base += "\n⚠ Strategy ORB — OSOBNY strumień. NIE myl z A/B, C ani F. Korelacja z A/B ≈ 0.10."
    return base

# ---------- TradersPost ----------
def td_payload(x, action='enter'):
    isL = x['dir'] == 'LONG'; e = float(x['entry']); sl = float(x['SL']); R = abs(e - sl)
    tp = (e + TARGET_R * R) if isL else (e - TARGET_R * R)
    qty = 1
    s = _size_for(e, sl); qty = int(s[0]) if s else 1
    cap = os.environ.get('EXEC_MAX_QTY_ORB', '').strip()
    if cap.isdigit() and int(cap) > 0: qty = min(qty, int(cap))
    qty = max(1, qty)
    lim = round(e + (SLIP_TICKS if isL else -SLIP_TICKS) * TICK + OFFSET, 2)
    p = {"ticker": os.environ.get('EXEC_TICKER_ORB', os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))),
         "action": ("buy" if isL else "sell") if action == 'enter' else "exit",
         "quantity": qty,
         "takeProfit": {"limitPrice": round(tp + OFFSET, 2)},
         "stopLoss": {"type": "stop", "stopPrice": round(sl + OFFSET, 2)},
         "timeInForce": "gtc", "strategy": "STRATEGY_ORB"}
    if ORDER_TYPE == 'stopLimit':
        p["orderType"] = "stopLimit"; p["stopPrice"] = round(e + OFFSET, 2); p["limitPrice"] = lim
    elif ORDER_TYPE == 'stop':
        p["orderType"] = "stop"; p["stopPrice"] = round(e + OFFSET, 2)
    else:
        p["orderType"] = "limit"; p["limitPrice"] = round(e + OFFSET, 2)   # NOTE: limit = retest entry, ~half the edge
    return p

def exec_orb(x, text=None, action='enter'):
    if not EXEC_ORB or requests is None: return 'no-exec'
    p = td_payload(x, action)
    if text: p['text'] = text
    try:
        r = requests.post(EXEC_ORB, json=p, timeout=10); print('EXEC_ORB', getattr(r, 'status_code', None), flush=True); return 'exec'
    except Exception as ex:
        print('EXEC_ORB err', ex, flush=True); return f'ERR {ex}'

# ---------- io ----------
def _ld(p):
    try: return json.load(open(p))
    except Exception: return {}
def _sv(p, d):
    dd = os.path.dirname(p)
    if dd: os.makedirs(dd, exist_ok=True)
    json.dump(d, open(p, 'w'))
def key_orb(x): return f"ORB|{x['date']}|{x['dir']}"        # one alert per day+direction

def poll():
    if not ENABLED:
        print('STRAT_ORB_ENABLED != 1 -> idle'); return []
    x = orb_signal(BUF)
    if not x: print('[orb_live] no signal'); return []
    if not x['fresh']:
        print(f"[orb_live] break at {x['brk_time']} not fresh (> {FRESH_MIN}m old) -> skip"); return []
    sent = _ld(SENT_ORB); k = key_orb(x)
    if k in sent:
        print('[orb_live] already alerted today'); return []
    txt = to_alert(x)
    if WEBHOOK: _post_webhook(txt, WEBHOOK)
    if EXEC_ORB: exec_orb(x, text=txt, action='enter')
    j = _ld(ORB_TRADES); j[k] = dict(x, status='alerted', alert_ts=dt.datetime.utcnow().isoformat(timespec='seconds')); _sv(ORB_TRADES, j)
    sent[k] = dt.datetime.utcnow().isoformat(timespec='seconds'); _sv(SENT_ORB, sent)
    print(f"[orb_live] ALERTED {k}"); return [x]

# ================= OUTCOME TRACKING + PERFORMANCE =================
COMM_PT = float(os.environ.get('ORB_COMM_PT', '0.75'))     # $1.5 RT / $2 per pt, in points

def update_outcomes():
    """Reconcile each logged trade against the buffer: win(TP)/loss(SL)/eod, realized net R."""
    log = _ld(ORB_TRADES)
    if not log: return log
    try: df = load_buffer(BUF)
    except Exception: return log
    last_date = df['date'].iloc[-1]
    for k, t in log.items():
        if t.get('status') == 'closed': continue
        try: d = pd.to_datetime(t['date']).date()
        except Exception: continue
        bh, bm = t['brk_time'].split(':'); bkmin = int(bh) * 60 + int(bm)
        day = df[(df.date == d) & (df.mn > bkmin)]
        if day.empty: continue
        long = t['dir'] == 'LONG'; entry = t['entry']; stop = t['SL']; tp = t['TP']; R = abs(entry - stop)
        res, rr = None, 0.0
        for _, b in day.iterrows():
            if long:
                if b['low'] <= stop: res, rr = 'loss', -1.0; break
                if b['high'] >= tp: res, rr = 'win', TARGET_R; break
            else:
                if b['high'] >= stop: res, rr = 'loss', -1.0; break
                if b['low'] <= tp: res, rr = 'win', TARGET_R; break
        eod = (int(df[df.date == d]['mn'].max()) >= 15 * 60 + 58) or (d < last_date)
        if res is not None:
            t['status'] = 'closed'; t['result'] = res; t['R'] = round(rr - COMM_PT / R, 3)
        elif eod:
            c = float(day['close'].iloc[-1]); rr = (c - entry) / R if long else (entry - c) / R
            t['status'] = 'closed'; t['result'] = 'eod'; t['R'] = round(rr - COMM_PT / R, 3)
        else:
            c = float(day['close'].iloc[-1]); t['status'] = 'open'; t['R'] = round((c - entry) / R if long else (entry - c) / R, 3)
    _sv(ORB_TRADES, log)
    return log

def perf(log):
    closed = [t for t in log.values() if t.get('status') == 'closed']
    rs = [t['R'] for t in closed]
    if not rs: return dict(n=0, win=0, exp=0, totR=0, eq=[], open=sum(1 for t in log.values() if t.get('status') == 'open'))
    wins = sum(1 for t in closed if t.get('result') == 'win')
    eq = np.cumsum(rs).round(2).tolist()
    return dict(n=len(rs), win=round(wins / len(rs) * 100, 1), exp=round(float(np.mean(rs)), 3),
                totR=round(float(np.sum(rs)), 1), eq=eq, open=sum(1 for t in log.values() if t.get('status') == 'open'))

def today_state():
    """Candidate view: today's opening range + break status (even before a trade fires)."""
    try: df = load_buffer(BUF)
    except Exception as e: return dict(err=str(e))
    if df.empty: return dict(err='empty buffer')
    today = df['date'].iloc[-1]; day = df[df.date == today]
    orw = day[(day.mn >= OPEN_MIN) & (day.mn <= ORB_END)]
    now_mod = int(day['mn'].max()); price = float(day['close'].iloc[-1])
    st = dict(date=str(today), now=f"{now_mod//60:02d}:{now_mod%60:02d}", price=price)
    if len(orw) < max(5, ORB_MIN - 3):
        st['status'] = 'forming'; st['note'] = 'opening range not complete yet'; return st
    orh = float(orw['high'].max()); orl = float(orw['low'].min())
    st.update(or_hi=round(orh, 2), or_lo=round(orl, 2), or_pts=round(orh - orl, 1))
    sig = orb_signal(BUF)
    if sig: st['status'] = 'SIGNAL'; st['sig'] = sig
    elif price > orh or price < orl: st['status'] = 'broke-but-filtered'; st['note'] = 'break occurred but a filter (time/bias/gap) blocked it'
    else: st['status'] = 'watching'; st['note'] = 'inside range, waiting for a break'
    return st

# ================= DASHBOARD (Flask app; gunicorn strategy_orb.orb_live:app) =================
try:
    from flask import Flask, jsonify, Response
    app = Flask(__name__)
except Exception:
    app = None

def _svg_equity(eq):
    if not eq: return '<p style="color:#888">No closed trades yet.</p>'
    w, h, pad = 620, 160, 24
    lo, hi = min(eq + [0]), max(eq + [0]); rng = (hi - lo) or 1
    pts = []
    for i, v in enumerate(eq):
        x = pad + (w - 2 * pad) * (i / max(1, len(eq) - 1))
        y = h - pad - (h - 2 * pad) * ((v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    zero_y = h - pad - (h - 2 * pad) * ((0 - lo) / rng)
    col = '#26a69a' if eq[-1] >= 0 else '#ef5350'
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:640px">'
            f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" stroke="#ccc" stroke-dasharray="3"/>'
            f'<polyline fill="none" stroke="{col}" stroke-width="2" points="{" ".join(pts)}"/></svg>')

def render_dashboard():
    log = update_outcomes(); p = perf(log); ts = today_state()
    rows = sorted(log.values(), key=lambda t: (t.get('date', ''), t.get('brk_time', '')), reverse=True)
    def badge(t):
        s = t.get('result') or t.get('status', '')
        c = {'win': '#1b7a5a', 'loss': '#c62828', 'eod': '#8a6d00', 'open': '#1565c0'}.get(s, '#666')
        return f'<span style="background:{c};color:#fff;padding:1px 7px;border-radius:9px;font-size:12px">{s}</span>'
    logrows = "".join(
        f"<tr><td>{t.get('date','')}</td><td>{t.get('brk_time','')}</td><td>{t.get('dir','')}</td>"
        f"<td>{t.get('entry','')}</td><td>{t.get('SL','')}</td><td>{t.get('TP','')}</td>"
        f"<td>{t.get('risk','')}</td><td>{t.get('bias_align','')}</td>"
        f"<td>{('%+.2f'%t['R']) if t.get('R') is not None else '—'}</td><td>{badge(t)}</td></tr>"
        for t in rows[:200]) or '<tr><td colspan=10 style="color:#888">No trades logged yet.</td></tr>'
    # candidate panel
    if ts.get('status') == 'SIGNAL':
        s = ts['sig']; cand = f"<b style='color:#1565c0'>🅾 SIGNAL {s['dir']}</b> — entry {s['entry']} · SL {s['SL']} · TP {s['TP']} · break {s['brk_time']} · bias {s['bias_align']}"
    elif ts.get('status') in ('watching', 'broke-but-filtered', 'forming'):
        cand = f"<b>{ts.get('status','?')}</b> — {ts.get('note','')}. " + (f"OR {ts.get('or_lo','?')}–{ts.get('or_hi','?')} ({ts.get('or_pts','?')} pt), price {ts.get('price','?')}" if 'or_hi' in ts else "")
    else:
        cand = f"<span style='color:#888'>{ts.get('err','no data')}</span>"
    enabled = 'ON' if ENABLED else 'OFF (idle / dry-run)'
    html = f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<title>Strategy ORB</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#222;background:#fafafa}}
h1{{font-size:20px}} .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}}
.card{{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:12px 16px;min-width:120px}}
.card b{{font-size:22px}} table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}} th{{background:#f4f4f4}}
.small{{color:#888;font-size:12px}}</style></head><body>
<h1>🅾 Strategy ORB — Trend-Day Opening Breakout <span class=small>({enabled})</span></h1>
<div class=small>Momentum breakout (NOT ICT). Separate stream from A/B · C · F. Backtest ref (4yr, net): bias-aligned +0.132R, ~121 trades/yr, ~19% to 2R, +0.10 corr w/ A/B.</div>

<h3>① Candidate — today ({ts.get('date','?')} · {ts.get('now','')})</h3>
<div class=card style="min-width:100%">{cand}</div>

<h3>② Performance — realized (live log)</h3>
<div class=cards>
<div class=card>trades<br><b>{p['n']}</b></div>
<div class=card>win→2R<br><b>{p['win']}%</b></div>
<div class=card>expectancy<br><b>{p['exp']:+}</b> R</div>
<div class=card>total<br><b>{p['totR']:+}</b> R</div>
<div class=card>open now<br><b>{p['open']}</b></div>
</div>
{_svg_equity(p['eq'])}

<h3>③ Trade log</h3>
<table><tr><th>date</th><th>break</th><th>dir</th><th>entry</th><th>SL</th><th>TP</th><th>risk</th><th>bias</th><th>R</th><th>status</th></tr>
{logrows}</table>
<p class=small>Auto-refreshes every 60s. Log file: {ORB_TRADES}. Performance is realized R net of cost; open trades excluded from stats.</p>
</body></html>"""
    return html

if app is not None:
    @app.route('/')
    def _home(): return Response(render_dashboard(), mimetype='text/html')
    @app.route('/health')
    def _health(): return jsonify(ok=True, enabled=ENABLED, buffer=BUF)
    @app.route('/api/state')
    def _state(): return jsonify(today=today_state(), performance=perf(update_outcomes()))
    @app.route('/poll')
    def _pollroute(): return jsonify(fired=poll())
    def _bg_loop():
        while True:
            try: poll(); update_outcomes()
            except Exception as ex: print('bg err', ex, flush=True)
            time.sleep(int(os.environ.get('STRAT_ORB_POLL_SEC', '60')))
    if os.environ.get('ORB_BG', '1') == '1':
        import threading; threading.Thread(target=_bg_loop, daemon=True).start()

if __name__ == '__main__':
    if '--serve' in sys.argv and app is not None:
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
    elif '--loop' in sys.argv:
        while True:
            try: poll(); update_outcomes()
            except Exception as ex: print('poll err', ex, flush=True)
            time.sleep(int(os.environ.get('STRAT_ORB_POLL_SEC', '60')))
    else:
        # one-shot; if disabled, still PRINT what it WOULD alert (dry test)
        if ENABLED:
            poll()
        else:
            x = orb_signal(BUF)
            if x: print('\n' + to_alert(x))
            else: print('no signal on current buffer (dry-run)')
