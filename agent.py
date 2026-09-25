"""
AGENT live (osobny serwis — NIE wrzucac do NQsignals).
- TV (alert co domkniety bar 1m) -> POST /bars  [CICHO, bez Telegrama]
- agent trzyma bufor, liczy det_v10.py (LIVE), i TYLKO nowe potwierdzone setupy -> POST na WEBHOOK_URL (Telegram)
- na starcie oznacza istniejace setupy jako 'widziane' (zero zalewania historia)

ENV:
  WEBHOOK_URL  = https://YOUR-ALERT-RELAY/webhook?secret=CREATE_A_SECRET
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
import market_context  # weekly regime + daily ICT bias + causal Monitor history; informational only
import pnl         # UNIFIED P&L JOURNAL — izolowane: nowa tabela `fills` + trasy /pnl; nie rusza intake'u/detektora
import how_ab      # A/B "how it works" page at /how — isolated add-on, does not touch the detector
import cme_calendar  # v22: kalendarz CME (swieta/early close) dla heartbeat — koniec falszywych STALE w swieta
import dashboard   # / — unified home shell (federuje istniejące strony; izolowany dodatek)
import dol_dashboard  # /dol — read-only ranked DOL metadata panel
import shadow      # /shadow/data + /shadow/log — LIVE shadow-executor log (hands-off, no money; isolated add-on)
import downside_manager_shadow_v1  # read-only fixed-2R vs frozen downside manager; feature-flagged
import ab_dol_live # ranked DOL/narrative metadata; attached only at persistence, never read by execution
import a_cont_both_aligned_shadow  # post-decision A Continuation + frozen multi-horizon DOL shadow
import dol_delivery_reversal_shadow  # post-decision DOL Delivery Reversal shadow; no broker authority
import dol_reversal_manager_shadow_v1  # dedicated DOL Reversal manager challenger; shadow-only
import dol_reversal_control  # fail-closed activation gate + immutable DOL identities
import dol_reversal_live  # DOL entry/manager lifecycle via the account-specific TradersPost webhook
import continuation_shadow  # independent canonical HTF Continuation Policy-B shadow; broker-inert
import continuation_live  # opt-in, Guard-routed LONG/SHORT execution adapter
import forex_pnl   # forexpnl - joined forex-only P&L (isolated add-on)
import fxguard     # /fxguard - joined forex Auto-Executor view (isolated add-on)
import allview     # /all/trades + /all/candidates - joined view across A/B/C/F (isolated add-on)
import ab_quality  # causal Q0-Q3 A/B label; shadow-only, never changes execution
import guardrails  # /guard — MFF-eval-safe auto-exec gate (dedup, sessions, DD/target halt) — isolated add-on
import portfolio_guard  # append-only audit of actual Guard notes; read-only dashboard
import ab_shallow  # causal A/B-shallow sibling; one shared setup-group budget
import ab_candidates_view  # /ab/candidates — joined step-by-step A/B + Shallow funnel
import m15_shadow_strategy  # M15 setup + M5 BOS; isolated candidates/forward shadow, no order path

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
CAND_TRACE = os.path.join(DATA_DIR, 'candidate_trace.json')  # refreshed by the normal live detector on every bar
SEED_CSV    = os.environ.get('SEED_CSV', os.path.join(HERE,'seed.csv'))  # najswiezszy Databento CSV
MARKET_CONTEXT_DB = os.environ.get('MARKET_CONTEXT_DB', os.path.join(HERE, market_context.DATABASE_FILE))
MARKET_PREDICTIONS_DB = os.environ.get(
    'MARKET_PREDICTIONS_DB', os.path.join(DATA_DIR, market_context.PREDICTION_DATABASE_FILE))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL','')
BUFFER_BARS = int(os.environ.get('BUFFER_BARS','14000'))
VERSION = 'v31.23-classification-backfill'
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

# ====== v25: PER-SATELLITE WATCH (C, F) — down + disabled + starved ======
# C and F are SEPARATE Railway services. Two ways they silently break: (1) the service dies (crash/sleep/
# redeploy) — A/B's own feed is fine so its heartbeat stays happy; (2) the service is UP but DISABLED
# (enabled=false) — it still 200s on bars but produces ZERO signals (exactly the F config-drift on 07-17).
# The heartbeat loop below therefore CACHE-BUSTS each satellite's /health and checks reachable + enabled,
# plus uses the fanout timestamp (recorded here) to catch "up+enabled but A/B stopped forwarding" (starved).
_sat = {'C': {'ok_at': None, 'alerted': False},
        'F': {'ok_at': None, 'alerted': False}}
SAT_STALE_MIN = float(os.environ.get('SAT_STALE_MIN', '20'))   # min without an accepted bar = satellite stale
SAT_WATCH     = os.environ.get('SAT_WATCH', '1') != '0'        # default ON; SAT_WATCH=0 to silence C/F alerts

def _init_db():
    c=sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS signals(
        key TEXT PRIMARY KEY, logged_at TEXT, date TEXT, model TEXT, cat TEXT, dir TEXT,
        trig TEXT, disp_end TEXT, bounce TEXT, bos TEXT,
        entry REAL, ote62 REAL, ote79 REAL, SL REAL, TP REAL,
        fvg_lo REAL, fvg_hi REAL, bias TEXT, bias_align TEXT,
        trail TEXT, alert TEXT, posted TEXT, result TEXT, pnl REAL,
        dol_json TEXT, quality_json TEXT)''')
    if 'dol_json' not in {row[1] for row in c.execute('PRAGMA table_info(signals)')}:
        c.execute('ALTER TABLE signals ADD COLUMN dol_json TEXT')
    if 'quality_json' not in {row[1] for row in c.execute('PRAGMA table_info(signals)')}:
        c.execute('ALTER TABLE signals ADD COLUMN quality_json TEXT')
    c.commit(); c.close()

def _save_db(x, alert_text, code):
    # Deliberately after the canonical alert/guard/execution decision.  No DOL
    # value can influence whether or how this A/B signal trades.
    if x.get('_strat', 'A/B') == 'A/B' and '_dol' not in x:
        ab_dol_live.attach_metadata(x, BUF)
    # Read-only shadow consumer.  This runs after the canonical decision and
    # cannot alter, submit, or retry the existing A/B order.
    try:
        a_cont_both_aligned_shadow.observe(
            x, x.get('_dol'), candidate_id=live_emit.key(x))
    except Exception as _ba:
        print('[A_CONT_BOTH_ALIGNED] persistence err', _ba, flush=True)
    try:
        dol_delivery_reversal_shadow.observe(
            x, x.get('_dol'), candidate_id=live_emit.key(x))
    except Exception as _dr:
        print('[DOL_DELIVERY_REVERSAL] persistence err', _dr, flush=True)
    c=sqlite3.connect(DB)
    c.execute('''INSERT OR IGNORE INTO signals
        (key,logged_at,date,model,cat,dir,trig,disp_end,bounce,bos,entry,ote62,ote79,SL,TP,fvg_lo,fvg_hi,bias,bias_align,trail,alert,posted,result,pnl,dol_json,quality_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (live_emit.key(x), dt.datetime.utcnow().isoformat(timespec='seconds'),
         x['date'],x['model'],x['cat'],x['dir'],x.get('trig',''),x.get('disp_end',''),x.get('bounce',''),x['bos'],
         x['entry'],x.get('ote62'),x.get('ote79'),x['SL'],x['TP'],x['fvg_lo'],x['fvg_hi'],
         x['bias'],x['bias_align'], json.dumps(x.get('trail',[])), alert_text, str(code), '', None,
         json.dumps(x.get('_dol')) if x.get('_dol') is not None else None,
         json.dumps(x.get('_ab_quality')) if x.get('_ab_quality') is not None else None))
    c.commit(); c.close()

    # Observe only after canonical signal persistence. This module has no
    # broker authority and cannot change the signal or sent order.
    try:
        _canonical_emitted = (code == 'exec' or code == 'exec-manual' or
                              code == 'no-url' or
                              (isinstance(code, int) and 200 <= code < 300) or
                              (isinstance(code, str) and code.startswith('2') and code.isdigit()))
        if _canonical_emitted:
            _shadow_qty = x.get('_sent_qty') or x.get('_exec_qty_override')
            if _shadow_qty is None:
                _sized = live_emit.size_for(x['entry'], x['SL'])
                _shadow_qty = int(_sized[0]) if _sized else 0
            downside_manager_shadow_v1.observe_signal(x, live_emit.key(x), _shadow_qty)
    except Exception as _dm_exc:
        print('[downside-shadow] observe error:', _dm_exc, flush=True)

def _entry_cancel_after_sec():
    """Broker-side expiry for resting ENTRY limits.

    TradersPost supports ``cancelAfter`` from 1 to 3600 seconds.  By default the
    broker clock is kept identical to the model's FILL_WIN_MIN, so a 10-minute
    model no-fill cannot remain resting at the broker for the rest of the day.
    EXEC_CANCEL_AFTER_SEC may override the value explicitly.
    """
    try:
        raw = os.environ.get('EXEC_CANCEL_AFTER_SEC', '').strip()
        if raw:
            sec = int(round(float(raw)))
        else:
            sec = int(round(float(os.environ.get('FILL_WIN_MIN', '10') or 10) * 60.0))
    except Exception:
        sec = 600
    return max(1, min(3600, sec))


def _signal_reject_after_sec():
    """Maximum TradersPost queue age for a just-generated execution request."""
    try:
        sec = int(round(float(os.environ.get('EXEC_REJECT_AFTER_SEC', '15') or 15)))
    except Exception:
        sec = 15
    return max(1, min(30, sec))


def _exec_order(x, text=None):
    """Route 2 (semi-auto): wyślij PRE-STAGED zlecenie bracket do TradersPost (EXEC_WEBHOOK).
    Z auto-submit OFF w TradersPost zlecenie czeka na Twoje 1-klik zatwierdzenie (MFF: nadzór nad
    każdym wejściem). NIE rusza strategii — to tylko dodatkowe wyjście.
    EXEC_QTY='auto' (domyślnie) = ryzyko jak w alercie (size_for); liczba = sztywno; EXEC_MAX_QTY = limit."""
    if os.environ.get('EXEC_FX', '') == '1':          # FX services: MetaApi/MT5 adapter (exec_fx.py)
        try:
            import exec_fx
            return exec_fx.place(x, text)
        except Exception as _fe:
            print('EXEC_FX import/place err', _fe, flush=True)
            return {"sent": False, "error": str(_fe)}
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
        _risk_override = x.get('_risk_pct_override')
        _risk_budget_usd = x.get('_risk_budget_usd')
        _strict_risk = bool(x.get('_strict_risk_budget'))
        if _strict_risk:
            try:
                _sf = (live_emit.size_for_budget(e, sl, _risk_budget_usd)
                       if _risk_budget_usd is not None
                       else live_emit.size_for(e, sl, _risk_override))
                qty = int(_sf[0]) if _sf else 0
            except Exception:
                qty = 0
        elif _q.isdigit() and int(_q) > 0:
            qty = int(_q)
        else:
            try:
                _sf = live_emit.size_for(e, sl, _risk_override)
                qty = int(_sf[0]) if _sf else 0
            except Exception:
                qty = 0
        if _strict_risk and qty < 1:
            return {"sent": False, "reason": "risk_budget_below_one_contract", "qty": 0}
        if qty < 1:
            qty = 1
        if not _strict_risk:
            qty = int(round(qty * float(x.get('_size_mult', 1.0))))   # 🧲 normal A/B size-up path
        try:      # ⭐ SELECT size skew: T4 setups (~+0.30R live vs +0.195R baseline) get more size. Off by default.
            _ssm = float(os.environ.get('SELECT_SIZE_MULT', '1') or 1)
            if (not _strict_risk) and x.get('_select') and _ssm != 1.0: qty = max(1, int(round(qty * _ssm)))
        except Exception: pass
        try:      # 🌙 per-session size multiplier — overnight test sessions run reduced size until proven.
                  # SESSION_SIZE_MULT='ASIA:0.5,LO:0.5' default; '' disables; e.g. 'ASIA:0.25,LO:0.5,PREM:0.75'
            _smv = os.environ.get('SESSION_SIZE_MULT', 'ASIA:0.5,LO:0.5')
            if _smv:
                _ss = guardrails._sess_of(x)
                for _kv in _smv.split(','):
                    _k, _v = _kv.split(':')
                    if _k.strip() == _ss:
                        _session_qty = max(1, int(round(qty * float(_v))))
                        # Strict dollar budgets (Continuation/shared groups) may
                        # be reduced by a session rule, never increased by it.
                        qty = min(qty, _session_qty) if _strict_risk else _session_qty
                        break
        except Exception: pass
        try:      # 📈 GUARD_DYN_RISK=1: scale size with the DD cushion — never above base while the buffer
                  # is thin, up to DYN_RISK_MAX_MULT× once the buffer outgrows DYN_RISK_BASE_BUF ($).
                  # Rationale: worst guarded day = 2 losses; keep 2R well under ~1/3 of the live buffer.
            if (not _strict_risk) and os.environ.get('GUARD_DYN_RISK', '0') == '1':
                _buf = float(guardrails.eval_progress().get('buffer') or 0)
                _base = float(os.environ.get('DYN_RISK_BASE_BUF', '3000'))
                _mx = float(os.environ.get('DYN_RISK_MAX_MULT', '2'))
                if _buf > _base: qty = int(round(qty * min(_mx, _buf / _base)))
        except Exception: pass
        if x.get('_exec_qty_override') is not None:
            qty = int(x['_exec_qty_override'])            # guardrails min-size ramp (first N live trades = 1)
        if x.get('_mon_quarter'):
            qty = max(1, int(round(qty * 0.5)))           # Monday quarter-size (0.5% -> 0.25%) if MONDAY_MODE=quarter
        _cap = os.environ.get('EXEC_MAX_QTY', '15').strip()   # default HARD cap: a 5-pt SL used to compute 50 micros
        if _cap.isdigit() and int(_cap) > 0:
            qty = min(qty, int(_cap))
        if x.get('_group_qty_cap') is not None:
            qty = min(qty, int(x['_group_qty_cap']))
        if _strict_risk and qty < 1:
            return {"sent": False, "reason": "risk_budget_below_one_contract", "qty": 0}
        qty = max(1, int(qty))
        _tk = float(os.environ.get('EXEC_TICK', '0.25') or 0)   # tick-align prices before the broker
        def _t(p):                                                 # sees them (OTE math emits 29043.43;
            return round(round(p / _tk) * _tk, 6) if _tk > 0 else round(p, 2)   # MNQ trades in 0.25s)
        x['_exec_entry'] = _t(e + off)  # tp is recomputed from the POST-ENTRY_OFFSET_PTS entry; x['TP']
                                        # is the detector's PRE-offset value (3 pts apart at offset=1).
        # ---- v30: 1R partial. Split ONE signal into TWO brackets at the broker:
        #   leg A ("banker"):  PARTIAL_ACCT_PCT of the account realized at exactly +1R
        #                      (0.2% at RISK_PCT 0.5 -> 40% of the contracts, TP = entry +/- 1R)
        #   leg B ("runner"):  the rest, TP = the detector's target (v30 swing level / 2R).
        # Same entry limit, same stop, same TIF on both -> they fill and stop together; only the
        # targets differ. Entirely broker-side: no dependency on the agent being awake mid-trade.
        # PARTIAL_AT_1R=0 disables (single bracket, exactly the v29 behaviour). qty=1 cannot split.
        # v30: the runner's target is the DETECTOR's TP (swing level or 2R fallback), not a local
        # 2R recompute. The old inline `tp = e ± 2R` above stays only as a fallback for records
        # without a TP field. (Caught by the executor test: leg B was going to 2R while the
        # detector aimed at the swing level.)
        try: tp = float(x['TP']) if x.get('TP') is not None else tp
        except Exception: pass
        x['_exec_tp'] = _t(tp + off)   # the book/shadow must score the target the broker actually receives
        legs = [(qty, tp)]
        try:
            if (not x.get('_disable_partial') and
                    os.environ.get('PARTIAL_AT_1R', '0') == '1' and qty >= 2):   # v30.1: default OFF (measured: costs ~14%/yr for little protection); PARTIAL_AT_1R=1 re-enables
                _rp  = float(_risk_override if _risk_override is not None else os.environ.get('RISK_PCT', '0.5') or 0.5)
                _pp  = float(os.environ.get('PARTIAL_ACCT_PCT', '0.2') or 0.2)
                _fr  = max(0.0, min(0.9, _pp / _rp)) if _rp > 0 else 0.0
                qa   = int(round(qty * _fr))
                if 0 < qa < qty:
                    r1 = (e + R) if bull else (e - R)
                    legs = [(qa, r1), (qty - qa, tp)]
        except Exception as _pe:
            print('EXEC partial split err (single bracket fallback)', _pe, flush=True)
        x['_legs'] = [{"qty": q_, "tp": _t(t_ + off)} for q_, t_ in legs]
        st = None; body = ''; leg_results = []; accepted_legs = 0
        for _i, (q_, t_) in enumerate(legs):
            payload = {
                "ticker": os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!')),
                "action": "buy" if bull else "sell",
                "orderType": "limit",
                "limitPrice": _t(e + off),
                "quantity": q_,
                "takeProfit": {"limitPrice": _t(t_ + off)},
                "stopLoss": {"type": "stop", "stopPrice": _t(sl + off)},
                "timeInForce": os.environ.get('EXEC_TIF', 'day').strip().lower() or 'day',
                "cancelAfter": _entry_cancel_after_sec(),
                "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                "rejectAfter": _signal_reject_after_sec(),
            }
            if _i == 0 and x.get('_strat') == 'DOL_DELIVERY_REVERSAL':
                _claimed, _claim_reason = dol_reversal_live.claim_entry(x, qty, payload)
                if not _claimed:
                    return {"sent": False, "reason": _claim_reason, "qty": qty,
                            "route_id": guardrails._exec_route_id()}
            if x.get('_test_signal'):
                payload["test"] = True
                payload["extras"] = dict(payload.get("extras") or {}, **{
                    "chainTest": str(x.get('_test_label') or 'single-route-test'),
                    "accountLabel": os.environ.get('ACCOUNT_LABEL', 'account'),
                    "routeId": guardrails._exec_route_id(),
                })
            if text and _i == 0: payload["text"] = text
            try:
                r = requests.post(url, json=payload, timeout=10)
                st = getattr(r, 'status_code', None)
                try: body = (r.text or '')[:200]
                except Exception: body = ''
                ok_leg = st is not None and 200 <= int(st) < 300
            except Exception as e:
                st = None; body = str(e)[:200]; ok_leg = False
                if _i == 0 and x.get('_strat') == 'DOL_DELIVERY_REVERSAL':
                    dol_reversal_live.record_entry_response(x, None, error=str(e))
            else:
                if _i == 0 and x.get('_strat') == 'DOL_DELIVERY_REVERSAL':
                    dol_reversal_live.record_entry_response(x, r)
            leg_results.append({"leg": _i + 1, "status": st, "ok": ok_leg, "qty": q_})
            if ok_leg: accepted_legs += 1
            print('EXEC', st, ('leg %d/%d' % (_i + 1, len(legs))), payload, flush=True)
            if not ok_leg:
                break                         # never send later legs after a failed sibling/partial leg
        all_ok = len(leg_results) == len(legs) and accepted_legs == len(legs)
        return {"sent": all_ok, "accepted_any": accepted_legs > 0,
                "accepted_legs": accepted_legs, "status": st, "resp": body,
                "legs": len(legs), "leg_results": leg_results,
                "route_id": guardrails._exec_route_id(), "qty": qty}
    except Exception as ex:
        print('EXEC err', ex, flush=True)
        return {"sent": False, "error": str(ex), "route_id": guardrails._exec_route_id()}

def _signal_bar_close(x):
    """Return the close of the detector BOS bar from the same rolling buffer.

    This is the only price used to construct A/B-shallow, keeping the sibling
    causal.  ``bos_bar`` is preferred; ``bos_iso`` is a defensive fallback.
    """
    try:
        wanted = x.get('bos_bar')
        wanted = int(wanted) if wanted is not None else None
        bos_iso = str(x.get('bos_iso') or '')
        with open(BUF, newline='', encoding='utf-8') as f:
            for i, row in enumerate(csv.DictReader(f)):
                if wanted is not None and i == wanted:
                    return float(row['close'])
                if bos_iso and str(row.get('ts_event') or '').replace('+00:00', 'Z') == bos_iso:
                    return float(row['close'])
    except Exception as e:
        print('A/B-shallow signal close lookup err', e, flush=True)
    return None

def _prepare_ab_siblings(repx):
    """Build one causal setup group: normal A/B plus optional A/B-shallow.

    The group is guarded once. Its siblings are never passed separately through
    ``position_open``/duplicate checks, so the one-position rule blocks OTHER
    setups, not the second leg of this same A/B signal.
    """
    repx['_strat'] = repx.get('_strat', 'A/B')
    acct = float(os.environ.get('ACCOUNT', '100000') or 100000)
    deep_pct = float(os.environ.get('RISK_PCT', '0.5') or 0.5)
    repx['_planned_group_risk_usd'] = max(0.0, acct * deep_pct / 100.0)
    if repx['_strat'] != 'A/B' or not ab_shallow.enabled():
        return [repx]
    if repx.get('_exec_qty_override') is not None and os.environ.get('AB_SHALLOW_DURING_RAMP', '0') != '1':
        repx['_shallow_skip'] = 'ramp'
        return [repx]
    close = _signal_bar_close(repx)
    if close is None:
        repx['_shallow_skip'] = 'signal_close_missing'
        return [repx]
    repx['_signal_close'] = close
    gid = ab_shallow.setup_group_id(repx)
    repx['_setup_group_id'] = gid
    requested = ab_shallow.setup_group_budget_usd()
    capacity = guardrails.setup_group_risk_capacity(requested)
    allowed = float(capacity.get('allowed') or 0.0)
    dynamic_env = dict(os.environ)
    dynamic_env['SETUP_GROUP_RISK_USD'] = str(allowed)
    repx['_setup_group_budget_usd'] = requested
    repx['_setup_group_allowed_usd'] = allowed
    repx['_setup_group_floor_capacity'] = capacity
    repx['_risk_budget_usd'] = allowed * 0.5
    repx['_risk_pct_override'] = (100.0 * repx['_risk_budget_usd'] / acct) if acct > 0 else 0.0
    repx['_strict_risk_budget'] = True
    repx['_risk_mode'] = 'shared_group'
    repx.pop('_size_mult', None)
    repx.pop('_select', None)
    try:
        shallow = ab_shallow.build_shallow_signal(repx, dynamic_env)
        meta = ab_shallow.apply_shared_group_budget(repx, shallow, dynamic_env)
        repx['_ab_risk_meta'] = meta
        shallow['_ab_risk_meta'] = meta
        repx['_batch_sibling'] = True
        shallow['_batch_sibling'] = True
        items = [repx, shallow]
    except Exception as e:
        repx['_shallow_skip'] = str(e)
        print('A/B-shallow build skip:', e, flush=True)
        items = [repx]

    # Calculate the exact integer quantities before the guard decision. The
    # executor may reduce these quantities, but `_group_qty_cap` prevents any
    # later multiplier or override from increasing them.
    viable = []
    planned = 0.0
    for item in items:
        sf = live_emit.size_for_budget(item['entry'], item['SL'], item.get('_risk_budget_usd'))
        max_qty = int(sf[0]) if sf else 0
        if item.get('_exec_qty_override') is not None:
            max_qty = min(max_qty, int(item['_exec_qty_override']))
        try:
            cap = int(os.environ.get('EXEC_MAX_QTY', '15') or 15)
            if cap > 0:
                max_qty = min(max_qty, cap)
        except Exception:
            pass
        if max_qty < 1:
            item['_risk_budget_skip'] = 'below_one_contract'
            continue
        per_contract = float(sf[2])
        leg_risk = max_qty * per_contract
        item['_group_qty_cap'] = max_qty
        item['_planned_leg_risk_usd'] = round(leg_risk, 2)
        item['_setup_group_budget_usd'] = requested
        item['_setup_group_allowed_usd'] = allowed
        viable.append(item)
        planned += leg_risk
    if not viable:
        repx['_setup_group_budget_unavailable'] = True
        repx['_planned_group_risk_usd'] = 0.0
        return [repx]
    planned = min(planned, allowed, requested)
    for item in viable:
        item['_planned_group_risk_usd'] = round(planned, 2)
    return viable


def _batch_group_id(items):
    for item in items:
        if item.get('_setup_group_id'):
            return str(item['_setup_group_id'])
    return 'single_' + live_emit.key(items[0])


def _exec_sibling_batch(items, base_text):
    """Send one setup group with a persistent fail-closed reservation.

    The guard is called once for the whole setup, therefore A/B and
    A/B-shallow do not block each other. If one relay call succeeds and a later
    sibling fails, the system sends CANCEL then EXIT. A rollback without 2xx
    relay confirmation creates a hard kill because broker state is uncertain.
    """
    if not items:
        return False, [], {'ok': False, 'reason': 'empty_batch'}
    gid = _batch_group_id(items)
    planned = max(float(i.get('_planned_group_risk_usd') or 0.0) for i in items)
    # Preflight every sibling before the first network call.
    for item in items:
        try:
            float(item['entry']); float(item['SL']); float(item['TP'])
            if item.get('dir') not in ('LONG', 'SHORT'):
                raise ValueError('bad direction')
        except Exception as e:
            return False, [(item, {'sent': False, 'reason': 'preflight:' + str(e)}, '')], {'ok': True, 'reason': 'preflight'}
    if not guardrails.begin_sibling_batch(gid, planned, [i.get('_strat', 'A/B') for i in items]):
        return False, [], {'ok': False, 'reason': 'batch_reservation_failed'}
    results = []
    accepted = []
    for item in items:
        itxt = base_text if item.get('_strat', 'A/B') == 'A/B' else live_emit.to_alert(item)
        item['_alert_txt'] = itxt
        item['_batch_group_id'] = gid
        res = _exec_order(item, itxt)
        item['_sent_qty'] = res.get('qty')
        results.append((item, res, itxt))
        try:
            ok = bool(res.get('sent')) and 200 <= int(res.get('status') or 0) < 300
        except Exception:
            ok = False
        if ok:
            accepted.append(item)
            guardrails.touch_sibling_batch(gid, item.get('_strat', 'A/B'), res.get('status'))
            continue
        accepted_any = bool(res.get('accepted_any') or accepted)
        if accepted_any:
            rb = guardrails.rollback_sibling_batch(gid, 'partial_send')
            for a in accepted:
                a['_batch_accepted_then_rollback'] = True
                a['_rollback_confirmed'] = bool(rb.get('ok'))
            return False, results, rb
        guardrails.finish_sibling_batch(gid, 'failed_before_accept')
        return False, results, {'ok': True, 'reason': 'nothing_accepted'}
    return True, results, {'ok': True, 'group_id': gid}

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
    with open(ARCHIVE,'a',newline='') as f:          # pelna historia — NIGDY nie przycinana
        w=csv.writer(f)
        if arch_new: w.writerow(COLS)
        w.writerow(row)
    rows = []
    if os.path.exists(BUF):
        try:
            with open(BUF, newline='') as f:
                rows = list(csv.reader(f))
        except Exception:
            rows = []
    data = rows[1:] if rows and rows[0] == COLS else rows
    data.append(row)
    tmp = BUF + '.tmp'
    with open(tmp, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(data[-BUFFER_BARS:])
    os.replace(tmp, BUF)

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
    trace_work = CAND_TRACE + '.live'
    env=dict(os.environ, DATA_CSV=BUF, OUT_PKL=OUT, CUTOFF='',
             DEBUG_TRACE='1', TRACE_OUT=trace_work)   # one detector run also refreshes the live candidate page
    if gated:
        env['EOD_INTRADAY'] = '1' if _eod_flag() else ''        # regime-gated: ON w choppy, OFF w trend
    _det = subprocess.run(['python3', os.path.join(HERE, det_file)], env=env,
                          capture_output=True, timeout=180)
    if _det.returncode == 0 and os.path.exists(trace_work):
        os.replace(trace_work, CAND_TRACE)             # readers never see a half-written JSON trace
    import pickle
    try: conf=pickle.load(open(OUT,'rb'))
    except Exception: conf=[]
    return conf, []                                              # wejscie = LIMIT po BOS

# ====== KALENDARZ NEWSOW (ForexFactory weekly) + FLAGI NO-TRADE ======
HIGH = {'CPI','Core CPI','Non-Farm','NFP','PPI','GDP','Core PCE','PCE','ISM','FOMC','Federal Funds','Powell'}
_cal = {'at': None, 'events': [], 'raw_events': []}   # execution tuples + richer Monitor-only metadata
def _load_calendar():
    if requests is None: return
    if _cal['at'] and (dt.datetime.utcnow()-_cal['at']).total_seconds() < 6*3600: return
    try:
        r=requests.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=15)
        evs=[]; raw=[]
        for e in r.json():
            if str(e.get('impact','')).lower()!='high': continue
            t=dt.datetime.fromisoformat(e['date']).timestamp()
            title=e.get('title','event')
            evs.append((t, title))
            raw.append(dict(epoch=t, title=title, country=e.get('country') or e.get('currency') or '',
                            impact='high', source='ForexFactory'))
        _cal['events']=evs; _cal['raw_events']=raw; _cal['at']=dt.datetime.utcnow()
    except Exception as ex:
        print('[cal] blad pobierania:', ex, flush=True)   # guard side: NEWS_STRICT=1 blokuje sendy gdy kalendarz nieosiagalny >24h

def _cal_age_h():
    """Hours since the last SUCCESSFUL ForexFactory calendar fetch (None = never). Feeds the
    fail-closed news gate: can't verify news => guard blocks unattended sends (NEWS_STRICT)."""
    try:
        _load_calendar()
        if not _cal['at']: return None
        return (dt.datetime.utcnow() - _cal['at']).total_seconds() / 3600.0
    except Exception:
        return None


def _market_context_news():
    """High-impact macro risk for Monitor only; never creates market direction."""
    try: _load_calendar()
    except Exception: pass
    now = dt.datetime.now(dt.timezone.utc); rows=[]
    raw = _cal.get('raw_events') or [dict(epoch=item[0], title=item[1], country='', impact='high', source='ForexFactory')
                                     for item in (_cal.get('events') or [])]
    critical_words = ('cpi', 'non-farm', 'nfp', 'fomc', 'federal funds', 'core pce', 'powell', 'gdp')
    for item in raw:
        country = str(item.get('country') or '').upper()
        if country and country not in ('USD', 'US', 'USA'):
            continue
        try: when = dt.datetime.fromtimestamp(float(item['epoch']), tz=dt.timezone.utc)
        except Exception: continue
        minutes = (when - now).total_seconds() / 60.0
        if minutes < -360 or minutes > 8 * 24 * 60:
            continue
        title = str(item.get('title') or 'High-impact event')
        severity = 'critical' if any(word in title.lower() for word in critical_words) else 'high'
        status = 'live_window' if abs(minutes) <= 30 else 'soon' if 0 < minutes <= 24*60 else 'recent' if minutes < 0 else 'upcoming'
        rows.append(dict(title=title, country=country or 'USD/high-impact', impact='high', severity=severity,
                         time_utc=when.isoformat(), time_et=when.astimezone(ZoneInfo('America/New_York')).isoformat(),
                         minutes_from_now=round(minutes,1), status=status, source=item.get('source','ForexFactory')))
    rows.sort(key=lambda item: item['time_utc'])
    live = [item for item in rows if abs(item['minutes_from_now']) <= 30]
    next_two_hours = [item for item in rows if 0 <= item['minutes_from_now'] <= 120]
    next_day_critical = [item for item in rows if item['severity']=='critical' and 0 <= item['minutes_from_now'] <= 1440]
    risk = 'EXTREME' if live else 'HIGH' if next_two_hours or next_day_critical else 'ELEVATED' if rows else 'NORMAL'
    age = None if not _cal.get('at') else (dt.datetime.utcnow()-_cal['at']).total_seconds()/3600.0
    return dict(ok=bool(_cal.get('at')), status='fresh' if age is not None and age <= 12 else 'stale_or_unavailable',
                risk_level=risk, calendar_age_hours=None if age is None else round(age,2),
                directional_effect='none', execution_effect='none', events=rows,
                note='Scheduled-event risk only. News never creates bullish/bearish BIAS.')

def _monitor_bias_gate(x):
    """Block auto execution when the Monitor's current daily bias opposes the signal direction."""
    if os.environ.get('MONITOR_BIAS_GATE', '1') != '1':
        return True, 'monitor_bias_gate_off'
    expected = {'LONG': 'BULLISH', 'SHORT': 'BEARISH'}.get(str(x.get('dir') or '').upper())
    if not expected:
        return False, 'monitor_bias_bad_direction'
    try:
        report = market_context.build_report(_market_context_sources(), daily_limit=7, weekly_limit=4,
                                             database_path=MARKET_CONTEXT_DB,
                                             prediction_database_path=MARKET_PREDICTIONS_DB)
        if not report.get('ok'):
            x['_monitor_bias_error'] = report.get('error')
            return False, 'monitor_bias_unavailable'
        data = report.get('data') or {}
        if data.get('stale'):
            x['_monitor_bias_age_minutes'] = data.get('age_minutes')
            return False, 'monitor_bias_stale'
        daily = report.get('daily') or {}
        if not daily.get('ok'):
            x['_monitor_bias_error'] = daily.get('error')
            return False, 'monitor_bias_unavailable'
        bias = daily.get('bias')
        confidence = float(daily.get('confidence') or 0.0)
        x['_monitor_bias'] = bias
        x['_monitor_confidence'] = confidence
        x['_monitor_as_of'] = daily.get('as_of')
        x['_monitor_expected_bias'] = expected
        min_conf = float(os.environ.get('MONITOR_BIAS_MIN_CONF', '0') or 0)
        if bias in ('BULLISH', 'BEARISH') and bias != expected and confidence >= min_conf:
            return False, 'monitor_bias:' + str(bias)
        return True, 'ok'
    except Exception as e:
        x['_monitor_bias_error'] = str(e)
        print('[monitor_bias_gate] err', e, flush=True)
        return False, 'monitor_bias_error'

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


def _continuation_live_signal(order):
    """Translate one frozen order into the existing Guard/executor contract."""
    activation_ms = int(order['activation_ms'])
    when = dt.datetime.fromtimestamp(activation_ms / 1000.0, tz=dt.timezone.utc).astimezone(NY)
    direction = str(order['direction']).upper()
    entry = float(order['entry_price']); sl = float(order['stop_price']); tp = float(order['target_price'])
    risk = abs(entry - sl)
    if direction not in ('LONG', 'SHORT') or risk <= 0:
        raise ValueError('invalid Continuation direction/geometry')
    if not ((direction == 'LONG' and sl < entry < tp) or
            (direction == 'SHORT' and tp < entry < sl)):
        raise ValueError('invalid Continuation Entry/SL/OPEN-DOL geometry')
    sess = ('ASIA' if (when.hour >= 18 or when.hour < 2) else
            'LO' if when.hour < 5 else 'PREM' if (when.hour < 9 or (when.hour == 9 and when.minute < 30)) else
            'NYAM' if when.hour < 11 else 'NYL' if (when.hour < 13 or (when.hour == 13 and when.minute < 30)) else
            'NYPM' if when.hour < 16 else 'PM_AH')
    is_abdir = str(order.get('strategy') or '').upper() == 'AB_DIRECTIONAL'
    strategy = (('A/B Directional LONG' if direction == 'LONG' else 'A/B Directional SHORT') if is_abdir else
                ('Continuation LONG' if direction == 'LONG' else 'Continuation SHORT'))
    return {
        'date': when.strftime('%Y-%m-%d'), 'model': ('A/B Directional' if is_abdir else 'Continuation'),
        'cat': strategy + (' · Fixed 2R' if is_abdir else ' · OPEN DOL'),
        'dir': direction, 'bos': when.strftime('%H:%M'), 'bos_ms': activation_ms,
        'entry_ms': activation_ms, 'entry': entry, 'SL': sl, 'TP': tp,
        'fvg_lo': min(entry, sl), 'fvg_hi': max(entry, sl),
        'bias': direction, 'bias_align': 'Y', 'trail': [], 'brk': 1,
        # Explicit audit/display provenance. Continuation uses the structural
        # protection level and the frozen open-DOL target, not A/B swing/2R.
        'sl_src': 'struct', 'tp_src': ('2R' if is_abdir else 'open_dol'),
        'sess': sess, '_strat': strategy, '_continuation_order_id': str(order['order_id']),
        '_continuation_candidate_id': str(order['candidate_id']), '_continuation_dol_id': str(order['dol_id']),
        '_disable_partial': True, '_strict_risk_budget': True,
    }


def _continuation_live_budget(profile):
    """Hard ceilings: $500 on Pro 100K and $250 on Builder/Rapid 50K."""
    is_50k = profile.get('plan') in ('builder50', 'rapid_eod50')
    ceiling = 250.0 if is_50k else 500.0
    key = 'CONTINUATION_RISK_USD_50K' if is_50k else 'CONTINUATION_RISK_USD_100K'
    try: requested = float(os.environ.get(key, str(ceiling)) or ceiling)
    except Exception: requested = ceiling
    return max(0.0, min(requested, ceiling))


def _directional_live_budget(profile):
    """Separate capped risk switch for A/B Directional; never borrows Continuation config."""
    is_50k = profile.get('plan') in ('builder50', 'rapid_eod50')
    ceiling = 250.0 if is_50k else 500.0
    key = 'AB_DIRECTIONAL_RISK_USD_50K' if is_50k else 'AB_DIRECTIONAL_RISK_USD_100K'
    try: requested = float(os.environ.get(key, str(ceiling)) or ceiling)
    except Exception: requested = ceiling
    return max(0.0, min(requested, ceiling))


def _dispatch_continuation_live(order):
    """Run one new Continuation order through this account's normal Guard and route."""
    x = _continuation_live_signal(order)
    profile = guardrails.account_profile()
    base = {'account_label': profile.get('label'), 'route_id': guardrails._exec_route_id()}
    if not profile.get('config_ok'):
        reason = 'account_config:' + ','.join(profile.get('config_warnings') or ['invalid'])
        guardrails.note(x, 'blocked', reason)
        return dict(base, state='BLOCKED', reason=reason)
    is_abdir = str(order.get('strategy') or '').upper() == 'AB_DIRECTIONAL'
    budget = _directional_live_budget(profile) if is_abdir else _continuation_live_budget(profile)
    if budget <= 0:
        reason = 'ab_directional_risk_disabled' if is_abdir else 'continuation_risk_disabled'
        guardrails.note(x, 'blocked', reason)
        return dict(base, state='BLOCKED', reason=reason)
    x['_risk_budget_usd'] = budget
    x['_planned_group_risk_usd'] = budget
    x['_risk_pct_override'] = 100.0 * budget / float(os.environ.get('ACCOUNT', '100000') or 100000)
    text = ('🧭 %s · %s\n%s LIMIT %.2f · SL %.2f · TP %.2f\n'
            'Account: %s · max risk $%.0f · order %s' %
            (x['_strat'], 'fixed 2R' if is_abdir else 'frozen OPEN DOL',
             'BUY' if x['dir'] == 'LONG' else 'SELL', x['entry'], x['SL'], x['TP'],
             profile.get('label'), budget, order['order_id']))
    x['_alert_txt'] = text
    mode = guardrails.exec_mode()
    if mode == 'off':
        guardrails.note(x, 'blocked', 'mode_off')
        return dict(base, state='BLOCKED', reason='mode_off')
    if mode == 'manual':
        ok, reason = guardrails.manual_ok(x, _feed_age_min(), _market_open_now())
        reason = 'manual_review_only' if ok else reason
        guardrails.note(x, 'blocked', reason)
        if ok and WEBHOOK_URL:
            try: live_emit.post_webhook('🟦 MANUAL REVIEW — NO ORDER SENT\n' + text, WEBHOOK_URL)
            except Exception: pass
        return dict(base, state='BLOCKED', reason=reason)

    guardrails.ramp_qty(x)
    _, news_hard = flags_for(x)
    ok, reason = guardrails.guard_ok(x, feed_age_min=_feed_age_min(), market_open=_market_open_now(),
                                    news_hard=news_hard, cal_age_h=_cal_age_h())
    if not ok:
        guardrails.note(x, 'blocked', reason)
        return dict(base, state='BLOCKED', reason=reason)
    gid = ('ab_directional_' if is_abdir else 'continuation_') + str(order['order_id'])
    if not guardrails.begin_sibling_batch(gid, budget, [x['_strat']]):
        guardrails.note(x, 'blocked', 'batch_reservation_failed')
        return dict(base, state='BLOCKED', reason='batch_reservation_failed')
    result = _exec_order(x, text)
    if result.get('sent'):
        guardrails.touch_sibling_batch(gid, x['_strat'], result.get('status'))
        guardrails.note(x, 'sent')
        if WEBHOOK_URL:
            try: live_emit.post_webhook(('🟢 A/B DIRECTIONAL LIVE SENT\n' if is_abdir else
                                         '🟢 CONTINUATION LIVE SENT\n') + text, WEBHOOK_URL)
            except Exception: pass
        return dict(base, state='SENT', reason='ok', quantity=result.get('qty'),
                    route_id=result.get('route_id') or base['route_id'], broker=result)
    # A timeout/no HTTP status may have reached the external route. Never retry it.
    unknown = result.get('accepted_any') or result.get('status') is None
    if unknown:
        guardrails.touch_sibling_batch(gid, x['_strat'], result.get('status'))
        rollback = guardrails.rollback_sibling_batch(gid, 'continuation_submission_unknown')
        reason = 'submission_unknown_rolled_back' if rollback.get('ok') else 'submission_unknown'
    else:
        guardrails.finish_sibling_batch(gid, 'failed_before_accept')
        rollback = None
        reason = 'broker_rejected'
    guardrails.note(x, 'blocked', reason)
    return dict(base, state=('SUBMISSION_UNKNOWN' if unknown else 'BLOCKED'), reason=reason,
                quantity=result.get('qty'), route_id=result.get('route_id') or base['route_id'],
                broker=result, rollback=rollback)

def _process_new(now_ms=None, gap_min=None):
    global _primed
    setups, _ = _detect()
    # Causal display-only repair for Guard rows created before classification
    # fields existed. It uses detector inputs available at the original BOS,
    # never outcomes, and cannot alter Guard/execution decisions.
    try: guardrails.backfill_trade_classifications(setups)
    except Exception as _bce: print('[guard] classification backfill err', _bce, flush=True)
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
    _gap = _feed_gap_min() if gap_min is None else gap_min
    if _gap is not None and _gap > float(os.environ.get('GAP_REPRIME_MIN','30')):
        skipped = 0
        for x in setups:
            try:
                if now_ms and x.get('bos_ms') and int(x['bos_ms']) > int(now_ms):
                    continue
            except Exception:
                pass
            sentn.add(live_emit.key(x)); sentn.add(_tkey(x)); skipped += 1
        _save_sent(sentn)
        print('GAP RE-PRIME: feed wrocil po %.0f min — pomijam %d katch-up setupow (stale poziomy)' % (_gap, skipped), flush=True)
        if WEBHOOK_URL:
            try: live_emit.post_webhook(f"♻️ Feed wrócił po przerwie ~{_gap:.0f} min — pomijam katch-up (poziomy policzone w poprzek dziury). Świeże setupy od następnego bara.", WEBHOOK_URL)
            except Exception as e: print('[gap-reprime] post err', e, flush=True)
        return {'gap_reprime': skipped, 'gap_min': round(_gap,1)}
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
        try:      # v27.4 ENTRY_OFFSET_PTS (default 0=off): quote the resting limit N pts SHALLOWER
                  # (toward market) than the detector level. Rationale: a real limit fills on trade-
                  # THROUGH, so resting 1pt shallower converts a touch of the detector level into a
                  # REAL fill — 4y through-model: +0.443R/fill vs +0.365 baseline, better in all 5
                  # years, plateau-stable 0.5-1.5pt. Applied BEFORE alert/exec/book/shadow so every
                  # witness sees the same level. SL stays the detector's (widening tested: adds nothing).
            _eo = float(os.environ.get('ENTRY_OFFSET_PTS', '0') or 0)
            if _eo:
                _es = 1 if repx.get('dir') == 'LONG' else -1
                repx['entry'] = round(float(repx['entry']) + _es * _eo, 2)
        except Exception as _eoe: print('entry_offset err', _eoe, flush=True)
        # DOL is a final classification of this one canonical A/B order. In
        # LIVE it changes only the label and target to fixed +2R before Guard;
        # no parallel A/B order is created.
        try:
            dol_reversal_live.classify(repx, BUF)
        except Exception as _dre:
            repx['_dol_state'] = 'DOL_STATE_UNAVAILABLE'
            print('[dol-reversal-live] classification error', _dre, flush=True)
        # Forward-observation label only. It is deliberately attached after the
        # canonical setup exists and is never read by Guard or the executor.
        try:
            if repx.get('_ab_quality') is None:
                if repx.get('signal_close') is None:
                    repx['signal_close'] = _signal_bar_close(repx)
                ab_quality.attach(repx)
        except Exception as _aqe:
            print('[ab-quality] classification error', _aqe, flush=True)
        # The human-facing alert is built before the floor-aware group is
        # prepared. Stamp the maximum equal sibling allocation now so it never
        # displays the obsolete $500/0.5% deep-leg sizing. The executor may
        # reduce it further when the live floor cushion is tight.
        if repx.get('_strat', 'A/B') == 'A/B' and ab_shallow.enabled():
            _preview_budget = ab_shallow.setup_group_leg_budget_usd()
            _preview_acct = float(os.environ.get('ACCOUNT', '100000') or 100000)
            repx['_risk_budget_usd'] = _preview_budget
            repx['_risk_pct_override'] = (100.0 * _preview_budget / _preview_acct) if _preview_acct > 0 else 0.0
        fl, hard = flags_for(rep)
        txt=live_emit.to_alert(repx)
        if repx.get('_ab_quality'):
            txt = ab_quality.tagline(repx) + '\n' + txt
        _age=(now_ms-rep['bos_ms'])/60000.0 if (now_ms and rep.get('bos_ms')) else None   # v20: stempel swiezosci
        _hdr='🕒 '+dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')+((f' · setup sprzed {_age:.0f} min'+(' ⚠️ STARY!' if _age>20 else '')) if _age is not None else '')
        txt=_hdr+'\n'+txt                                                                   # pierwsza linia = KIEDY -> stary alert widac na pierwszy rzut oka
        try:                                                                                # v22: ⭐ SELECT tag (tier T4, AB_AUDIT_6K_2026-07) — tylko oznaczenie, zero zmian logiki
            import select_tag as _sel
            _st=_sel.tagline(repx, members)
            if _st: txt=_st+txt
            if not _sel.why_not(repx, members): repx['_select']=True   # ⭐ mark T4 for SELECT_SIZE_MULT sizing
        except Exception as _se: print('select_tag err', _se, flush=True)
        try:                                                                                # 🧲 magnet size-up tag (isolated, read-only — never changes entry/SL/TP/direction)
            import magnet as _mag, sqlite3 as _sq3
            _recent=[r[0] for r in _sq3.connect(DB).execute("SELECT dir FROM signals ORDER BY logged_at DESC LIMIT 5").fetchall()][::-1]
            _mres=_mag.check(repx, *(_mag.load_buffer(BUF) or (None,None,None)), _recent)
            if _mres['magnet']:
                txt=_mres['tag']+'\n'+txt; repx['_size_mult']=_mres['size_mult']
        except Exception as _me: print('magnet err', _me, flush=True)
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
        _book_items = [repx]
        if os.environ.get('EXEC_WEBHOOK') or os.environ.get('EXEC_FX') == '1':   # FX services: MetaApi, no webhook
            _QUIET = ('duplicate', 'monday_skip', 'monday_prem')
            _TG_BLOCKED = os.environ.get('TG_BLOCKED', '0') == '1'
            repx['_alert_txt'] = txt
            _gmode = guardrails.exec_mode()                       # auto | manual | off
            # Prepare the COMPLETE group before the one and only guard decision.
            # Siblings are sent by one batch path and never block each other.
            if _gmode == 'auto':
                guardrails.ramp_qty(repx)
            _book_items = _prepare_ab_siblings(repx)

            def _blocked_items(_why):
                for item in _book_items:
                    item['_alert_txt'] = txt if item.get('_strat', 'A/B') == 'A/B' else live_emit.to_alert(item)
                    guardrails.note(item, 'blocked', _why)
                if _TG_BLOCKED and _why not in _QUIET and not str(_why).startswith('session') and WEBHOOK_URL:
                    live_emit.post_webhook(txt, WEBHOOK_URL)
                return _book_items

            def _exec_fail_alert(_res, _tag):
                m = ('🔴 EXEC FAILED (%s): %s' % (_tag, json.dumps(_res)[:240]))
                print(m, flush=True)
                if WEBHOOK_URL:
                    try: live_emit.post_webhook(m + '\n' + txt, WEBHOOK_URL)
                    except Exception: pass

            if _gmode == 'off':
                _blocked_items('mode_off'); code = 'guard:off'
                if WEBHOOK_URL: live_emit.post_webhook(txt, WEBHOOK_URL)
            elif _gmode == 'manual':
                # v31.11 SAFETY: MANUAL is review-only. It must NEVER call EXEC_WEBHOOK.
                # The prior implementation mislabeled an actually-sent TradersPost batch as "ARMED",
                # which could auto-submit at the broker when the TradersPost subscription had Auto Submit ON.
                _gok, _gwhy = guardrails.manual_ok(repx, _feed_age_min(), _market_open_now())
                if _gok:
                    _blocked_items('manual_review_only')
                    if WEBHOOK_URL:
                        try: live_emit.post_webhook('🟦 MANUAL REVIEW — NO ORDER SENT\n' + txt, WEBHOOK_URL)
                        except Exception: pass
                    code = 'manual-review'
                else:
                    _blocked_items(_gwhy); code = 'guard:' + _gwhy
            else:
                _gok, _gwhy = guardrails.guard_ok(repx, feed_age_min=_feed_age_min(),
                                                  market_open=_market_open_now(), news_hard=hard,
                                                  cal_age_h=_cal_age_h())
                if _gok:
                    _mok, _mwhy = _monitor_bias_gate(repx)
                    if not _mok:
                        _blocked_items(_mwhy); code = 'guard:' + _mwhy
                    else:
                        _batch_ok, _batch, _rb = _exec_sibling_batch(_book_items, txt)
                        if _batch_ok:
                            for _item, _res, _itxt in _batch: guardrails.note(_item, 'sent')
                            guardrails.finish_sibling_batch(_batch_group_id(_book_items), 'sent')
                            code = 'exec'
                        else:
                            _why = 'sibling_batch_rolled_back' if _rb.get('ok') else 'sibling_batch_uncertain'
                            for _item in _book_items: guardrails.note(_item, 'blocked', _why)
                            _exec_fail_alert({'batch': (_batch[-1][1] if _batch else {}), 'rollback': _rb}, 'auto-batch')
                            code = 'exec-failed'
                else:
                    _blocked_items(_gwhy); code = 'guard:' + _gwhy
        else:
            _book_items = _prepare_ab_siblings(repx)
            code=live_emit.post_webhook(txt, WEBHOOK_URL) if WEBHOOK_URL else 'no-url'
        print('ALERT', code, txt, flush=True)
        for _item in _book_items:
            _itxt = txt if _item.get('_strat', 'A/B') == 'A/B' else live_emit.to_alert(_item)
            _save_db(_item, _itxt, code)
            try: manage.register(_item, TRADES)
            except Exception as e: print('manage.register err', e, flush=True)
            try: shadow.record(_item.get('_strat', 'A/B'), _item.get('dir'), _item.get('entry'), _item.get('SL'),
                               _item.get('_exec_tp') or _item.get('TP'), _item.get('bos_ms'),
                               entry_ms=_item.get('entry_ms'), metadata=_item.get('_dol'))
            except Exception as e: print('shadow.record err', e, flush=True)
        if code in ('exec', 'exec-manual') or (WEBHOOK_URL and str(code).startswith('2')) or not WEBHOOK_URL:
            for kk in allkeys: sentn.add(kk)
        nfired+=1
    # ====== (usunięte) PRE-ALERTY — stary etap odbicia od CE „czekaj na BOS" zniesiony.
    # v10: wejście to LIMIT stawiany PO potwierdzeniu BOS, wysyłany przez to_alert powyżej.
    _save_sent(sentn)
    return {'nowe': nfired}

_barq_lock = threading.Lock()
_barq = []
_barq_running = False
_barq_state = {
    'last_result': None,
    'last_error': None,
    'last_at': None,
    'last_batch': 0,
    'queue_depth': 0,
}

def _gap_threshold_min():
    try: return float(os.environ.get('GAP_REPRIME_MIN','30'))
    except Exception: return 30.0

def _schedule_bar_work(b, now_ms, gap_min):
    """Queue expensive post-intake work so TradingView gets a fast 2xx."""
    global _barq_running
    job = {'bar': dict(b), 'now_ms': int(now_ms), 'gap_min': gap_min}
    with _barq_lock:
        _barq.append(job)
        _barq_state['queue_depth'] = len(_barq)
        if _barq_running:
            return False, len(_barq)
        _barq_running = True
    threading.Thread(target=_bar_worker_loop, daemon=True, name='bars-worker').start()
    return True, 1

def _bar_worker_loop():
    global _barq_running
    while True:
        with _barq_lock:
            jobs = list(_barq)
            _barq.clear()
            _barq_state['queue_depth'] = 0
            if not jobs:
                _barq_running = False
                _barq_state['queue_depth'] = 0
                _last.update(bar_worker_running=False, bar_worker_queue_depth=0)
                return
        try:
            res = _process_bar_jobs(jobs)
            with _barq_lock:
                _barq_state.update(last_result=res, last_error=None,
                                   last_at=dt.datetime.utcnow().isoformat(timespec='seconds'),
                                   last_batch=len(jobs), queue_depth=len(_barq))
            _last.update(bar_worker_queue_depth=len(_barq))
        except Exception as e:
            print('[bars-worker] err', e, flush=True)
            with _barq_lock:
                _barq_state.update(last_error=str(e),
                                   last_at=dt.datetime.utcnow().isoformat(timespec='seconds'),
                                   last_batch=len(jobs), queue_depth=len(_barq))
            _last.update(bar_worker_queue_depth=len(_barq))

def _process_bar_jobs(jobs):
    jobs = sorted(jobs, key=lambda j: j.get('now_ms') or 0)
    latest = jobs[-1]
    gap_job = next((j for j in jobs if j.get('gap_min') is not None and j['gap_min'] > _gap_threshold_min()), None)
    if gap_job:
        res = _process_new(gap_job['now_ms'], gap_min=gap_job['gap_min'])
        if latest['now_ms'] > gap_job['now_ms']:
            res = {'gap_reprime': res, 'latest': _process_new(latest['now_ms'], gap_min=0)}
    else:
        res = _process_new(latest['now_ms'], gap_min=0)

    for job in jobs:
        _after_bar_processed(job['bar'], job['now_ms'])

    try: shadow.refresh()                       # resolve shadow trades after the batch's fresh bars land
    except Exception as e: print('shadow.refresh err', e, flush=True)
    try: guardrails.sweep_orphans()             # cancel broker-side limits the model already wrote off
    except Exception as e: print('guard.sweep err', e, flush=True)

    _last.update(setups_seen=(res.get('nowe') if isinstance(res, dict) else None),
                 detector_at=dt.datetime.utcnow().isoformat(timespec='seconds'),
                 detector_result=res)
    print(f"[bars-worker] batch={len(jobs)} latest={latest['bar'].get('ts_event')} -> {res}", flush=True)
    return res

def _after_bar_processed(b, now_ms):
    try:                                              # sledzenie 1R/3R — nie moze ruszyc intake'u
        _hi=float(b['high']); _lo=float(b['low'])
        def _msend(m):
            print('MANAGE', m, flush=True)
            if WEBHOOK_URL: live_emit.post_webhook(m, WEBHOOK_URL)
        manage.check(_hi, _lo, now_ms, _msend, TRADES, outcomes_path=OUTCOMES)
    except Exception as e:
        print('manage.check err', e, flush=True)
    # --- M15 -> M5 A/B + shallow: local, forward-only SHADOW. ---
    try:
        _m15res = m15_shadow_strategy.on_bar(b)
        if _m15res.get('scheduled'):
            print('[m15-shadow] M5 scan scheduled', b.get('ts_event'), flush=True)
    except Exception as e:
        print('[m15-shadow] on_bar err', e, flush=True)
    # --- MNQ Continuation canonical baseline: independent detector/state, shadow only. ---
    # It receives every persisted closed bar, regardless of whether production
    # Reversal emitted, consumed, rejected, or executed anything.
    try:
        _contres = continuation_shadow.on_bar(b)
        if _contres.get('scheduled'):
            print('[continuation-shadow] scan scheduled', b.get('ts_event'), flush=True)
    except Exception as e:
        print('[continuation-shadow] on_bar err', e, flush=True)
    # --- Strategy F: przekaz bar do serwisu F (fire-and-forget; NIE wplywa na A/B) ---
    _furl = os.environ.get('STRAT_F_FORWARD_URL', '')
    if _furl and requests is not None:
        try:
            _rf = requests.post(_furl, json=b, timeout=3)
            if getattr(_rf, 'status_code', 0) == 200: _sat['F']['ok_at'] = dt.datetime.utcnow()
        except Exception: pass
    # --- Strategy C: przekaz bar do serwisu C (fire-and-forget; NIE wplywa na A/B) ---
    _curl = os.environ.get('STRAT_C_FORWARD_URL', '')
    if _curl and requests is not None:
        try:
            _rc = requests.post(_curl, json=b, timeout=3)
            if getattr(_rc, 'status_code', 0) == 200: _sat['C']['ok_at'] = dt.datetime.utcnow()
        except Exception: pass
    # --- Builder 50K: market-data fanout only, outside TradingView's request path. ---
    _burl = os.environ.get('BUILDER50_URL', '').rstrip('/')
    if (_burl and os.environ.get('BUILDER50_FORWARD_BARS', '1') == '1'
            and requests is not None):
        try:
            requests.post(_burl + '/bars', json=b, timeout=3)
        except Exception as e:
            print('[builder50] bar fanout failed:', e, flush=True)
    # Queue a read-only shadow refresh after the canonical bar work. The
    # shadow worker reads persisted bars and never blocks trade execution.
    downside_manager_shadow_v1.notify_bar()
    dol_reversal_manager_shadow_v1.notify_bar()
    try:
        _dol_live = dol_reversal_live.on_closed_bar(b)
        if _dol_live.get('events'):
            print('[dol-reversal-live]', _dol_live, flush=True)
    except Exception as e:
        print('[dol-reversal-live] on_bar err', e, flush=True)

@app.route('/bars', methods=['POST'])
def bars():
    b=request.get_json(force=True, silent=True) or {}
    if 'close' not in b: return jsonify(error='brak OHLC'), 400
    ts=str(b.get('ts_event','')).strip()
    try: now_ms=int(dt.datetime.fromisoformat(ts if ('+' in ts or 'Z' in ts) else ts+'+00:00').timestamp()*1000)
    except Exception: now_ms=int(dt.datetime.utcnow().timestamp()*1000)   # fail-safe: zawsze "teraz", strażnik nigdy nie wyłączony
    with _lock:
        _append_bar(b)
        gap_min = _feed_gap_min()
        nb=(sum(1 for _ in open(BUF))-1) if os.path.exists(BUF) else 0
        _last.update(last_bar=str(b.get('ts_event')), bars_in_buffer=nb,
                     processed_at=dt.datetime.utcnow().isoformat(timespec='seconds'))
        started, depth = _schedule_bar_work(b, now_ms, gap_min)
        _last.update(bar_worker_running=True, bar_worker_queue_depth=depth,
                     processed_at=dt.datetime.utcnow().isoformat(timespec='seconds'))
        res = {'queued': True, 'worker_started': started, 'queue_depth': depth,
               'gap_min': (round(gap_min, 1) if gap_min is not None else None)}
        print(f"[bars] {b.get('ts_event')} buf={nb} -> {res}", flush=True)
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
_VIEW_NAV = ("<div class='nav'><a href='/'>home</a><a href='/pnl'>P&amp;L</a><a href='/journal'>journal</a><a href='/candidates'>candidates</a><a href='/how'>how</a><a href='/all/trades'>all·trades</a><a href='/all/reconcile'>reconcile</a>"
 "<a href='/regime'>regime</a><a href='/status'>status</a><a href='/monitor'>monitor</a>"
 "<span style='color:#444'>&nbsp;|&nbsp;F:</span>"
 f"<a href='{_F_URL}/candidates'>F·candidates</a><a href='{_F_URL}/log'>F·log</a>"
 f"<a href='{_F_URL}/performance_f'>F·perf</a>"
 "<span style='color:#444'>&nbsp;|&nbsp;C:</span>"
 "<a href='/c'>C·dashboard</a><a href='/c/candidates'>C·candidates</a><a href='/c/performance'>C·perf</a></div>")
_TIMEKEYS = ('bos_ms','entry_ms','trig_ms','bos','ts','date','id')
_PREF = ['date','bos','time','dir','cat','model','entry','SL','T1','T2','T3','TP','stage','magnet','result','pnl','rr']

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
    with _barq_lock:
        _worker = dict(_barq_state, running=_barq_running, queue_depth=len(_barq))
    _age=_feed_age_min(); _mkt=_market_open_now()                  # v21: zdrowie feedu wprost w /status
    try: _cme=cme_calendar.status().get('note','')                 # v22: DLACZEGO rynek zamkniety (swieto/early close)
    except Exception: _cme=''
    try: _amode=guardrails.exec_mode()
    except Exception: _amode='?'
    try: _alive=guardrails.is_live()            # v26.1: auto AND webhook AND NOT halted (honest — halt makes it false)
    except Exception: _alive=(_amode=='auto' and bool(os.environ.get('EXEC_WEBHOOK')))
    _body=dict(version=VERSION, primed=_primed, archive_bars=na, **_last,
               feed_age_min=round(_age,1), market_open=_mkt, cme_note=_cme,
               feed_ok=bool(_age<=STALE_MIN or not _mkt),          # OK = swiezy LUB rynek zamkniety
               auto_mode=_amode, auto_live=_alive,                 # v26: is the AUTO executor live?
               heartbeat=HEARTBEAT, healthcheck=bool(os.environ.get('HEALTHCHECK_URL')),
               exec_cancel_after_sec=_entry_cancel_after_sec(),
               ab_shallow_enabled=ab_shallow.enabled(),
               ab_shallow_fraction=float(os.environ.get('AB_SHALLOW_FRACTION','0.25') or 0.25),
               ab_shallow_rr=float(os.environ.get('AB_SHALLOW_RR','2') or 2),
               setup_group_risk_usd=ab_shallow.setup_group_budget_usd(),
               setup_group_leg_risk_usd=ab_shallow.setup_group_leg_budget_usd(),
               setup_group_floor_reserve_usd=float(os.environ.get('SETUP_GROUP_FLOOR_RESERVE_USD','100') or 100),
               guard_sync_max_h=float(os.environ.get('GUARD_SYNC_MAX_H','24') or 24),
               ab_shallow_risk_pct=round(100.0 * ab_shallow.setup_group_leg_budget_usd() /
                                         float(os.environ.get('ACCOUNT','100000') or 100000), 3),
               ab_shallow_combined_max_risk_pct=round(100.0 * ab_shallow.setup_group_budget_usd() /
                                                       float(os.environ.get('ACCOUNT','100000') or 100000), 3),
               projected_dd_check=os.environ.get('DD_PROJECTED_RISK','1') == '1',
               day_loss_count_mode=os.environ.get('DAY_LOSS_COUNT_MODE','group'),
               bar_worker=_worker)
    if _wants_html(): return _kv_page('Status', _body)
    return jsonify(_body)

@app.route('/archive')
def archive():
    if not os.path.exists(ARCHIVE): return jsonify(error='brak archiwum jeszcze'), 404
    return send_file(ARCHIVE, mimetype='text/csv', as_attachment=True, download_name='archive.csv')

@app.route('/exectest', methods=['GET', 'POST'])
def exectest():
    """Route 2 test: wyślij PRZYKŁADOWE zlecenie do TradersPost (EXEC_WEBHOOK) + ping Telegram.
    Wymaga EXEC_TEST_SECRET (env) i ?secret=. Payload zawsze ma test=true, więc TradersPost
    zapisuje sygnał diagnostyczny, ale nie wysyła żadnego zlecenia do brokera.
    Param: ?dir=LONG&entry=29700&sl=29690"""
    sec = os.environ.get('EXEC_TEST_SECRET', '')
    supplied = request.headers.get('X-Exec-Test-Secret', '') or request.args.get('secret', '')
    if not sec or supplied != sec:
        return jsonify(error='ustaw EXEC_TEST_SECRET (env) i podaj ?secret=...'), 401
    side = request.args.get('dir', 'LONG').upper()
    entry = float(request.args.get('entry', '29700'))
    sl = float(request.args.get('sl', str(entry - 10 if side == 'LONG' else entry + 10)))
    label = request.args.get('label', '').strip()[:80]
    sample = {'dir': side, 'entry': entry, 'SL': sl,
              '_test_signal': True, '_exec_qty_override': 1,
              '_test_label': label or 'single-route-test'}
    relay = _exec_order(sample)   # -> relay /stage -> JEDNA wiadomość z przyciskami
    return jsonify(ok=True, exec_webhook_set=bool(os.environ.get('EXEC_WEBHOOK')), relay=relay,
                   ticker=os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!')),
                   sample=sample, note='status 200 + sent:true = TradersPost przyjął test; test:true oznacza brak zlecenia brokerskiego. 401/404 = zły EXEC_WEBHOOK; sent:false = błąd URL/sieci')


@app.route('/dualexectest', methods=['GET', 'POST'])
def dualexectest():
    """Run simultaneous test:true signals through the independent 100K and Builder routes.

    This endpoint belongs on the existing 100K service. It never creates broker orders.
    The 100K EXEC_TEST_SECRET authorizes the request; BUILDER50_TEST_SECRET is used only
    server-to-server and is sent in a header, never returned to the caller.
    """
    sec = os.environ.get('EXEC_TEST_SECRET', '')
    supplied = request.headers.get('X-Exec-Test-Secret', '') or request.args.get('secret', '')
    if not sec or supplied != sec:
        return jsonify(ok=False, error='auth'), 401
    builder_url = os.environ.get('BUILDER50_URL', '').strip().rstrip('/')
    builder_secret = os.environ.get('BUILDER50_TEST_SECRET', '').strip()
    if not builder_url or not builder_secret:
        return jsonify(ok=False, error='set BUILDER50_URL and BUILDER50_TEST_SECRET on the 100K service'), 503

    side = request.args.get('dir', 'LONG').upper()
    if side not in ('LONG', 'SHORT'):
        return jsonify(ok=False, error='dir must be LONG or SHORT'), 400
    try:
        entry = float(request.args.get('entry', '20000'))
        sl = float(request.args.get('sl', str(entry - 10 if side == 'LONG' else entry + 10)))
    except Exception:
        return jsonify(ok=False, error='entry and sl must be numbers'), 400
    chain_id = 'dual-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    sample = {'dir': side, 'entry': entry, 'SL': sl, '_test_signal': True,
              '_exec_qty_override': 1, '_test_label': chain_id}

    def local_test():
        return _exec_order(dict(sample))

    def builder_test():
        try:
            r = requests.post(builder_url + '/exectest', params={
                'dir': side, 'entry': entry, 'sl': sl, 'label': chain_id,
            }, headers={'X-Exec-Test-Secret': builder_secret}, timeout=15)
            try:
                body = r.json()
            except Exception:
                body = {'error': (r.text or '')[:200]}
            return {'http_status': r.status_code, 'body': body}
        except Exception as e:
            return {'http_status': None, 'body': {'error': str(e)}}

    if requests is None:
        return jsonify(ok=False, error='requests missing'), 503
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_local = pool.submit(local_test)
        f_builder = pool.submit(builder_test)
        local = f_local.result()
        builder = f_builder.result()

    builder_body = builder.get('body') or {}
    builder_relay = builder_body.get('relay') or {}
    local_route = local.get('route_id') or ''
    builder_route = builder_relay.get('route_id') or ''
    distinct = bool(local_route and builder_route and local_route != builder_route)
    local_ok = bool(local.get('sent') and local.get('status') == 200)
    builder_ok = bool(builder.get('http_status') == 200 and builder_body.get('ok')
                      and builder_relay.get('sent'))
    return jsonify(ok=bool(local_ok and builder_ok and distinct), test=True,
                   chain_id=chain_id, routes_distinct=distinct,
                   local={'label': os.environ.get('ACCOUNT_LABEL', '100K'), 'relay': local},
                   builder={'label': (builder_body.get('sample') or {}).get('_account_label', 'Builder 50K'),
                            'http_status': builder.get('http_status'), 'response': builder_body},
                   note='Both signals use test:true and quantity 1; no broker order is created. Verify chainTest under each separate TradersPost strategy.')

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

_cand_scan_lock = threading.Lock()


def _scan_ab_candidates(hours, force=False):
    """Read the trace produced by the normal per-bar detector.

    An on-demand detector run is only a startup/manual fallback.  Normal page
    refreshes are therefore cheap and cannot start a detector subprocess every
    15 seconds.
    """
    import json as _json
    tr=[]; loaded=False
    try:
        tr=_json.load(open(CAND_TRACE)); loaded=True
    except Exception:
        pass
    if force or not loaded:
        with _cand_scan_lock:
            tout='/tmp/cand_trace_on_demand.json'   # writable fallback; the live trace remains owned by _detect()
            gated = os.environ.get('REGIME_GATE','')=='1'
            det_file = os.environ.get('DET_FILE', 'det_v11.py')
            env=dict(os.environ, DATA_CSV=BUF, OUT_PKL='/tmp/cand_out.pkl',
                     CUTOFF='', DEBUG_TRACE='1', TRACE_OUT=tout)
            if gated: env['EOD_INTRADAY']='1' if _eod_flag() else ''
            try:
                _r = subprocess.run(['python3', os.path.join(HERE,det_file)], env=env, capture_output=True, timeout=600)
                if _r.returncode != 0:
                    print('[candidates] det rc=%s STDERR:\n%s' % (_r.returncode, (_r.stderr or b'').decode('utf-8','replace')[-3000:]), flush=True)
            except subprocess.TimeoutExpired:
                print('[candidates] det TIMEOUT >600s — trace not refreshed', flush=True)
            except Exception as _e:
                print('[candidates] det EXC:', _e, flush=True)
            try:
                tr=_json.load(open(tout))
                staged=CAND_TRACE + '.ondemand'
                with open(staged, 'w') as _f: _json.dump(tr, _f)
                os.replace(staged, CAND_TRACE)
            except Exception: tr=[]
    cut=int((dt.datetime.utcnow().timestamp()-hours*3600)*1000)
    rec=[r for r in tr if r.get('trig_ms',0)>=cut]
    rec.sort(key=lambda r:r.get('trig_ms',0), reverse=True)   # newest first
    try:                                                       # 🧲 magnet badge on confirmed candidates
        import magnet as _mag, sqlite3 as _sq3
        _mbuf=_mag.load_buffer(BUF)
        _mrec=[r[0] for r in _sq3.connect(DB).execute("SELECT dir FROM signals ORDER BY logged_at DESC LIMIT 5").fetchall()][::-1]
        if _mbuf:
            for _cr in rec:
                if _cr.get('stage')=='POTWIERDZONY':
                    _cm=_mag.check(_cr, _mbuf[0], _mbuf[1], _mbuf[2], _mrec)
                    if _cm['magnet']: _cr['magnet']=_cm['badge']
    except Exception as _me: print('[candidates] magnet err', _me, flush=True)
    return rec


def _candidate_trace_status():
    try:
        stamp = dt.datetime.utcfromtimestamp(os.path.getmtime(CAND_TRACE))
        updated = stamp.isoformat(timespec='seconds') + 'Z'
        age_sec = max(0, int((dt.datetime.utcnow() - stamp).total_seconds()))
    except Exception:
        updated, age_sec = None, None
    try: refresh_sec=max(5, min(60, int(os.environ.get('CANDIDATE_REFRESH_SEC', '15') or 15)))
    except Exception: refresh_sec=15
    return dict(updated_at=updated, age_sec=age_sec, last_bar=_last.get('last_bar'),
                refresh_seconds=refresh_sec, version=VERSION)


@app.route('/candidates')
def candidates():
    from collections import Counter
    hours=float(request.args.get('hours','12'))
    rec = _scan_ab_candidates(hours, force=request.args.get('refresh') == '1')
    if _wants_html():
        _summ=' · '.join("%s: %s"%(k,v) for k,v in Counter(r['stage'] for r in rec).items())
        _legend=("<div style='font-size:12px;line-height:1.7;color:#9aa6b2;border:1px solid #334;"
                 "border-radius:8px;padding:9px 12px;margin:8px 0'>"
                 "<b>Etapy — kolejność do wejścia (guide):</b><br>"
                 "1. <b>displacement OK</b> — wykryto impuls + FVG (pierwszy filtr; jeszcze nie trade)<br>"
                 "2. <b>brak setupu (odbicie/BOS)</b> — <span style='color:#c66'>UMARŁ tu</span>: brak odbicia od 50% FVG albo brak break-of-structure<br>"
                 "3. <b>setup OK (BOS)</b> — odbicie utrzymane + BOS potwierdzony (setup gotowy)<br>"
                 "4. <b style='color:#3cba7a'>POTWIERDZONY</b> — ⭐ <b>TO JEST TRADE</b>: entry / SL / TP policzone, alert wysłany. <b>Ostatni etap.</b>"
                 "</div>")
        return _page('Candidates (%gh)'%hours, _legend + "<div class='sum'>etapy: %s</div>"%_summ + _table(rec))
    return jsonify(hours=hours, liczba=len(rec),
                   podsumowanie=dict(Counter(r['stage'] for r in rec)), kandydaci=rec)


@app.route('/ab/candidates')
def ab_combined_candidates():
    """Human-readable two-leg funnel; JSON is available with Accept: application/json."""
    try: hours=max(1.0, min(168.0, float(request.args.get('hours','12'))))
    except Exception: hours=12.0
    rec = _scan_ab_candidates(hours, force=request.args.get('refresh') == '1')
    live_status = _candidate_trace_status()
    if not _wants_html():
        body = ab_candidates_view.payload(rec, hours, os.environ)
        body['live'] = live_status
        return jsonify(body)
    return ab_candidates_view.render_page(rec, hours, os.environ, live_status=live_status)

# ====== MONITOR REŻIMU (logika w regime.py — rdzen det_v10.py nietkniety) ======
def _market_context_sources():
    """Oldest -> newest; duplicate timestamps are resolved in favour of live data."""
    custom = os.environ.get('MARKET_CONTEXT_DATA', '').strip()
    paths = ([p for p in custom.split(os.pathsep) if p] if custom else []) + [SEED_CSV, ARCHIVE, BUF]
    out = []
    for path in paths:
        if path and path not in out and os.path.exists(path): out.append(path)
    return out


@app.route('/regime')
def regime():
    try: w=int(request.args.get('window','20'))
    except Exception: w=20
    import regime as _regime
    _st=_regime.regime_stats(BUF, HERE, window=w)
    if _wants_html(): return _kv_page('Reżim', _st)
    return jsonify(_st)


@app.route('/market-context')
def market_context_data():
    """Weekly regime, weekly/daily bias and causal history for /monitor."""
    try: days = max(7, min(730, int(request.args.get('days', '365'))))
    except Exception: days = 365
    try: weeks = max(4, min(260, int(request.args.get('weeks', '156'))))
    except Exception: weeks = 156
    history_file = os.path.join(DATA_DIR, market_context.HISTORY_FILE)
    body = market_context.build_report(_market_context_sources(), daily_limit=days,
                                       weekly_limit=weeks, snapshot_file=history_file,
                                       database_path=MARKET_CONTEXT_DB,
                                       news=_market_context_news(),
                                       prediction_database_path=MARKET_PREDICTIONS_DB)
    code = 200 if body.get('ok') else 503
    return jsonify(body), code


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
            try: guardrails.beat(_feed_age_min(), _market_open_now())   # v26: per-cycle liveness for /guard/health
            except Exception: pass
            try: guardrails.news_calendar_check(_cal_age_h(), _market_open_now())
            except Exception as e: print('[heartbeat] news calendar check err', e, flush=True)
            try:                                                        # holiday early-close -> move flatten + entry cutoff up
                import cme_calendar as _cmec
                _hm = _cmec.EARLY_CLOSE.get(dt.datetime.now(_cmec.CT).date())
                if _hm is not None:
                    guardrails.note_early_close(_hm + 60 - 10)          # CT->ET (+60), flatten 10 min before the halt
            except Exception: pass
            try: guardrails.eod_flatten_check(_market_open_now())       # daily flatten+cancel (def 16:04 ET; early-close aware)
            except Exception: pass
            try: guardrails.daily_digest_check()                        # ☀️ proof-of-life digest — silence = the alarm
            except Exception: pass
            try:                                                        # Sun 18:15 ET + weekdays 08:45 ET; JSONL audit only
                _written = market_context.record_if_due(_market_context_sources(), DATA_DIR,
                                                        database_path=MARKET_CONTEXT_DB,
                                                        news=_market_context_news(),
                                                        prediction_database_path=MARKET_PREDICTIONS_DB)
                if _written: print('[market_context] snapshots:', ','.join(x['kind'] for x in _written), flush=True)
            except Exception as e: print('[market_context] snapshot err', e, flush=True)
            try:
                _settled = market_context.settle_prediction_journal_if_due(
                    _market_context_sources(), MARKET_PREDICTIONS_DB,
                    market_database_path=MARKET_CONTEXT_DB)
                if _settled: print('[market_context] predictions settled:', _settled, flush=True)
            except Exception as e: print('[market_context] prediction settlement err', e, flush=True)
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
            # v25: per-satellite watch (C, F). Three real failures, one latched alert each, market hours only:
            #   (a) DOWN     — /health GET fails or non-200 (crashed / asleep / redeploy)
            #   (b) DISABLED — /health 200 but enabled=false (F 07-17: 200s on bars, produces NOTHING)
            #   (c) STARVED  — reachable+enabled but A/B's fanout stopped landing (ok_at stale => not fed)
            # /health is CACHE-BUSTED (bare endpoints are edge-cached and lie). C via internal C_URL, F via _F_URL.
            if SAT_WATCH and WEBHOOK_URL and requests is not None and _market_open_now():
                _nowu = dt.datetime.utcnow()
                _bases = {'C': os.environ.get('C_URL', '').rstrip('/'), 'F': _F_URL}
                for _name in ('C', 'F'):
                    _s = _sat[_name]; _base = _bases.get(_name) or ''
                    if not _base: continue                               # can't watch without a URL
                    _reason = None
                    try:
                        _hr = requests.get('%s/health?cb=%d' % (_base, int(_time.time())), timeout=5)
                        if _hr.status_code != 200:
                            _reason = 'serwis nie odpowiada (HTTP %s) — padł / redeploy' % _hr.status_code
                        elif not (_hr.json() or {}).get('enabled', True):
                            _reason = 'WYŁĄCZONY (enabled=false) — przyjmuje bary, ale sygnałów ZERO'
                    except Exception:
                        _reason = 'brak odpowiedzi (padł / śpi / redeploy)'
                    if _reason is None and _s['ok_at'] is not None:
                        _sage = (_nowu - _s['ok_at']).total_seconds() / 60.0
                        if _sage > SAT_STALE_MIN:
                            _reason = 'nie dostaje barów od %.0f min (A/B nie forwarduje)' % _sage
                    if _reason and not _s['alerted']:
                        _m = '⚠️ STRATEGY %s: %s' % (_name, _reason)
                        if PUBLIC_URL: _m += '\n%s/status' % PUBLIC_URL
                        try: live_emit.post_webhook(_m, WEBHOOK_URL)
                        except Exception as e: print('[heartbeat] %s post err' % _name, e, flush=True)
                        _s['alerted'] = True
                        print('[heartbeat] SAT %s PROBLEM: %s' % (_name, _reason), flush=True)
                    elif (_reason is None) and _s['alerted']:
                        try: live_emit.post_webhook('✅ STRATEGY %s — znowu OK (feed + enabled).' % _name, WEBHOOK_URL)
                        except Exception as e: print('[heartbeat] %s post err' % _name, e, flush=True)
                        _s['alerted'] = False
                        print('[heartbeat] SAT %s RECOVERED' % _name, flush=True)
        except Exception as e:
            print('[heartbeat] loop err', e, flush=True)   # never die

_init_db(); _seed_buffer()
pnl.register(app, DB, render_page=_page, wants_html=_wants_html)   # /pnl unified journal (isolated add-on)
how_ab.register(app)                        # /how — A/B explainer page (isolated add-on)
dashboard.register(app)                     # /    — unified home shell (federates existing pages, isolated add-on)
dol_dashboard.register(app, DB)              # /dol — A/B DOL diagnostics; no execution path
a_cont_both_aligned_shadow.register(app)      # /a-cont-both-aligned — shadow-only; GET routes only
dol_delivery_reversal_shadow.register(app)       # /dol-delivery-reversal — shadow-only; GET routes only
dol_reversal_manager_shadow_v1.register(app)      # /dol-reversal-manager — dedicated 58-feature shadow manager
dol_reversal_control.register(app)                 # /dol-reversal/readiness — hashes, modes and live blockers
dol_reversal_live.register(app)                    # /dol-reversal-live — webhook/local-fill lifecycle audit
continuation_shadow.register(app, archive_path=ARCHIVE)  # /continuation — independent Policy-B shadow
continuation_live.configure(_dispatch_continuation_live)
continuation_shadow.register_scan_listener(continuation_live.drain)
shadow.register(app)                        # /shadow/data + /shadow/log — live shadow-executor log (isolated add-on)
downside_manager_shadow_v1.register(app)    # /downside-shadow — frozen manager, no broker actions
m15_shadow_strategy.register(app)           # /m15/* — M15->M5 candidates + isolated shadow-only book
guardrails.register(app)                    # /guard — MFF-eval auto-exec gate + progress counter (isolated add-on)
portfolio_guard.register(app, data_dir=guardrails.DATA_DIR)  # /portfolio-guard — actual decision audit
forex_pnl.register(app)                     # /forexpnl - joined forex P&L (isolated add-on)
fxguard.register(app)                       # /fxguard - joined forex Auto-Executor (isolated add-on)
allview.register(app)                       # /all/trades + /all/candidates - joined view (isolated add-on)

@app.route('/continuation/live')
def _continuation_live_status():
    """Read-only status/decision ledger; secrets and webhook URLs are never exposed."""
    return jsonify(status=continuation_live.status(), decisions=continuation_live.rows(200))

@app.route('/continuation/live/dashboard')
def _continuation_live_dashboard():
    """Human-readable account dispatch table with explicit strategy classes."""
    from flask import Response
    return Response(r'''<!doctype html><meta charset="utf-8"><title>Continuation LIVE</title>
<style>*{box-sizing:border-box}body{margin:0;padding:18px;background:#0b0e14;color:#e6e9ef;font:13px system-ui}.cards{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.card{background:#111827;border:1px solid #263248;border-radius:9px;padding:10px 14px;min-width:145px}.mut{color:#94a3b8}.ok{color:#4ade80}.bad{color:#f87171}.wrap{overflow:auto;border:1px solid #263248;border-radius:10px}table{border-collapse:collapse;width:100%;font:12px ui-monospace,monospace}th,td{padding:8px 9px;border-bottom:1px solid #202b40;text-align:left;white-space:nowrap}th{color:#94a3b8;background:#111827;position:sticky;top:0}</style>
<h2>Continuation + A/B Directional · LIVE dispatch</h2><div class="mut">A/B Directional ma osobną klasę FIXED 2R i osobne przełączniki LONG/SHORT. Każdy wariant przechodzi przez ten sam account-local Guard.</div><div id="cards" class="cards"></div><div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<script>const C=['activation_ms','classification','setup_class','strategy','direction','state','guard_reason','account_label','quantity','route_id','order_id'];const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function load(){fetch('/continuation/live',{cache:'no-store'}).then(r=>r.json()).then(x=>{const s=x.status||{},d=x.decisions||[];cards.innerHTML=[['Continuation LONG',s.long_enabled],['Continuation SHORT',s.short_enabled],['A/B Dir LONG',s.ab_directional_long_enabled],['A/B Dir SHORT',s.ab_directional_short_enabled],['Dispatcher',s.dispatcher_ready],['Armed after',s.armed_after_ms],['Counts',JSON.stringify(s.counts||{})]].map(v=>'<div class="card"><b>'+esc(v[0])+'</b><br><span class="'+(v[1]===false?'bad':'ok')+'">'+esc(v[1])+'</span></div>').join('');head.innerHTML=C.map(k=>'<th>'+esc(k)+'</th>').join('');body.innerHTML=d.map(r=>'<tr>'+C.map(k=>'<td>'+esc(k==='activation_ms'&&r[k]?new Date(r[k]).toISOString():r[k])+'</td>').join('')+'</tr>').join('')||'<tr><td colspan="11" class="mut">Brak nowych decyzji LIVE. Pierwszy skan tylko uzbraja adapter i nie wysyła historii.</td></tr>'}).catch(e=>{body.innerHTML='<tr><td class="bad">'+esc(e)+'</td></tr>'})}load();setInterval(load,15000)</script>''',mimetype='text/html')

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
