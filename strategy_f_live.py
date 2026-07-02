#!/usr/bin/env python3
"""
strategy_f_live.py — LIVE signal generator for Strategy F (F.P. PFVG first-touch, Continuation).

SAFE BY DESIGN — does NOT touch the live A/B agent:
  * Runs as its OWN process. The emit-hook it installs is process-local, so it can never affect the
    agent's detection (which runs in a different process).
  * Imports detcore + strategy_f READ-ONLY; reuses live_emit.size_for / post_webhook only.
  * Own dedup file (SENT_F_FILE), own Telegram webhook, own TradersPost strategy (EXEC_WEBHOOK_F).
  * With STRAT_F_ENABLED unset it does nothing.

WHAT IT DOES each poll (run on a 1-min cron, or --loop):
  1. Read the agent's bar buffer (STRAT_F_BUF / BUF), run the slim F.P.FVG detector.
  2. Take the FIRST displacement of the day off the F.P.FVG level; keep it only if it is CONTINUATION.
  3. Build the order: LIMIT at the near edge of the F.P. PFVG, SL just past the gap, TP = 2R.
  4. INVALIDATION: if a 1-min candle BODY closes through the gap (long: close<gap-low / short:
     close>gap-high) -> the gap is broken -> CANCEL (no entry / flatten). Verified +EV: removes the
     ~breakeven-or-loss fills, lifts the cut to ~42% win / +0.572R in backtest.
  5. Alert (distinct wording) + optionally stage a TradersPost bracket using the SAME payload schema as
     agent._exec_order, so it works with the TradersPost you already have.

ENV (all optional; nothing fires unless STRAT_F_ENABLED=1 and a webhook is set):
  STRAT_F_ENABLED=1
  STRAT_F_BUF or BUF        path to the agent's bar buffer CSV (read-only)
  STRAT_F_WEBHOOK or WEBHOOK_URL    Telegram /webhook for F alerts
  EXEC_WEBHOOK_F           TradersPost relay for F (a SEPARATE strategy is recommended; set it to your
                          current EXEC_WEBHOOK if you deliberately want F in the same TradersPost strategy)
  EXEC_TICKER_F (def EXEC_TICKER/CONTRACT/'MNQ1!') · EXEC_MAX_QTY_F · PRICE_OFFSET
  SENT_F_FILE (def /home/claude/sent_signals_F.json) · STRAT_F_FRESH_MIN (def 30)
  STRAT_F_AUTO_CANCEL=1    also POST a TradersPost 'exit' on body-break (else: Telegram cancel notice only)
"""
import os, sys, json, time, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT); sys.path.insert(0, HERE)
import strategy_f as S
import detcore.emit as emit
import live_emit                      # reuse size_for / post_webhook (read-only)
try: import requests
except Exception: requests = None

BUF        = os.environ.get('STRAT_F_BUF') or os.environ.get('BUF') or os.path.join(PARENT, 'buffer.csv')
WEBHOOK_F  = os.environ.get('STRAT_F_WEBHOOK') or os.environ.get('WEBHOOK_URL', '')
EXEC_F     = os.environ.get('EXEC_WEBHOOK_F', '')
SENT_F     = os.environ.get('SENT_F_FILE', '/home/claude/sent_signals_F.json')
F_TRADES   = os.environ.get('F_TRADES_FILE') or os.path.join(os.path.dirname(SENT_F) or '.', 'f_trades.json')
FRESH_MIN  = int(os.environ.get('STRAT_F_FRESH_MIN', '30'))
FILL_MIN   = int(os.environ.get('STRAT_F_FILL_MIN', '30'))    # limit ważny tylko N min po displacemencie (było 240) — usuwa spóźnione wejścia
OFFSET     = float(os.environ.get('PRICE_OFFSET', '0'))
ONLY_CONT  = os.environ.get('STRAT_F_ONLY_CONT', '1') == '1'

# ---- process-local capture hook (records model + geometry) ----
_cap = []; _real = emit.emit
def _hook(ctx, t, model, name, dr, disp, conf=None):
    if name.startswith('F.P.FVG'):
        _cap.append(dict(dir=dr, model=model, fl=float(disp['fvg'][0]), fh=float(disp['fvg'][1]),
                         swlo=float(disp['swlo']), swhi=float(disp['swhi']), s=int(disp['s']),
                         u=int(disp['u']), fvg_bar=int(disp['fvg_bar'])))
    return _real(ctx, t, model, name, dr, disp, conf)
emit.emit = _hook


def key_f(x): return f"F|{x['date']}|{x['dir']}|{x['fvg_bar']}"   # own namespace -> never merges with A/B


def _signal_status(r, hi, lo, cl, n):
    """State of the resting limit as of the latest bar, scanning ONLY up to the first touch (a fill ends
    the 'no-entry' window — after that the trade is managed by the SL, not cancelled):
      'invalid' = a candle BODY closes through the gap before/at the fill -> CANCEL, no entry
      'filled'  = the near edge was touched (limit would fill) and the gap was NOT bodied through
      'live'    = within the fill window, not yet touched, gap intact -> keep the resting limit
      'expired' = fill window elapsed with no touch."""
    bull = r['dir'] == 'LONG'; fl, fh = r['fl'], r['fh']
    entry = fh if bull else fl; far = fl if bull else fh
    end = min(r['u'] + 1 + FILL_MIN, n)                 # okno wypełnienia = FILL_MIN min po displacemencie
    for i in range(r['u'] + 1, end):
        touched = lo[i] <= entry <= hi[i]
        broke = (cl[i] < far) if bull else (cl[i] > far)
        if touched:
            return 'invalid' if broke else 'filled'    # entry candle closes through -> cancel; else filled
        if broke:
            return 'invalid'                            # bodied through before any touch -> cancel
    return 'live' if (r['u'] + 1 + FILL_MIN) > n else 'expired'   # okno minęło bez dotknięcia -> expired


def f_signals(buf=BUF):
    """Today/most-recent first-presentation CONTINUATION F.P.FVG signals with live status."""
    _cap.clear()
    ded, ctx = S.detect_fpfvg(S.Config(disp_mode='chain', dispwin=30, minimp=3, cutoff='', data_csv=buf))
    hi, lo, cl, n, dates, dtc = ctx.hi, ctx.lo, ctx.cl, ctx.n, ctx.dates, ctx.df.dt
    byday = {}
    for r in sorted(S.dedup(list(_cap)), key=lambda z: z['u']):
        d = str(dates[r['u']])
        if d not in byday: byday[d] = r       # First Presentation = earliest displacement of the day
    out = []
    for d, r in byday.items():
        if ONLY_CONT and r['model'] != 'Cont':
            continue
        bull = r['dir'] == 'LONG'
        entry = S.entry_price(r, 'edge'); sl = S.stop_price(r, 'fvg', hi, lo)
        risk = (entry - sl) if bull else (sl - entry)
        if not (0 < risk <= S.MAXR):
            continue
        tp = round(entry + S.RR * risk if bull else entry - S.RR * risk, 2)
        status = _signal_status(r, hi, lo, cl, n)   # live / filled / invalid / expired
        out.append(dict(date=d, dir=r['dir'], model='Cont', entry=entry, SL=sl, TP=tp, risk=round(risk, 2),
                        fvg_bar=r['fvg_bar'], fvg_lo=round(r['fl'], 2), fvg_hi=round(r['fh'], 2),
                        disp_end=dtc.iloc[r['u']].strftime('%H:%M'),
                        disp_end_ms=int(dtc.iloc[r['u']].timestamp() * 1000),
                        status=status, invalidated=(status == 'invalid'), strategy='F'))
    return out


def to_alert_f(x):
    isL = x['dir'] == 'LONG'; side = 'BUY' if isL else 'SELL'; emoji = '🟢' if isL else '🔴'
    rp = round(x['risk'], 1)
    base = (f"⚡ STRATEGY F · F.P. PFVG first-touch (BEZ BOS) · {emoji} {x['dir']} | Cont · Kat: F.P.FVG"
            f"\n📋 {side} LIMIT {round(x['entry']+OFFSET,1)} — POSTAW TERAZ, fill na PIERWSZYM dotknięciu luki"
            f"\n🛑 SL {round(x['SL']+OFFSET,1)} · ryzyko {rp} pkt · BE po +{rp} (1R)"
            f"\n🎯 TP {round(x['TP']+OFFSET,1)} · +{round(2*x['risk'],1)} pkt (2R)"
            f"\n🧩 F.P. PFVG {x['fvg_lo']}–{x['fvg_hi']} (displacement {x['disp_end']})"
            f"\n⛔ UNIEWAŻNIENIE: jeśli świeca ZAMKNIE się CIAŁEM {'pod' if isL else 'nad'} "
            f"{round((x['fvg_lo'] if isL else x['fvg_hi'])+OFFSET,1)} → ANULUJ limit (brak wejścia)")
    s = live_emit.size_for(x['entry'], x['SL'])
    if s:
        qty, slpts, perc, real, pct = s
        base += f"\n📐 {qty} kontr. (SL {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)"
    base += "\n⚠ Strategy F — OSOBNY strumień, NIE myl z alertami A/B (BOS). Tylko Continuation."
    return base


def _td_payload(x, action):
    """Mirror agent._exec_order schema EXACTLY so it works with the current TradersPost relay."""
    isL = x['dir'] == 'LONG'; e = float(x['entry']); sl = float(x['SL']); R = abs(e - sl)
    tp = (e + 2*R) if isL else (e - 2*R)
    _sf = live_emit.size_for(e, sl); qty = int(_sf[0]) if _sf else 1
    cap = os.environ.get('EXEC_MAX_QTY_F', '').strip()
    if cap.isdigit() and int(cap) > 0: qty = min(qty, int(cap))
    qty = max(1, qty)
    return {
        "ticker": os.environ.get('EXEC_TICKER_F', os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))),
        "action": ("buy" if isL else "sell") if action == 'enter' else "exit",
        "orderType": "limit", "limitPrice": round(e + OFFSET, 2), "quantity": qty,
        "takeProfit": {"limitPrice": round(tp + OFFSET, 2)},
        "stopLoss": {"type": "stop", "stopPrice": round(sl + OFFSET, 2)},
        "timeInForce": "gtc", "strategy": "STRATEGY_F",
    }


def exec_f(x, text=None, action='enter'):
    if not EXEC_F or requests is None:
        return 'no-exec'
    p = _td_payload(x, action)
    if text: p['text'] = text
    try:
        r = requests.post(EXEC_F, json=p, timeout=10); print('EXEC_F', getattr(r, 'status_code', None), p, flush=True)
        return 'exec'
    except Exception as ex:
        print('EXEC_F err', ex, flush=True); return f'ERR {ex}'


def _ensure_dir(p):
    d = os.path.dirname(p)
    if d: os.makedirs(d, exist_ok=True)

def _load(p):
    try: return json.load(open(p))
    except Exception: return {}
def _save(p, d):
    _ensure_dir(p); json.dump(d, open(p, 'w'))


# ================= LIVE F JOURNAL (own file on the volume; separate from A/B) =================
def _journal_add(x):
    """Record a fresh F alert so you can SEE it live + track its outcome."""
    j = _load(F_TRADES); k = key_f(x)
    if k not in j:
        j[k] = dict(date=x['date'], dir=x['dir'], entry=x['entry'], SL=x['SL'], TP=x['TP'], risk=x['risk'],
                    fvg_lo=x['fvg_lo'], fvg_hi=x['fvg_hi'], be=False, status='alerted',
                    disp_end_ms=x.get('disp_end_ms', 0),
                    alert_ts=dt.datetime.utcnow().isoformat(timespec='seconds'))
        _save(F_TRADES, j)


def _journal_update(b):
    """Per-bar state machine for the journal: resting limit -> filled -> win/loss/be (BE@1R, TP=2R,
    intrabar SL-first); a body close through the gap before fill -> cancelled. Outcome modeled from
    the live bars (same rules as the backtest)."""
    try:
        hi = float(b['high']); lo = float(b['low']); cl = float(b['close'])
    except Exception:
        return
    ts = str(b.get('ts_event', '')); j = _load(F_TRADES); changed = False
    try:
        _t = ts if ('+' in ts or 'Z' in ts) else ts + '+00:00'
        bar_ms = int(dt.datetime.fromisoformat(_t.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        bar_ms = 0
    for k, t in j.items():
        if t['status'] in ('win', 'loss', 'be', 'cancelled', 'expired'):
            continue
        bull = t['dir'] == 'LONG'; e = t['entry']; sl = t['SL']; tp = t['TP']; risk = t['risk']
        far = t['fvg_lo'] if bull else t['fvg_hi']
        if t['status'] == 'alerted':                      # limit resting
            if bar_ms and t.get('disp_end_ms') and (bar_ms - t['disp_end_ms']) > FILL_MIN * 60 * 1000:
                t['status'] = 'expired'; t['close_ts'] = ts; changed = True; continue   # okno minęło -> anuluj limit
            broke = (cl < far) if bull else (cl > far)
            if broke:
                t['status'] = 'cancelled'; t['close_ts'] = ts; changed = True; continue
            if lo <= e <= hi:
                t['status'] = 'filled'; t['be'] = False; t['fill_ts'] = ts; changed = True
        elif t['status'] == 'filled':                     # manage BE@1R / TP2R / SL-first
            be = t.get('be', False); cur = e if be else sl; oneR = e + risk if bull else e - risk
            hit_sl = (lo <= cur) if bull else (hi >= cur); hit_tp = (hi >= tp) if bull else (lo <= tp)
            if hit_sl:
                t['status'] = 'be' if be else 'loss'; t['R'] = 0.0 if be else -1.0; t['close_ts'] = ts; changed = True; continue
            if hit_tp:
                t['status'] = 'win'; t['R'] = 2.0; t['close_ts'] = ts; changed = True; continue
            if (not be) and ((hi >= oneR) if bull else (lo <= oneR)):
                t['be'] = True; changed = True
    if changed:
        _save(F_TRADES, j)


def _journal_stats():
    from collections import Counter
    vals = list(_load(F_TRADES).values())
    closed = [t for t in vals if t['status'] in ('win', 'loss', 'be')]
    wins = sum(1 for t in closed if t['status'] == 'win'); n = len(closed)
    totR = round(sum(t.get('R', 0.0) for t in closed), 2)
    riskusd = float(os.environ.get('ACCOUNT', '100000')) * float(os.environ.get('RISK_PCT', '0.5')) / 100.0
    return dict(alerts=len(vals), filled=sum(1 for t in vals if t['status'] in ('filled', 'win', 'loss', 'be')),
                closed=n, wins=wins, winpct=round(100 * wins / n, 1) if n else 0.0,
                totR=totR, dollars=round(totR * riskusd), by_status=dict(Counter(t['status'] for t in vals)),
                trades=sorted(vals, key=lambda z: z.get('alert_ts', ''), reverse=True)[:60])


def process_f(buf=BUF, now_ms=None):
    if os.environ.get('STRAT_F_ENABLED') != '1':
        return {'disabled': True}
    now_ms = now_ms or int(time.time() * 1000)
    sigs = f_signals(buf); state = _load(SENT_F); fired = cancelled = 0
    fresh_ms = FRESH_MIN * 60 * 1000
    for x in sigs:
        k = key_f(x); st = state.get(k); status = x['status']
        if st is None:                                  # first time we see this setup
            if status == 'live' and (now_ms - x['disp_end_ms']) <= fresh_ms:
                txt = to_alert_f(x)
                code = exec_f(x, txt, 'enter') if EXEC_F else (live_emit.post_webhook(txt, WEBHOOK_F) if WEBHOOK_F else 'no-url')
                if EXEC_F and WEBHOOK_F: live_emit.post_webhook(txt, WEBHOOK_F)   # also notify
                print('F-ALERT', code, k, flush=True); state[k] = 'alerted'; fired += 1; _journal_add(x)
            else:
                state[k] = status      # filled/invalid/expired/stale on first sight -> never alert
        elif st == 'alerted':                           # limit was resting; react to a state change
            if status == 'invalid':                     # gap bodied through before fill -> CANCEL
                ctxt = f"⛔ STRATEGY F — ANULUJ: F.P. PFVG złamane ciałem świecy ({x['date']} {x['dir']}). Brak wejścia."
                if WEBHOOK_F: live_emit.post_webhook(ctxt, WEBHOOK_F)
                if EXEC_F and os.environ.get('STRAT_F_AUTO_CANCEL') == '1': exec_f(x, ctxt, 'exit')
                print('F-CANCEL', k, flush=True); state[k] = 'cancelled'; cancelled += 1
            elif status in ('filled', 'expired'):        # trade is on (broker manages) / window closed -> stop tracking
                state[k] = status
    _save(SENT_F, state)
    return {'f_alerts': fired, 'f_cancels': cancelled, 'f_signals': len(sigs)}


# ================= optional STANDALONE SERVICE (deploy as its OWN Railway service; never touches A/B) =================
# The A/B agent is a different process/service and is NOT imported or modified here. Point your bar feed
# (TradingView/databento webhook) at BOTH the agent's /bars and this service's /bars. F keeps its own
# buffer, its own Telegram webhook, and its own TradersPost strategy (EXEC_WEBHOOK_F).
import csv
F_BUF         = os.environ.get('STRAT_F_BUF', '/home/claude/buffer_F.csv')
F_BUFFER_BARS = int(os.environ.get('STRAT_F_BUFFER_BARS', '6000'))
F_COLS        = ['ts_event', 'open', 'high', 'low', 'close', 'volume']
_state = {'version': 'F-v1', 'last_alert': None, 'alerts': 0, 'cancels': 0, 'bars': 0}

def _append_bar_f(b):
    _ensure_dir(F_BUF)
    ts = str(b['ts_event']).strip()
    if '+' not in ts and 'Z' not in ts: ts = ts + '+00:00'
    row = [ts, b['open'], b['high'], b['low'], b['close'], b.get('volume', 0)]
    new = not os.path.exists(F_BUF)
    with open(F_BUF, 'a', newline='') as f:
        w = csv.writer(f)
        if new: w.writerow(F_COLS)
        w.writerow(row)
    with open(F_BUF) as f: rows = f.readlines()
    if len(rows) > F_BUFFER_BARS + 1:
        with open(F_BUF, 'w') as f: f.write(rows[0] + ''.join(rows[-F_BUFFER_BARS:]))

# ───────── /how page (inline, ORB-style — no separate file) ─────────
def _example_svg_f():
    return (
        '<svg viewBox="0 0 760 400" width="100%" style="max-width:760px;background:#fff;border:1px solid #eee;border-radius:8px">'
        '<text x="86" y="392" font-size="10" fill="#888">09:30</text>'
        '<text x="300" y="392" font-size="10" fill="#888">NY-AM</text>'
        '<text x="686" y="392" font-size="10" fill="#888">11:59</text>'
        '<rect x="150" y="70" width="540" height="30" fill="#9e9e9e" fill-opacity="0.16" stroke="#bdbdbd" stroke-dasharray="3"/>'
        '<line x1="150" y1="100" x2="690" y2="100" stroke="#9e9e9e" stroke-dasharray="4" stroke-width="1.3"/>'
        '<text x="152" y="63" font-size="11" fill="#616161" font-weight="bold">1 &#183; F.P.FVG catalyst (first NY-AM gap) &rarr; the level</text>'
        '<rect x="250" y="158" width="440" height="26" fill="#42a5f5" fill-opacity="0.28"/>'
        '<text x="408" y="173" font-size="11" fill="#0d47a1" font-weight="bold">4 &#183; the gap it leaves = the FVG we trade</text>'
        '<line x1="60" y1="150" x2="700" y2="150" stroke="#c62828" stroke-dasharray="4" stroke-width="1.4"/>'
        '<text x="498" y="144" font-size="11" fill="#c62828" font-weight="bold">6 &#183; STOP = just past the gap (1R, ~14pt)</text>'
        '<line x1="60" y1="184" x2="700" y2="184" stroke="#2e7d32" stroke-dasharray="4" stroke-width="1.4"/>'
        '<text x="498" y="197" font-size="11" fill="#2e7d32" font-weight="bold">5 &#183; ENTRY = near edge, first touch back</text>'
        '<line x1="60" y1="300" x2="700" y2="300" stroke="#1565c0" stroke-width="1.5"/>'
        '<text x="540" y="316" font-size="11" fill="#1565c0" font-weight="bold">7 &#183; TARGET = 2R (BE at +1R)</text>'
        '<polyline fill="none" stroke="#333" stroke-width="1.6" points="90,96 108,104 126,94 144,106 162,98 180,108 198,100 210,104 '
        '220,120 230,150 240,182 250,210 260,236 270,248 284,230 298,208 310,190 318,184 332,204 348,232 366,258 386,282 406,298 '
        '440,300 490,299 560,301 640,299 690,300"/>'
        '<circle cx="214" cy="104" r="4.5" fill="#8e24aa"/>'
        '<text x="70" y="128" font-size="11" fill="#8e24aa" font-weight="bold">2 &#183; a candle CLOSES through the level &rarr; continuation</text>'
        '<text x="118" y="250" font-size="11" fill="#e67e22" font-weight="bold">3 &#183; displacement (strong, breaks structure)</text>'
        '<circle cx="318" cy="184" r="5.5" fill="#fff" stroke="#2e7d32" stroke-width="2.4"/>'
        '<text x="326" y="176" font-size="10.5" fill="#2e7d32" font-weight="bold">FILL</text>'
        '<text x="404" y="292" font-size="22" fill="#1565c0">&#9733;</text>'
        '<text x="360" y="336" font-size="11" fill="#1565c0" font-weight="bold">price runs on &rarr; +2R &#183; WIN</text>'
        '<text x="60" y="368" font-size="10.5" fill="#555">1R = 0.5% of the account &rarr; wide gap = fewer contracts (gaps &gt; 40pt skipped).</text>'
        '</svg>')


def render_how_f():
    return (
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Strategy F - how it works</title><style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:26px;color:#222;background:#fafafa;max-width:820px}"
        "h1{font-size:21px} h3{margin-top:22px} a{color:#1565c0} .small{color:#888;font-size:13px}"
        "ol{line-height:1.7} .warn{background:#fff8e1;border:1px solid #f0d98a;border-radius:8px;padding:10px 14px;font-size:13px;margin-top:16px}"
        "</style></head><body>"
        "<p><a href='/candidates'>&larr; candidates</a> &nbsp;&#183;&nbsp; <a href='/log'>trade log</a></p>"
        "<h1>&#129518; Strategy F - NY-AM displacement-FVG continuation</h1>"
        "<div class=small>Waits for the first strong NY-AM move to leave a fair-value gap, then enters the <b>first "
        "pullback</b> into that gap, with the move, for a 2R target. It uses the ICT engine (FVG + displacement + "
        "structure break) <b>but the tested edge is momentum continuation - not the ICT narrative</b>: bias, "
        "liquidity-pool targets and sweep-catalyst confluence were all tested and do not help. Same family as A/B/C - "
        "<b>correlated, not a diversifier</b>.</div>"
        "<h3>Example trade (real, out-of-sample &#183; 2026-06-18 SHORT)</h3>"
        + _example_svg_f() +
        "<h3>The rules, step by step</h3><ol>"
        "<li><b>Mark the catalyst.</b> The <b>first fair-value gap of NY-AM</b> (09:30-11:59 ET) - a 3-candle gap. Its edges are the level.</li>"
        "<li><b>Wait for a continuation break.</b> A 1-min candle must <b>CLOSE through</b> the level (a close, not a wick).</li>"
        "<li><b>Require a real displacement:</b> &ge;3 same-colour candles that <b>break the prior 15-bar structure</b>, "
        "body &ge; <b>1.5&times; the 5m-ATR</b> and bigger than any recent candle - and it must <b>leave its own gap</b> (the one you trade).</li>"
        "<li><b>Order:</b> limit at the <b>near edge</b> of that gap - stop <b>just past the far edge</b> (~14pt = 1R) - target <b>2R</b> - break-even at +1R.</li>"
        "<li><b>One trade/day:</b> the <b>first continuation</b> of the session. The reversal direction is dropped (it doesn't pay).</li>"
        "<li><b>Fill within ~30 min or cancel.</b> Size is automatic (1R = 0.5%); gaps wider than 40pt are skipped.</li></ol>"
        "<h3>What to expect (backtest, 4 years, honest cut)</h3>"
        "<div class=small>~160 trades/yr (~0.9/session) &#183; win ~37% (2R payoff - judge on expectancy) &#183; expectancy "
        "<b>+0.42R</b> at a realistic 1-tick fill (band +0.33 to +0.48R) &#183; ~+$132k on $100k @ 0.5% over 4 years &#183; "
        "<b>positive every year</b> (+0.38 / +0.50 / +0.40 / +0.37R) &#183; correlation with A/B high (same family).</div>"
        "<div class=warn><b>In-sample.</b> 2022-06&rarr;2026-06. One out-of-sample check (14 June-2026 sessions) did not "
        "break but only <b>4 trades filled</b> - proves nothing. The only real fragility is <b>fill quality</b> on the "
        "~14pt stop; by design it <b>misses no-retrace runner days</b>. <b>Gate 0: prove &ge; +0.15R over 30-50 live "
        "trades before sizing up.</b> Not financial advice.</div>"
        "<p class=small><a href='/candidates'>&larr; back to candidates</a></p></body></html>")


try:
    from flask import Flask, request, jsonify
    app = Flask(__name__)


    @app.route('/health')
    def _health(): return jsonify(ok=True, version=_state['version'], enabled=os.environ.get('STRAT_F_ENABLED') == '1')

    @app.route('/status')
    def _status(): return jsonify(strategy='F (F.P. PFVG first-touch, Continuation)', buf=F_BUF,
                                  enabled=os.environ.get('STRAT_F_ENABLED') == '1', webhook_set=bool(WEBHOOK_F),
                                  exec_F_set=bool(EXEC_F), **_state)

    @app.route('/bars', methods=['POST'])
    def _bars():
        b = request.get_json(force=True, silent=True) or {}
        if 'close' not in b: return jsonify(error='no OHLC'), 400
        _append_bar_f(b); _state['bars'] += 1
        try:
            r = process_f(F_BUF, int(time.time() * 1000))
            _journal_update(b)                     # update live F journal (fill / TP / SL / R) from this bar
            _state['alerts'] += r.get('f_alerts', 0); _state['cancels'] += r.get('f_cancels', 0)
            if r.get('f_alerts'): _state['last_alert'] = dt.datetime.utcnow().isoformat(timespec='seconds')
            return jsonify(ok=True, **r)
        except Exception as e:
            print('F /bars err', e, flush=True); return jsonify(ok=False, error=str(e)), 200

    _SAMPLE = dict(date='TEST', dir='LONG', entry=25000.0, SL=24985.0, TP=25030.0, risk=15.0,
                   fvg_lo=24985.0, fvg_hi=25000.0, disp_end='10:00')

    @app.route('/testalert')
    def _testalert():                      # send a SAMPLE F alert to Telegram only (no TradersPost) -> verify wiring
        sec = os.environ.get('EXEC_TEST_SECRET', '')
        if not sec or request.args.get('secret') != sec:
            return jsonify(error='set EXEC_TEST_SECRET (env) and pass ?secret=...'), 401
        txt = '🧪 TEST ALERT (ignore) ·\n' + to_alert_f(_SAMPLE)
        code = live_emit.post_webhook(txt, WEBHOOK_F) if WEBHOOK_F else 'no-webhook-set'
        return jsonify(ok=True, telegram_webhook_set=bool(WEBHOOK_F), result=str(code))

    @app.route('/exectest_f')
    def _exectest_f():                     # dry-fire a sample F bracket to EXEC_WEBHOOK_F (TradersPost) + Telegram
        sec = os.environ.get('EXEC_TEST_SECRET', '')
        if not sec or request.args.get('secret') != sec:
            return jsonify(error='set EXEC_TEST_SECRET (env) and pass ?secret=...'), 401
        return jsonify(ok=True, exec_webhook_F_set=bool(EXEC_F), result=exec_f(_SAMPLE, to_alert_f(_SAMPLE), 'enter'))

    @app.route('/performance_f')
    def _perf_f():                         # live F results (JSON): win rate, R, $ — separate from A/B
        return jsonify(strategy='F (F.P. PFVG Continuation)', **_journal_stats())

    @app.route('/log')
    def _log():                            # live F journal as an HTML table -> just open it in a browser
        s = _journal_stats(); rows = ''
        for t in s['trades']:
            col = {'win': '#1b9e3a', 'loss': '#d33', 'be': '#888', 'cancelled': '#b80', 'filled': '#1565c0'}.get(t['status'], '#555')
            rows += (f"<tr><td>{t.get('alert_ts','')[:16]}</td><td>{t['dir']}</td><td>{t['entry']}</td>"
                     f"<td>{t['SL']}</td><td>{t['TP']}</td><td style='color:{col};font-weight:600'>{t['status']}</td>"
                     f"<td>{t.get('R','')}</td></tr>")
        return ("<html><head><meta charset=utf-8><style>"
                "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;color:#222;padding:24px}"
                "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
                "th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}th{background:#f4f4f4}"
                "a{color:#1565c0}h2{font-size:18px}.small{color:#888;font-size:12px}</style></head><body>"
                "<p style='font-size:13px'><a href='/candidates'>candidates</a> &middot; <a href='/log'>trade log</a> &middot; <a href='/how'>&#128214; how it works</a></p>"
                "<h2>Strategy F — live (Continuation only)</h2>"
                f"<p class=small>alerts <b>{s['alerts']}</b> · filled <b>{s['filled']}</b> · closed <b>{s['closed']}</b> · "
                f"win <b>{s['winpct']}%</b> · totR <b>{s['totR']}</b> · ~<b>${s['dollars']}</b> &nbsp;|&nbsp; {s['by_status']}</p>"
                "<table>"
                "<tr><th>alert (UTC)</th><th>dir</th><th>entry</th><th>SL</th><th>TP</th><th>status</th><th>R</th></tr>"
                f"{rows}</table><p class=small>Wynik modelowany z barów F (BE@1R/TP2R/SL-first). "
                "Porównaj z realnymi fillami TradersPost.</p></body></html>")

    @app.route('/candidates')
    def _candidates():                     # wykryte setupy F.P. PFVG (Continuation) + status -> czy F znajduje trady
        try:
            sigs = f_signals(F_BUF)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        leg = {'live': '#1565c0', 'filled': '#1b9e3a', 'invalid': '#b8860b', 'expired': '#888'}
        rows = ''
        for x in sorted(sigs, key=lambda z: (z['date'], z['disp_end']), reverse=True)[:80]:
            col = leg.get(x['status'], '#555')
            rows += (f"<tr><td>{x['date']} {x['disp_end']}</td><td>{x['dir']}</td><td>{x['entry']}</td>"
                     f"<td>{x['SL']}</td><td>{x['TP']}</td><td>{x['fvg_lo']}–{x['fvg_hi']}</td>"
                     f"<td style='color:{col};font-weight:600'>{x['status']}</td></tr>")
        return ("<html><head><meta charset=utf-8><style>"
                "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;color:#222;padding:24px}"
                "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
                "th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}th{background:#f4f4f4}"
                "a{color:#1565c0}h2{font-size:18px}.small{color:#888;font-size:12px}</style></head><body>"
                "<p style='font-size:13px'><a href='/candidates'>candidates</a> &middot; <a href='/log'>trade log</a> &middot; <a href='/how'>&#128214; how it works</a></p>"
                "<h2>Strategy F — kandydaci (wykryte setupy F.P. PFVG · Continuation)</h2>"
                f"<p class=small>{len(sigs)} w buforze &nbsp;|&nbsp; <b style='color:#1565c0'>live</b> = limit czeka na dotknięcie · "
                "<b style='color:#1b9e3a'>filled</b> = cena dotknęła · <b style='color:#b8860b'>invalid</b> = ciało przebiło lukę (brak wejścia) · "
                "<b style='color:#888'>expired</b> = okno minęło</p>"
                "<table>"
                "<tr><th>setup (NY-AM)</th><th>dir</th><th>entry</th><th>SL</th><th>TP</th><th>F.P. PFVG</th><th>status</th></tr>"
                f"{rows}</table><p class=small>Kandydat = pierwszy displacement Continuation danego dnia. "
                "Alert/zlecenie idzie tylko dla świeżych ze statusem 'live'.</p></body></html>")

    @app.route('/how')
    def _how(): return render_how_f()
except Exception as _flask_err:
    app = None


if __name__ == '__main__':
    if '--serve' in sys.argv:
        if app is None: print('flask not installed'); sys.exit(1)
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8090')))
    elif '--dry' in sys.argv:
        for x in f_signals(): print(json.dumps(x)); print(to_alert_f(x), '\n')
    elif '--loop' in sys.argv:
        while True:
            try: print(process_f(), flush=True)
            except Exception as e: print('loop err', e, flush=True)
            time.sleep(int(os.environ.get('STRAT_F_POLL_SEC', '60')))
    else:
        print(process_f())
