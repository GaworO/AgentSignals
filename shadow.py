#!/usr/bin/env python3
"""
shadow.py - LIVE shadow-executor log (isolated add-on, same pattern as pnl.py / dashboard.py).

Records EVERY fresh signal hands-off (no money, no order), then resolves its outcome from the live
bar buffer - resting limit, 240-bar fill window, fixed 2R stop, 1-tick slippage. This is the Gate-0
evidence dataset: prove >= +0.15R over 30-50 logged fills before real size.

Excludes London + Asia by ET clock (asleep / failed the 3-yr regime test). Keeps all weekdays.
$100k @ 0.5% => $500 = 1R.

Wire into agent.py (next to dashboard.register):   import shadow ; shadow.register(app)
Then log A/B signals:   shadow.record('A/B', x['dir'], x['entry'], x['SL'], x['TP'], x['bos_ms'])
Model C / Strategy F services POST to /shadow/log (set SHADOW_URL on those services).

Env: DATA_DIR (persist dir, def '.'), SHADOW_BUF or BUF (live bar CSV for outcome resolution).
"""
import os, json, datetime as dt
try:
    from zoneinfo import ZoneInfo; _NY = ZoneInfo('America/New_York')
except Exception:
    _NY = None

DATA_DIR   = os.environ.get('DATA_DIR', '.')
LOG        = os.path.join(DATA_DIR, 'shadow_log.json')
SHADOW_BUF = os.environ.get('SHADOW_BUF') or os.environ.get('BUF') or os.path.join(DATA_DIR, 'archive.csv')  # agent's full bar history (never trimmed); falls back to buffer.csv
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
                        entry=entry, sl=sl, tp=tp, ms=ms, outcome='open', R=None, net=None))
        _save(log); return True
    except Exception as e:
        print('[shadow] record err', e, flush=True); return False

def _bars():
    import pandas as pd, numpy as np
    path = SHADOW_BUF if os.path.exists(SHADOW_BUF) else os.path.join(DATA_DIR, 'buffer.csv')
    df = pd.read_csv(path)
    _tail = int(os.environ.get('SHADOW_TAIL', '8000'))
    if len(df) > _tail: df = df.tail(_tail).reset_index(drop=True)
    tcol = df.columns[0]
    ms = (pd.to_datetime(df[tcol], utc=True).astype('int64') // 10**6).to_numpy()
    low = {c.lower(): c for c in df.columns}
    hi = df[low.get('high', 'high')].to_numpy(float)
    lo = df[low.get('low', 'low')].to_numpy(float)
    return ms, hi, lo

def refresh():
    """Resolve open shadow trades against the live bar buffer. Safe if buffer missing (stays open)."""
    import numpy as np
    log = _load()
    if not any(t.get('outcome') == 'open' for t in log): return log
    try: MS, HI, LO = _bars()
    except Exception as _e:
        print('[shadow] _bars err - open trades stay open:', _e, flush=True); return log
    N = len(MS); changed = False
    for t in log:
        if t.get('outcome') != 'open': continue
        bull = t['dir'] == 'LONG'; e = t['entry']; sl = t['sl']; tp = t['tp']; R = abs(e - sl)
        if R <= 0: continue
        sb = int(np.searchsorted(MS, t['ms'], side='left')); fb = None
        for i in range(sb, min(sb + 240, N)):
            if LO[i] <= e <= HI[i]: fb = i; break
        if fb is None:
            if N > sb + 240: t['outcome'] = 'no_fill'; changed = True
            continue
        if (HI[fb] - LO[fb]) > 20:                       # fast bar -> resting limit missed
            t['outcome'] = 'missed'; changed = True; continue
        win = None
        for i in range(fb, min(fb + 2880, N)):
            hsl = (LO[i] <= sl) if bull else (HI[i] >= sl)
            htp = (HI[i] >= tp) if bull else (LO[i] <= tp)
            if hsl: win = False; break
            if htp: win = True;  break
        if win is None:
            if N >= fb + 2880:                            # full 2-day window elapsed, chopped -> scratch (don't hang open forever)
                t['outcome'] = 'timeout'; t['R'] = 0.0; t['net'] = 0; changed = True
            continue                                      # else still genuinely running
        ct = max(1, round(RISK / (R * PV))); cost = ct * (COMM * 2 + 0.25 * PV + 0.25 * PV)
        net = (2.0 if win else -1.0) * RISK - cost
        t['outcome'] = 'win' if win else 'loss'; t['R'] = round(net / RISK, 3); t['net'] = round(net)
        changed = True
    if changed: _save(log)
    return log

def register(app):
    """Adds /shadow/data (GET, live log JSON) and /shadow/log (POST, ingest from C/F services)."""
    try: from flask import request, jsonify
    except Exception: return app
    def _data():
        log = refresh()
        out = [t for t in log if t.get('outcome') in ('win', 'loss', 'open', 'timeout')]  # filled + still-open; hide missed/no_fill
        return jsonify(out)
    def _post():
        d = request.get_json(force=True, silent=True) or {}
        ok = record(d.get('strategy'), d.get('dir'), d.get('entry'), d.get('sl'),
                    d.get('tp'), d.get('ms'), d.get('sess'))
        return jsonify(ok=bool(ok))
    def _health():
        import collections
        log = _load(); cc = collections.Counter(t.get('outcome') for t in log)
        ap = SHADOW_BUF if os.path.exists(SHADOW_BUF) else os.path.join(DATA_DIR, 'buffer.csv')
        nb = -1
        try:
            import pandas as pd; nb = int(len(pd.read_csv(ap)))
        except Exception: pass
        return jsonify(total=len(log), counts=dict(cc), archive=ap, archive_exists=os.path.exists(ap), archive_bars=nb)
    app.add_url_rule('/shadow/health', 'shadow_health', _health)
    app.add_url_rule('/shadow/data', 'shadow_data', _data)
    app.add_url_rule('/shadow/log', 'shadow_log', _post, methods=['POST'])
    return app
