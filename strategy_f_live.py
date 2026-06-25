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
FRESH_MIN  = int(os.environ.get('STRAT_F_FRESH_MIN', '30'))
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
    end = min(r['u'] + 1 + S.MAXFILL, n)
    for i in range(r['u'] + 1, end):
        touched = lo[i] <= entry <= hi[i]
        broke = (cl[i] < far) if bull else (cl[i] > far)
        if touched:
            return 'invalid' if broke else 'filled'   # entry candle closes through -> cancel; else filled
        if broke:
            return 'invalid'                           # bodied through before any touch -> cancel
    return 'live' if end < n or (r['u'] + 1 + S.MAXFILL) > n else 'expired'


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
                print('F-ALERT', code, k, flush=True); state[k] = 'alerted'; fired += 1
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
