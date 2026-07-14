#!/usr/bin/env python3
"""
shadow.py - LIVE shadow-executor log (isolated add-on, same pattern as pnl.py / dashboard.py).

Records EVERY fresh signal hands-off (no money, no order), then resolves its outcome from the live
bar buffer - resting limit, 240-bar fill window, 1-tick slippage. This is the Gate-0 evidence
dataset: prove >= +0.15R over 30-50 logged fills before real size.

Each filled trade is scored under BOTH exit rules in one bar pass:
  * FIXED  : stop stays at SL, target 2R           -> R_fixed / outcome_fixed   (the VALIDATED edge,
             4yr backtest +0.298R; this is what the Gate-0 KPI reports)
  * BE@1R  : after +1R the stop moves to entry      -> R / outcome (the per-trade view Aleks asked
             for: WIN 2R / BE 0R / LOSS -1R, mirrors forex manage.py; 4yr +0.146R, shown for context)

Excludes London + Asia by ET clock (asleep / failed the 3-yr regime test). Keeps all weekdays.
$100k @ 0.5% => $500 = 1R.

Wire into agent.py (next to dashboard.register):   import shadow ; shadow.register(app)
Then log A/B signals:   shadow.record('A/B', x['dir'], x['entry'], x['SL'], x['TP'], x['bos_ms'])
Model C / Strategy F services POST to /shadow/log (set SHADOW_URL on those services).

Backfill: a bundled shadow_seed.json (historical A/B, pre-scored) is merged into the live log once on
startup, tagged src='backfill' so it is visibly distinct from live-recorded fills and never re-resolved.

Env: DATA_DIR (persist dir, def '.'), SHADOW_BUF or BUF (live bar CSV for outcome resolution),
     SHADOW_SEED (backfill file, def <module dir>/shadow_seed.json), SHADOW_STALE_DAYS (def 3).
"""
import os, json, datetime as dt
try:
    from zoneinfo import ZoneInfo; _NY = ZoneInfo('America/New_York')
except Exception:
    _NY = None

HERE       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get('DATA_DIR', '.')
LOG        = os.path.join(DATA_DIR, 'shadow_log.json')
SHADOW_BUF = os.environ.get('SHADOW_BUF') or os.environ.get('BUF') or os.path.join(DATA_DIR, 'archive.csv')  # agent's full bar history (never trimmed); falls back to buffer.csv
SEED_FILE  = os.environ.get('SHADOW_SEED', os.path.join(HERE, 'shadow_seed.json'))
RISK, PV, COMM = 500.0, 2.0, 0.62
EXCLUDE = {'ASIA', 'LO'}          # London + Asia never logged

def _et(ms):
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
    return d.astimezone(_NY) if _NY else d

def _sess(et):
    m = et.hour * 60 + et.minute
    if m >= 18 * 60 or m < 2 * 60: return 'ASIA'
    if m < 5 * 60:                 return 'LO'
    if m < 9 * 60 + 30:            return 'PREM'
    if m < 11 * 60:                return 'NYAM'
    if m < 13 * 60 + 30:           return 'NYL'
    if m < 16 * 60:                return 'NYPM'
    return 'PM_AH'

def _load():
    try: return json.load(open(LOG))
    except Exception: return []

def _save(x):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(x, open(LOG, 'w'))
    except Exception as e:
        print('[shadow] save err', e, flush=True)

def _key(strategy, dirn, ms, entry):
    return "%s|%s|%d|%.2f" % (strategy, dirn, int(ms), round(float(entry), 2))

def _costed(R, gross_R):
    """contracts + net$ + net-R for a gross-R outcome (costs: 2 commissions + 1 tick each side)."""
    ct = max(1, round(RISK / (R * PV)))
    cost = ct * (COMM * 2 + 0.25 * PV + 0.25 * PV)
    net = gross_R * RISK - cost
    return ct, round(net), round(net / RISK, 3)

def record(strategy, dirn, entry, sl, tp=None, ms=None, sess=None):
    """Log ONE fresh signal hands-off. Dedups. Skips London/Asia. tp defaults to 2R."""
    try:
        ms = int(ms if ms is not None else dt.datetime.utcnow().timestamp() * 1000)
        et = _et(ms); s = sess if sess in ('PREM','NYAM','NYL','NYPM','PM_AH','ASIA','LO') else _sess(et)
        if s in EXCLUDE: return False
        entry = round(float(entry), 2); sl = round(float(sl), 2); R = abs(entry - sl)
        if tp is None: tp = entry + 2 * R if dirn == 'LONG' else entry - 2 * R
        tp = round(float(tp), 2)
        log = _load(); k = _key(strategy, dirn, ms, entry)
        if any(t.get('key') == k for t in log): return False
        wk = (et - dt.timedelta(days=et.weekday())).strftime('%Y-%m-%d')
        log.append(dict(key=k, strategy=strategy, dir=dirn, sess=s, week=wk, dow=et.strftime('%a'),
                        et=et.strftime('%Y-%m-%d %H:%M'), date=et.strftime('%Y-%m-%d'),
                        entry=entry, sl=sl, tp=tp, ms=ms, src='live',
                        outcome='open', R=None, net=None, outcome_fixed='open', R_fixed=None, net_fixed=None))
        _save(log); return True
    except Exception as e:
        print('[shadow] record err', e, flush=True); return False

def _bars(since_ms=None):
    """Return (ms, high, low) arrays from the archive. If since_ms is given, slice the
    archive from just before that timestamp so EVERY open trade is fully covered - a blind
    tail(N) could stop short of an older open trade and leave it unresolvable (hung 'open').

    Robust parse: a stray duplicate header row or a blank/partial bar (both happen on the live
    /data archive across redeploys/feed gaps) used to make pd.to_datetime raise -> refresh()
    swallowed it and EVERY trade hung 'open' forever with no R. Now unparseable rows are coerced
    to NaT and dropped, keeping the OHLC arrays aligned."""
    import pandas as pd, numpy as np
    path = SHADOW_BUF if os.path.exists(SHADOW_BUF) else os.path.join(DATA_DIR, 'buffer.csv')
    df = pd.read_csv(path)
    tcol = df.columns[0]
    ts = pd.to_datetime(df[tcol], utc=True, errors='coerce', format='ISO8601')
    ok = ts.notna().to_numpy()
    if not ok.all():                                   # drop stray header / blank / partial rows
        df = df.loc[ok].reset_index(drop=True); ts = ts[ok]
    ms = (ts.astype('int64') // 10**6).to_numpy()
    if since_ms is not None and len(ms):
        start = max(0, int(np.searchsorted(ms, int(since_ms), side='left')) - 5)  # tiny pad before oldest open trade
        df = df.iloc[start:].reset_index(drop=True); ms = ms[start:]
    else:                                          # no open trades to anchor on -> keep a generous tail
        _tail = int(os.environ.get('SHADOW_TAIL', '20000'))
        if len(df) > _tail: df = df.tail(_tail).reset_index(drop=True); ms = ms[-_tail:]
    low = {c.lower(): c for c in df.columns}
    hi = pd.to_numeric(df[low.get('high', 'high')], errors='coerce').to_numpy(float)  # a non-numeric OHLC cell -> NaN, never a crash
    lo = pd.to_numeric(df[low.get('low', 'low')], errors='coerce').to_numpy(float)
    return ms, hi, lo

# ---------------------------------------------------------------------------
# Shared scorer - used by BOTH live refresh() and the backfill builder, so a
# live trade and a historical one are scored by identical rules.
# ---------------------------------------------------------------------------
def score(dirn, entry, sl, tp, ms, MS, HI, LO, fill_bars=240, hold_bars=2880):
    """Resolve one trade against bar arrays under FIXED and BE@1R in a single pass.

    Returns a dict of outcome/R fields, or {'outcome':'open'/'no_fill'/'missed'} when unresolved.
    Adverse-first within every bar (conservative), mirroring forex manage.py."""
    import numpy as np
    bull = dirn == 'LONG'; e = float(entry); sl = float(sl); tp = float(tp); R = abs(e - sl)
    N = len(MS)
    if R <= 0 or N == 0:
        return {'outcome': 'open'}
    if int(ms) < int(MS[0]) - 60000 or int(ms) > int(MS[-1]):
        return {'outcome': 'out_of_range'}
    sb = max(0, int(np.searchsorted(MS, int(ms), side='right')) - 1)   # include the bar the signal fired in
    # ---- FILL GATE: resting limit at entry within fill_bars ----
    fb = None
    for i in range(sb, min(sb + fill_bars, N)):
        if LO[i] <= e <= HI[i]: fb = i; break
    if fb is None:
        return {'outcome': 'no_fill'} if N > sb + fill_bars else {'outcome': 'open'}
    if (HI[fb] - LO[fb]) > 20:                        # fast bar -> resting limit missed
        return {'outcome': 'missed'}
    r1 = e + R if bull else e - R                     # +1R level (arms BE)
    fixed = None; be = None; armed = False
    for i in range(fb, min(fb + hold_bars, N)):
        h = HI[i]; l = LO[i]
        hit_sl = (l <= sl) if bull else (h >= sl)
        hit_tp = (h >= tp) if bull else (l <= tp)
        hit_1r = (h >= r1) if bull else (l <= r1)
        hit_e  = (l <= e)  if bull else (h >= e)
        # FIXED bracket (adverse-first)
        if fixed is None:
            if hit_sl:   fixed = -1.0
            elif hit_tp: fixed = 2.0
        # BE bracket (adverse-first; SL until armed, then entry-stop)
        if be is None:
            if not armed:
                if hit_sl:   be = -1.0
                elif hit_1r: armed = True            # move stop to BE; targets checked next bars
            else:
                if hit_e:    be = 0.0                # returned to entry after arming -> break-even
                elif hit_tp: be = 2.0
        if fixed is not None and be is not None: break
    if fixed is None and be is None:
        return {'outcome': 'open'}                    # still running / not enough bars
    # If one leg resolved but the other ran out of bars, fall back to timeout(0) for the open leg.
    if fixed is None: fixed = 0.0
    if be is None:    be = 0.0
    ct, net_fx, Rn_fx = _costed(R, fixed)
    _,  net_be, Rn_be = _costed(R, be)
    oc_fx = 'win' if fixed > 0 else ('loss' if fixed < 0 else 'timeout')
    oc_be = 'win' if be > 1.5 else ('loss' if be < -0.5 else ('be' if abs(be) < 0.5 else 'timeout'))
    return {'ct': ct,
            'outcome_fixed': oc_fx, 'R_fixed': Rn_fx, 'net_fixed': net_fx,
            'outcome': oc_be, 'R': Rn_be, 'net': net_be}

def refresh():
    """Resolve open shadow trades against the live bar buffer. Safe if buffer missing (stays open).
    Backfilled rows (src='backfill') are already scored offline and are never touched here."""
    import time as _time
    log = _load()
    open_ms = [int(t['ms']) for t in log
               if t.get('outcome') == 'open' and t.get('src') in (None, 'live') and isinstance(t.get('ms'), (int, float))]
    if not open_ms: return log
    try: MS, HI, LO = _bars(since_ms=min(open_ms))    # anchor read on the oldest open trade -> always covered
    except Exception as _e:
        print('[shadow] _bars err - open trades stay open:', _e, flush=True); return log
    changed = False
    now_ms = int(_time.time() * 1000)
    STALE_MS = int(os.environ.get('SHADOW_STALE_DAYS', '3')) * 86400000
    for t in log:
        if t.get('outcome') != 'open' or t.get('src') not in (None, 'live'): continue   # history (backtest/live-hist) is pre-scored
        stale = (now_ms - int(t['ms'])) > STALE_MS
        res = score(t['dir'], t['entry'], t['sl'], t['tp'], t['ms'], MS, HI, LO)
        oc = res.get('outcome')
        if oc == 'open':
            if stale: t['outcome'] = 'expired'; changed = True   # bars never arrived / not enough -> don't hang forever
            continue
        if oc == 'out_of_range':
            if stale: t['outcome'] = 'expired'; changed = True   # archive doesn't span this signal
            continue
        if oc in ('no_fill', 'missed'):
            t['outcome'] = oc; changed = True; continue
        t.update(res); changed = True                            # win / be / loss / timeout -> both R sets stored
    if changed: _save(log)
    return log

def _backfill_seed():
    """Merge the bundled historical seed into the live log ONCE (idempotent by key).
    Tagged src='backfill' so the dashboard shows it distinctly and refresh() never re-scores it."""
    try:
        if not os.path.exists(SEED_FILE): return
        seed = json.load(open(SEED_FILE))
        log = _load(); have = {t.get('key') for t in log}
        add = [s for s in seed if s.get('key') not in have]
        if not add: return
        for s in add: s.setdefault('src', 'backtest')   # preserve backtest / live-hist tags from the seed
        _save(log + add)
        print('[shadow] backfilled %d historical trades' % len(add), flush=True)
    except Exception as e:
        print('[shadow] backfill err', e, flush=True)

def register(app):
    """Adds /shadow/data (GET, live log JSON), /shadow/log (POST, ingest), /shadow/health (GET)."""
    _backfill_seed()
    try: from flask import request, jsonify
    except Exception: return app
    def _data():
        log = refresh()
        # Show EVERYTHING with a key (incl. no_fill / missed / expired, greyed by the UI) so trades
        # never silently vanish - the old filter hid them and made the tab look empty.
        out = [t for t in log if t.get('key')]
        return jsonify(out)
    def _post():
        d = request.get_json(force=True, silent=True) or {}
        ok = record(d.get('strategy'), d.get('dir'), d.get('entry'), d.get('sl'),
                    d.get('tp'), d.get('ms'), d.get('sess'))
        return jsonify(ok=bool(ok))
    def _health():
        import collections
        log = _load()
        cc = collections.Counter(t.get('outcome') for t in log)
        sc = collections.Counter(t.get('src', 'live') for t in log)
        ap = SHADOW_BUF if os.path.exists(SHADOW_BUF) else os.path.join(DATA_DIR, 'buffer.csv')
        nb = -1
        try:
            import pandas as pd; nb = int(len(pd.read_csv(ap)))
        except Exception: pass
        return jsonify(total=len(log), counts=dict(cc), by_src=dict(sc),
                       archive=ap, archive_exists=os.path.exists(ap), archive_bars=nb)
    app.add_url_rule('/shadow/health', 'shadow_health', _health)
    app.add_url_rule('/shadow/data', 'shadow_data', _data)
    app.add_url_rule('/shadow/log', 'shadow_log', _post, methods=['POST'])
    return app
