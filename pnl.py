"""
pnl.py  —  UNIFIED P&L JOURNAL (isolated add-on; does NOT touch intake / detector / signals logic).

Why this exists: A/B, C, F, ORB (+ PREM/S1/S2 backtests) each track *modeled* R on their own
service. Nothing recorded (a) whether Aleks actually TOOK a trade, or (b) her REAL broker fill.
This module adds ONE new table `fills` to journal.db and a /pnl dashboard where she logs real
trades (by hand or by importing a broker CSV), tags Taken yes/no, and sees a cross-strategy P&L
summary. The whole DB is downloadable for future reference.

Wire-up (2 lines in agent.py, nothing else):
    import pnl
    pnl.register(app, DB, render_page=_page, wants_html=_wants_html)

Routes added:
    GET  /pnl            dashboard (HTML) / full JSON  (Accept: application/json)
    POST /pnl/save       add or update a trade (form)
    POST /pnl/del/<id>   delete a trade
    POST /pnl/toggle/<id> flip Taken yes/no
    POST /pnl/import     upload a broker CSV (auto-maps columns; all rows tagged with chosen strategy)
    GET  /pnl.csv        download all fills as CSV
    GET  /pnl.db         download the whole journal.db (SQLite) for future reference
"""
import os, csv, io, json, sqlite3, datetime as dt

STRATEGIES = ['A/B', 'C', 'F', 'ORB', 'Other']   # PREM is a catalyst inside A/B, not a separate strategy

# Backtest reference — the edge your LIVE numbers should track. COMPUTED from the project's own
# 4-yr trade logs (not memory): ab_trades.csv, modelC_trades.csv, orb_trades.csv (real_R),
# prem_trades_4y.csv (net) / PREM_ANALYSIS headline, strategy_f results (net F.P. first-touch),
# s1_trades.csv, s2_trades.csv. EDIT if you re-run with a different config.
BACKTEST_REF = {
    'A/B':  {'exp_r': 0.195, 'win_pct': 31.2, 'pf': 1.49, 'n': 6569},  # ab_trades.csv, all rows (PREM catalyst included)
    'C':    {'exp_r': 0.679, 'win_pct': 50.0, 'pf': 3.28, 'n': 56},    # modelC_trades.csv (net)
    'F':    {'exp_r': 0.125, 'win_pct': 27.0, 'pf': 1.31, 'n': 378},   # F.P. first-touch, NET (realistic)
    'ORB':  {'exp_r': 0.227, 'win_pct': 46.4, 'pf': 1.46, 'n': 491},   # orb_trades.csv, real_R (realistic)
}
_COLS = ['id', 'logged_at', 'date', 'time', 'strategy', 'setup', 'side', 'taken',
         'entry', 'exit', 'size', 'risk_usd', 'pnl_usd', 'pnl_r', 'result', 'fees',
         'signal_key', 'notes']


def init(DB):
    c = sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at TEXT, date TEXT, time TEXT, strategy TEXT, setup TEXT, side TEXT,
        taken INTEGER DEFAULT 1, entry REAL, exit REAL, size REAL, risk_usd REAL,
        pnl_usd REAL, pnl_r REAL, result TEXT, fees REAL, signal_key TEXT, notes TEXT)''')
    # alerts = which strategy FIRED an alert (fed by /pnl/fire from each strategy service or the relay)
    c.execute('''CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, strategy TEXT, date TEXT, time TEXT, side TEXT, setup TEXT,
        entry REAL, sl REAL, tp REAL, key TEXT, text TEXT)''')
    c.commit(); c.close()


# ---------- helpers ----------
def _f(v):
    try:
        if v in (None, ''):
            return None
        s = str(v).strip().replace(',', '').replace('$', '').replace(' ', '')
        neg = s.startswith('(') and s.endswith(')')   # broker notation: (438.00) = -438.00
        s = s.strip('()')
        val = float(s)
        return -val if neg else val
    except Exception:
        return None


def _parse_dt(s):
    """Return (YYYY-MM-DD, HH:MM) from common broker timestamp formats. Falls back to raw string."""
    s = (s or '').strip()
    for fmt in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                '%m/%d/%Y %I:%M:%S %p', '%d/%m/%Y %H:%M:%S'):
        try:
            d = dt.datetime.strptime(s, fmt); return d.strftime('%Y-%m-%d'), d.strftime('%H:%M')
        except Exception:
            pass
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return dt.datetime.strptime(s, fmt).strftime('%Y-%m-%d'), ''
        except Exception:
            pass
    return s, ''


def _classify(pnl):
    if pnl is None:
        return 'open'
    if pnl > 0:
        return 'win'
    if pnl < 0:
        return 'loss'
    return 'be'


def _row_from_form(form):
    entry, exit_ = _f(form.get('entry')), _f(form.get('exit'))
    size = _f(form.get('size'))
    risk = _f(form.get('risk_usd'))
    fees = _f(form.get('fees')) or 0.0
    pnl = _f(form.get('pnl_usd'))
    # if she gave entry/exit/size but no $ P&L, and a point-value, leave it — brokers give $ directly.
    r = _f(form.get('pnl_r'))
    if r is None and pnl is not None and risk:
        r = round(pnl / risk, 3)
    result = form.get('result', '').strip() or _classify(pnl)
    taken = 1 if form.get('taken', 'on') in ('on', '1', 'yes', 'true', 'Y') else 0
    return dict(
        date=form.get('date', '').strip(), time=form.get('time', '').strip(),
        strategy=form.get('strategy', 'Other').strip(), setup=form.get('setup', '').strip(),
        side=(form.get('side', '') or '').upper().strip(), taken=taken,
        entry=entry, exit=exit_, size=size, risk_usd=risk, pnl_usd=pnl, pnl_r=r,
        result=result, fees=fees, signal_key=form.get('signal_key', '').strip(),
        notes=form.get('notes', '').strip())


def _insert(DB, d):
    d = dict(d)
    d['logged_at'] = dt.datetime.utcnow().isoformat(timespec='seconds')
    cols = [k for k in _COLS if k != 'id' and k in d]
    c = sqlite3.connect(DB)
    c.execute('INSERT INTO fills(%s) VALUES (%s)' % (','.join(cols), ','.join('?' * len(cols))),
              [d.get(k) for k in cols])
    c.commit(); c.close()


def _update(DB, rid, d):
    cols = [k for k in _COLS if k not in ('id', 'logged_at') and k in d]
    c = sqlite3.connect(DB)
    c.execute('UPDATE fills SET %s WHERE id=?' % ','.join('%s=?' % k for k in cols),
              [d.get(k) for k in cols] + [rid])
    c.commit(); c.close()


def _all(DB):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute('SELECT * FROM fills ORDER BY date DESC, id DESC')]
    c.close()
    return rows


def _recent_signals(DB, n=15):
    """Recent alerts the agent fired — so she can one-click 'log this' with real fill. Read-only."""
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            'SELECT date,dir,cat,model,entry,SL,TP FROM signals ORDER BY logged_at DESC LIMIT ?', (n,))]
        c.close()
        return rows
    except Exception:
        return []


def _log_alert(DB, d):
    d = dict(d); d.setdefault('ts', dt.datetime.utcnow().isoformat(timespec='seconds'))
    cols = ['ts', 'strategy', 'date', 'time', 'side', 'setup', 'entry', 'sl', 'tp', 'key', 'text']
    c = sqlite3.connect(DB)
    c.execute('INSERT INTO alerts(%s) VALUES (%s)' % (','.join(cols), ','.join('?' * len(cols))),
              [d.get(k) for k in cols])
    c.commit(); c.close()


def _recent_alerts(DB, n=25):
    """Unified 'which strategy fired' feed: alerts table (F/ORB/C/… via /pnl/fire) + A/B signals
    from the existing signals table, tagged A/B. Sorted newest first. Read-only."""
    out = []
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        for r in c.execute('SELECT * FROM alerts ORDER BY ts DESC LIMIT ?', (n,)):
            r = dict(r)
            out.append(dict(strategy=r.get('strategy') or 'Other', date=r.get('date') or '',
                            time=r.get('time') or '', side=r.get('side') or '', setup=r.get('setup') or '',
                            entry=r.get('entry'), ts=r.get('ts') or ''))
        c.close()
    except Exception:
        pass
    for s in _recent_signals(DB, n):
        out.append(dict(strategy='A/B', date=s.get('date') or '', time='', side=s.get('dir') or '',
                        setup=s.get('cat') or '', entry=s.get('entry'), ts=s.get('date') or ''))
    out.sort(key=lambda x: (x.get('ts') or '', x.get('date') or ''), reverse=True)
    return out[:n]


def _alert_counts(DB):
    try:
        c = sqlite3.connect(DB)
        rows = c.execute('SELECT strategy, COUNT(*) FROM alerts GROUP BY strategy').fetchall()
        c.close()
        d = {(s or 'Other'): n for s, n in rows}
    except Exception:
        d = {}
    try:  # add A/B from signals table
        c = sqlite3.connect(DB)
        ab = c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        c.close()
        if ab:
            d['A/B'] = d.get('A/B', 0) + ab
    except Exception:
        pass
    return d


# ---------- stats ----------
def _stats(rows):
    taken = [r for r in rows if r.get('taken')]
    resolved = [r for r in taken if (r.get('result') or 'open') != 'open']
    pnls = [r['pnl_usd'] for r in taken if r.get('pnl_usd') is not None]
    rs = [r['pnl_r'] for r in taken if r.get('pnl_r') is not None]
    wins = [r for r in resolved if (r.get('result') == 'win')]
    def _sum(a): return round(sum(a), 2) if a else 0.0
    overall = dict(
        n_taken=len(taken), n_resolved=len(resolved), n_open=len(taken) - len(resolved),
        wins=len(wins), losses=sum(1 for r in resolved if r.get('result') == 'loss'),
        be=sum(1 for r in resolved if r.get('result') == 'be'),
        win_pct=round(100 * len(wins) / len(resolved), 1) if resolved else 0.0,
        total_usd=_sum(pnls), total_R=round(sum(rs), 2) if rs else 0.0,
        avg_usd=round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        best_usd=round(max(pnls), 2) if pnls else 0.0, worst_usd=round(min(pnls), 2) if pnls else 0.0,
        n_signals_not_taken=sum(1 for r in rows if not r.get('taken')))
    per = {}
    for s in STRATEGIES:
        srows = [r for r in taken if r.get('strategy') == s]
        if not srows:
            continue
        sres = [r for r in srows if (r.get('result') or 'open') != 'open']
        sw = sum(1 for r in sres if r.get('result') == 'win')
        sp = [r['pnl_usd'] for r in srows if r.get('pnl_usd') is not None]
        sr = [r['pnl_r'] for r in srows if r.get('pnl_r') is not None]
        gross_w = sum(x for x in sp if x > 0)
        gross_l = -sum(x for x in sp if x < 0)
        pf = (gross_w / gross_l) if gross_l > 0 else (None if gross_w == 0 else float('inf'))
        per[s] = dict(n=len(srows), resolved=len(sres), win_pct=round(100 * sw / len(sres), 1) if sres else 0.0,
                      total_usd=_sum(sp), total_R=round(sum(sr), 2) if sr else 0.0,
                      exp_r=round(sum(sr) / len(sr), 3) if sr else None,
                      avg_usd=round(sum(sp) / len(sp), 2) if sp else 0.0,
                      pf=(round(pf, 2) if (pf is not None and pf != float('inf')) else pf))
    return overall, per


# ---------- broker CSV import ----------
_MAP = {
    'date': ['date', 'trade date', 'exit time', 'close time', 'closed', 'time', 'opened', 'fill time', 'exit date'],
    'side': ['side', 'direction', 'type', 'b/s', 'buy/sell', 'action', 'position'],
    'entry': ['entry', 'entry price', 'avg entry', 'buy price', 'price in', 'open price', 'avg. entry price'],
    'exit': ['exit', 'exit price', 'avg exit', 'sell price', 'price out', 'close price', 'avg. exit price'],
    'size': ['qty', 'quantity', 'size', 'contracts', 'volume', 'filled qty'],
    'pnl_usd': ['pnl', 'p/l', 'p&l', 'profit', 'net pnl', 'realized pnl', 'profit/loss', 'net p&l',
                'gross p&l', 'net profit', 'realized p/l', 'pnl (usd)', 'realized'],
    'fees': ['fees', 'commission', 'comm', 'commissions'],
    'setup': ['setup', 'note', 'notes', 'tag', 'strategy', 'comment', 'symbol', 'ticker', 'contract', 'instrument'],
}


def _match(header):
    hl = header.strip().lower()
    for field, names in _MAP.items():
        if hl in names:
            return field
    for field, names in _MAP.items():
        if any(hl == n or hl.startswith(n) for n in names):
            return field
    return None


def _import_csv(DB, text, strategy, default_setup='', risk_per_trade=None):
    rdr = csv.reader(io.StringIO(text))
    rows = [r for r in rdr if any(c.strip() for c in r)]
    if not rows:
        return 0, 'empty file'
    header = [h.strip() for h in rows[0]]
    hl = [h.lower() for h in header]
    strat = '' if strategy in (None, '', '__perrow__') else strategy   # '' = set per row later

    # ---- Tradovate "Performance" export: derive side/entry/exit/date from the two fills ----
    if 'buyprice' in hl and 'sellprice' in hl and 'boughttimestamp' in hl:
        idx = {h: i for i, h in enumerate(hl)}
        def g(raw, k): return raw[idx[k]] if (k in idx and idx[k] < len(raw)) else ''
        def _ts(x):
            d, t = _parse_dt(x)
            try: return dt.datetime.strptime((d + ' ' + t).strip(), '%Y-%m-%d %H:%M')
            except Exception: return None
        n = 0
        for raw in rows[1:]:
            bp, sp = _f(g(raw, 'buyprice')), _f(g(raw, 'sellprice'))
            bt, st_ = g(raw, 'boughttimestamp'), g(raw, 'soldtimestamp')
            tb, ts = _ts(bt), _ts(st_)
            side = ('LONG' if tb <= ts else 'SHORT') if (tb and ts) else ''
            if side == 'SHORT':          # sold first = entry on a short
                entry, exit_, edt = sp, bp, st_
            else:                        # LONG (or unknown): bought first = entry
                entry, exit_, edt = bp, sp, bt
            date, tm = _parse_dt(edt)
            pnlv = _f(g(raw, 'pnl'))
            d = dict(date=date, time=tm, strategy=strat, setup='', side=side, taken=1,
                     entry=entry, exit=exit_, size=_f(g(raw, 'qty')), risk_usd=risk_per_trade,
                     pnl_usd=pnlv, pnl_r=(round(pnlv / risk_per_trade, 3) if (pnlv is not None and risk_per_trade) else None),
                     result=_classify(pnlv), fees=None, signal_key='', notes='imported (tradovate)')
            _insert(DB, d)
            n += 1
        tag = ' + risk/R applied' if risk_per_trade else ' (risk blank — not in export)'
        return n, 'Tradovate: derived side/entry/exit/date/size/P&L' + tag

    # ---- generic broker CSV: fuzzy column auto-map ----
    colmap = {i: _match(h) for i, h in enumerate(header)}
    n = 0
    for raw in rows[1:]:
        rec = {}
        for i, val in enumerate(raw):
            f = colmap.get(i)
            if f and val.strip() and rec.get(f) in (None, ''):
                rec[f] = val.strip()
        pnl = _f(rec.get('pnl_usd'))
        side = (rec.get('side') or '').upper()
        if 'BUY' in side or 'LONG' in side:
            side = 'LONG'
        elif 'SELL' in side or 'SHORT' in side:
            side = 'SHORT'
        date, tm = _parse_dt(rec.get('date', ''))
        d = dict(date=date, time=tm, strategy=strat,
                 setup=rec.get('setup', '') or default_setup, side=side, taken=1,
                 entry=_f(rec.get('entry')), exit=_f(rec.get('exit')), size=_f(rec.get('size')),
                 risk_usd=risk_per_trade,
                 pnl_usd=pnl, pnl_r=(round(pnl / risk_per_trade, 3) if (pnl is not None and risk_per_trade) else None),
                 result=_classify(pnl), fees=_f(rec.get('fees')), signal_key='', notes='imported')
        if d['date'] or d['pnl_usd'] is not None:
            _insert(DB, d)
            n += 1
    mapped = ', '.join('%s->%s' % (header[i], f) for i, f in colmap.items() if f)
    return n, mapped or 'no columns auto-mapped'


# ---------- rendering ----------
_CSS = ("<style>body{background:#0a0a0a;color:#ebebeb;font-family:system-ui,sans-serif;margin:0;padding:16px}"
        "h1{font-size:18px;margin:0 0 2px}h3{font-size:13px;color:#9a9a9a;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.06em}"
        ".sub{color:#555;font:11px monospace;margin-bottom:10px}"
        ".nav{margin-bottom:12px;font:11px monospace}.nav a{color:#22d3ee;text-decoration:none;margin-right:14px}"
        ".cards{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0 4px}"
        ".card{background:#141414;border:1px solid #262626;border-radius:8px;padding:10px 14px;min-width:120px}"
        ".card .k{color:#666;font:9px monospace;text-transform:uppercase;letter-spacing:.08em}"
        ".card .v{font:600 20px system-ui;margin-top:3px}"
        ".pos{color:#4ade80}.neg{color:#f87171}.zero{color:#9a9a9a}"
        ".wrap{overflow-x:auto;border:1px solid #262626;border-radius:6px;margin-bottom:8px}"
        "table{border-collapse:collapse;width:100%;font:12px monospace}"
        "th{position:sticky;top:0;background:#1c1c1c;color:#666;text-align:left;padding:7px 9px;"
        "font:9px monospace;letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid #2a2a2a;white-space:nowrap}"
        "td{padding:6px 9px;border-bottom:1px solid #1a1a1a;white-space:nowrap}"
        "tr:hover td{background:#161616}"
        "td select{background:#0a0a0a;color:#ebebeb;border:1px solid #333;border-radius:4px;font:11px monospace;padding:2px 4px}"
        "form.inl{display:inline}button{cursor:pointer;background:#1f2937;color:#cbd5e1;border:1px solid #374151;border-radius:4px;padding:3px 8px;font:11px monospace}"
        "button:hover{background:#374151}.bin{background:#3a1414;border-color:#5a1f1f;color:#f2b8b8}"
        ".addbox{background:#111;border:1px solid #262626;border-radius:8px;padding:12px;margin:8px 0 4px}"
        ".addbox input,.addbox select,.addbox textarea{background:#0a0a0a;color:#ebebeb;border:1px solid #333;border-radius:4px;padding:5px 7px;font:12px monospace;margin:3px 6px 3px 0}"
        ".addbox label{font:9px monospace;color:#777;text-transform:uppercase;display:inline-block}"
        ".addbox .fld{display:inline-block;margin-right:4px}.go{background:#134e2a;border-color:#1f7a41;color:#c7f9d8;padding:6px 16px}"
        ".empty{padding:16px;color:#555;font:12px monospace}a.dl{color:#22d3ee;text-decoration:none;margin-right:14px;font:12px monospace}"
        ".charts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:6px 0 4px}"
        ".chartcard{background:#111;border:1px solid #262626;border-radius:8px;padding:10px}"
        ".chartcard h4{margin:0 0 6px;font:10px monospace;color:#888;text-transform:uppercase;letter-spacing:.06em}"
        ".chartcard svg{display:block;width:100%;height:auto}"
        "@media(max-width:640px){"
        "body{padding:10px}h1{font-size:16px}h3{font-size:12px}"
        ".nav{line-height:1.9}.nav a{display:inline-block;margin:0 12px 4px 0}"
        ".cards{gap:6px}.card{flex:1 1 calc(50% - 6px);min-width:0;padding:8px 10px}.card .v{font-size:17px}"
        ".charts{grid-template-columns:1fr}"
        ".addbox{padding:10px}.addbox .fld{display:block;margin:0 0 8px}"
        ".addbox input,.addbox select,.addbox textarea{width:100%;box-sizing:border-box;margin:3px 0}"
        ".addbox label{display:block;margin-bottom:2px}.go{width:100%;margin-top:6px}"
        ".wrap{-webkit-overflow-scrolling:touch}table{font-size:11px}th,td{padding:5px 6px}"
        "a.dl{display:inline-block;margin:4px 14px 4px 0}"
        "}"
        "</style>")

_NAV = ("<div class='nav'><a href='/pnl'>P&amp;L journal</a><a href='/journal'>signals</a>"
        "<a href='/candidates'>candidates</a><a href='/performance'>A&#183;perf(modeled)</a>"
        "<a href='/status'>status</a></div>")


def _money(v):
    if v is None:
        return "<span class='zero'>-</span>"
    cls = 'pos' if v > 0 else ('neg' if v < 0 else 'zero')
    return "<span class='%s'>%s$%s</span>" % (cls, '+' if v > 0 else '', ('%.2f' % v))


def _chart_payload(rows):
    taken = [r for r in rows if r.get('taken')]
    seq = [r for r in taken if r.get('pnl_usd') is not None]
    seq.sort(key=lambda r: ((r.get('date') or ''), (r.get('time') or '')))
    eq = []; run = 0.0
    for r in seq:
        run += r['pnl_usd']
        eq.append({'t': ((r.get('date') or '') + ' ' + (r.get('time') or '')).strip(), 'y': round(run, 2)})
    per = {}
    for r in taken:
        s = r.get('strategy') or 'Other'
        per.setdefault(s, {'n': 0, 'pnl': 0.0})
        per[s]['n'] += 1
        if r.get('pnl_usd') is not None:
            per[s]['pnl'] += r['pnl_usd']
    order = sorted(per.items(), key=lambda x: -x[1]['pnl'])
    labels = [s for s, _ in order]
    pnl = [round(v['pnl'], 2) for _, v in order]
    counts = [v['n'] for _, v in order]
    resolved = [r for r in taken if (r.get('result') or 'open') != 'open']
    wl = {'win': 0, 'loss': 0, 'be': 0}
    for r in resolved:
        k = r.get('result')
        if k in wl:
            wl[k] += 1
    return dict(equity=eq, labels=labels, pnl=pnl, counts=counts, wl=wl)


def _charts_html(rows):
    """Pure inline-SVG charts — no external library, renders whenever the page does."""
    import html as _h
    cp = _chart_payload(rows)
    if not cp['labels'] and not cp['equity']:
        return ""
    G, R, B, TXT = '#4ade80', '#f87171', '#22d3ee', '#8a8a8a'

    # ---- equity line ----
    eq = cp['equity']
    if eq:
        W, H, pl, pr, pt, pb = 800, 220, 54, 14, 14, 24
        ys = [p['y'] for p in eq]; ymin = min(ys + [0.0]); ymax = max(ys + [0.0])
        span = (ymax - ymin) or 1.0; n = len(eq)
        def X(i): return pl + (i / (n - 1) if n > 1 else 0.5) * (W - pl - pr)
        def Y(v): return pt + (1 - (v - ymin) / span) * (H - pt - pb)
        y0 = Y(0.0)
        pts = ' '.join('%.1f,%.1f' % (X(i), Y(p['y'])) for i, p in enumerate(eq))
        area = ('M %.1f,%.1f ' % (X(0), y0) + ' '.join('L %.1f,%.1f' % (X(i), Y(p['y'])) for i, p in enumerate(eq)) + ' L %.1f,%.1f Z' % (X(n - 1), y0))
        dots = ''.join("<circle cx='%.1f' cy='%.1f' r='2.4' fill='%s'/>" % (X(i), Y(p['y']), B) for i, p in enumerate(eq))
        last = eq[-1]['y']
        eqsvg = ("<svg viewBox='0 0 %d %d' style='width:100%%;height:auto'>" % (W, H)
                 + "<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#333' stroke-width='1'/>" % (pl, y0, W - pr, y0)
                 + "<path d='%s' fill='%s' opacity='0.12'/>" % (area, B)
                 + "<polyline points='%s' fill='none' stroke='%s' stroke-width='2'/>" % (pts, B) + dots
                 + "<text x='6' y='%.1f' fill='%s' font-size='11' font-family='monospace'>$%.0f</text>" % (Y(ymax) + 9, TXT, ymax)
                 + "<text x='6' y='%.1f' fill='%s' font-size='11' font-family='monospace'>$%.0f</text>" % (Y(ymin) - 3, TXT, ymin)
                 + "<text x='6' y='%.1f' fill='%s' font-size='11' font-family='monospace'>$0</text>" % (y0 + 4, TXT)
                 + "<text x='%d' y='16' fill='%s' font-size='12' font-family='monospace' text-anchor='end'>%s</text>" % (W - pr, (G if last >= 0 else R), ('$%+.0f' % last))
                 + "</svg>")
    else:
        eqsvg = "<div class='empty'>No P&amp;L yet.</div>"

    def bars(labels, values, signed):
        if not labels:
            return "<div class='empty'>No data.</div>"
        W, H, pl, pr, pt, pb = 420, 200, 42, 8, 18, 28
        nn = len(labels); plotW = W - pl - pr; plotH = H - pt - pb
        vmin = min(values + [0.0]) if signed else 0.0
        vmax = max(values + [0.0]) if signed else max(values + [1.0])
        span = (vmax - vmin) or 1.0
        def Y(v): return pt + (1 - (v - vmin) / span) * plotH
        y0 = Y(0.0); step = plotW / nn; bw = step * 0.55
        out = ["<svg viewBox='0 0 %d %d' style='width:100%%;height:auto'>" % (W, H),
               "<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#333' stroke-width='1'/>" % (pl, y0, W - pr, y0)]
        for i, (lab, v) in enumerate(zip(labels, values)):
            x = pl + step * i + (step - bw) / 2
            yv = Y(v); top = min(yv, y0); h = max(abs(yv - y0), 0.6)
            col = (G if v >= 0 else R) if signed else B
            out.append("<rect x='%.1f' y='%.1f' width='%.1f' height='%.1f' fill='%s' rx='2'/>" % (x, top, bw, h, col))
            vs = ('%+.0f' % v) if signed else ('%d' % int(v))
            ly = (top - 4) if v >= 0 else (top + h + 11)
            out.append("<text x='%.1f' y='%.1f' fill='%s' font-size='10' font-family='monospace' text-anchor='middle'>%s</text>" % (x + bw / 2, ly, TXT, vs))
            out.append("<text x='%.1f' y='%d' fill='%s' font-size='10' font-family='monospace' text-anchor='middle'>%s</text>" % (x + bw / 2, H - 7, TXT, _h.escape(str(lab))))
        out.append("</svg>")
        return ''.join(out)

    def stack(wl):
        total = wl['win'] + wl['loss'] + wl['be']
        if total == 0:
            return "<div class='empty'>No resolved trades yet.</div>"
        W, H, pad, y, h = 420, 78, 8, 14, 28
        plotW = W - 2 * pad; x = pad
        out = ["<svg viewBox='0 0 %d %d' style='width:100%%;height:auto'>" % (W, H)]
        for name, cnt, col in (('win', wl['win'], G), ('loss', wl['loss'], R), ('be', wl['be'], '#777')):
            w = plotW * cnt / total
            if w > 0:
                out.append("<rect x='%.1f' y='%d' width='%.1f' height='%d' fill='%s'/>" % (x, y, w, h, col))
                if w > 22:
                    out.append("<text x='%.1f' y='%d' fill='#04210f' font-size='12' font-family='monospace' text-anchor='middle'>%d</text>" % (x + w / 2, y + 19, cnt))
                x += w
        out.append("<text x='%d' y='%d' fill='%s' font-size='11' font-family='monospace'>%d win &#183; %d loss &#183; %d BE (%.0f%% win)</text>" % (pad, H - 8, TXT, wl['win'], wl['loss'], wl['be'], 100 * wl['win'] / total))
        out.append("</svg>")
        return ''.join(out)

    return ("<div class='charts'>"
            "<div class='chartcard' style='grid-column:1/-1'><h4>Equity curve (cumulative $, taken trades)</h4>%s</div>"
            "<div class='chartcard'><h4>P&amp;L by strategy</h4>%s</div>"
            "<div class='chartcard'><h4>Trades taken by strategy</h4>%s</div>"
            "<div class='chartcard'><h4>Win / Loss / BE</h4>%s</div>"
            "</div>" % (eqsvg, bars(cp['labels'], cp['pnl'], True), bars(cp['labels'], cp['counts'], False), stack(cp['wl'])))


def _ref_table_html():
    """Backtest reference — computed from the project's 4-yr trade logs. Read-only target to beat."""
    pr = ''
    for s, d in BACKTEST_REF.items():
        if d.get('exp_r') is None:
            continue
        exp = d['exp_r']
        ecls = 'pos' if exp >= 0.15 else ('neg' if exp < 0 else 'zero')
        pr += ("<tr><td><b>%s</b></td><td class='%s'>%+.3fR</td><td>%.1f%%</td><td class='%s'>%.2f</td><td>%s</td></tr>"
               % (s, ecls, exp, d['win_pct'],
                  'pos' if d['pf'] >= 1 else 'neg', d['pf'], '{:,}'.format(d.get('n', 0))))
    if not pr:
        return ''
    return ("<div class='wrap'><table><thead><tr><th>Strategy</th>"
            "<th title='avg R per trade'>Exp R (edge)</th><th>Win rate</th>"
            "<th title='gross win / gross loss'>Profit factor</th><th>Backtest trades</th>"
            "</tr></thead><tbody>%s</tbody></table></div>"
            "<div class='sub'>Computed from the project's ~4-yr trade logs (A/B incl. PREM catalyst, C, F net, ORB realistic). "
            "This is the edge each strategy showed in testing — your live numbers above should track it. "
            "Edit BACKTEST_REF in pnl.py to change.</div>" % pr)


def _dashboard_html(rows, sigs, acounts=None):
    import html as _h
    overall, per = _stats(rows)
    acounts = acounts or {}
    def card(k, v, cls=''):
        return "<div class='card'><div class='k'>%s</div><div class='v %s'>%s</div></div>" % (k, cls, v)
    tot_cls = 'pos' if overall['total_usd'] > 0 else ('neg' if overall['total_usd'] < 0 else 'zero')
    cards = (card('Total P&amp;L', ('%s$%.2f' % ('+' if overall['total_usd'] > 0 else '', overall['total_usd'])), tot_cls)
             + card('Total R', ('%+.2fR' % overall['total_R']), 'pos' if overall['total_R'] > 0 else ('neg' if overall['total_R'] < 0 else 'zero'))
             + card('Trades taken', overall['n_taken'])
             + card('Win rate', '%.0f%%' % overall['win_pct'])
             + card('W / L / BE', '%d / %d / %d' % (overall['wins'], overall['losses'], overall['be']))
             + card('Open', overall['n_open'])
             + card('Best / Worst', '%s / %s' % (('$%.0f' % overall['best_usd']), ('$%.0f' % overall['worst_usd'])))
             + card('Signals not taken', overall['n_signals_not_taken']))

    # per-strategy table
    if per:
        pr = ''
        for s, d in per.items():
            exp = d.get('exp_r')
            if exp is None:
                expcell = "<span class='zero'>—</span>"
            else:
                ecls = 'pos' if exp >= 0.15 else ('neg' if exp < 0 else 'zero')   # +0.15R = Gate-0 edge
                expcell = "<span class='%s'>%+.3fR</span>" % (ecls, exp)
            pf = d.get('pf')
            pfcell = ('∞' if pf == float('inf') else ('%.2f' % pf if isinstance(pf, (int, float)) else '—'))
            pfcls = 'pos' if (pf == float('inf') or (isinstance(pf, (int, float)) and pf >= 1)) else 'neg'
            bt = BACKTEST_REF.get(s, {})
            bt_exp = ('%+.3fR' % bt['exp_r']) if bt.get('exp_r') is not None else '—'
            pr += ("<tr><td><b>%s</b></td><td>%d</td><td>%.0f%%</td><td>%s</td>"
                   "<td class='zero' style='background:#131313'>%s</td><td>%s</td>"
                   "<td class='%s'>%s</td><td>%s</td><td>%+.2fR</td></tr>"
                   % (s, d['n'], d['win_pct'], expcell, bt_exp, _money(d['avg_usd']),
                      pfcls, pfcell, _money(d['total_usd']), d['total_R']))
        pertbl = ("<div class='wrap'><table><thead><tr><th>Strategy</th><th>Trades</th><th>Win%%</th>"
                  "<th title='avg R per trade — your live edge'>Exp R (live)</th>"
                  "<th title='backtest target (computed from project trade logs)' style='background:#131313'>Exp R (BT)</th>"
                  "<th>Avg $</th><th title='gross win $ / gross loss $ (live)'>Profit factor</th>"
                  "<th>Total P&amp;L</th><th>Total R</th>"
                  "</tr></thead><tbody>%s</tbody></table></div>"
                  "<div class='sub'>Live edge vs the shaded <b>Exp R (BT)</b> target. Green ≥ +0.15R (Gate-0); "
                  "PF &gt; 1 = net winning. Live R needs Risk $ on the trade.</div>" % pr)
    else:
        pertbl = "<div class='empty'>No taken trades yet - log one below.</div>"
    reftbl = _ref_table_html()

    # add form
    opts = ''.join("<option>%s</option>" % s for s in STRATEGIES)
    res_opts = ''.join("<option value='%s'>%s</option>" % (v, v or 'auto') for v in ['', 'win', 'loss', 'be', 'open'])
    addf = ("<div class='addbox'><form method='post' action='/pnl/save'>"
            "<input type='hidden' name='id' id='f_id'>"
            "<span class='fld'><label>Date</label><br><input name='date' id='f_date' size='10' placeholder='2026-07-02' required></span>"
            "<span class='fld'><label>Time</label><br><input name='time' id='f_time' size='6' placeholder='09:45'></span>"
            "<span class='fld'><label>Strategy</label><br><select name='strategy' id='f_strategy'>%s</select></span>"
            "<span class='fld'><label>Setup</label><br><input name='setup' id='f_setup' size='10' placeholder='NYPMH'></span>"
            "<span class='fld'><label>Side</label><br><select name='side' id='f_side'><option>LONG</option><option>SHORT</option></select></span>"
            "<span class='fld'><label>Entry</label><br><input name='entry' id='f_entry' size='8'></span>"
            "<span class='fld'><label>Exit</label><br><input name='exit' id='f_exit' size='8'></span>"
            "<span class='fld'><label>Size</label><br><input name='size' id='f_size' size='4'></span>"
            "<span class='fld'><label>Risk $</label><br><input name='risk_usd' id='f_risk' size='6' placeholder='opt->R'></span>"
            "<span class='fld'><label>P&amp;L $</label><br><input name='pnl_usd' id='f_pnl' size='8' placeholder='real fill'></span>"
            "<span class='fld'><label>Result</label><br><select name='result' id='f_result'>%s</select></span>"
            "<span class='fld'><label>Taken?</label><br><input type='checkbox' name='taken' id='f_taken' checked></span>"
            "<span class='fld'><label>Notes</label><br><input name='notes' id='f_notes' size='16'></span>"
            "<button class='go' type='submit'>Save trade</button></form></div>" % (opts, res_opts))

    # import form (Tradovate-aware: fills side/entry/exit/date/size/P&L; you set strategy per row after)
    imp_opts = "<option value='__perrow__'>— set per row after import —</option>" + opts
    impf = ("<div class='addbox'><form method='post' action='/pnl/import' enctype='multipart/form-data'>"
            "<label>Broker CSV</label> <input type='file' name='file' accept='.csv' required> "
            "<label>strategy</label> <select name='strategy'>%s</select> "
            "<label>risk $/trade (optional)</label> <input name='risk_per_trade' size='6' placeholder='e.g. 100'> "
            "<button class='go' type='submit'>Import fills</button>"
            "<div class='sub'>Fills in side / entry / exit / date / size / P&amp;L automatically (Tradovate exports fully; "
            "others best-effort). Leave strategy on <b>set per row</b> and pick it on each row below. "
            "Risk isn't in broker exports — enter a $/trade to get R, or leave blank.</div></form></div>" % imp_opts)

    # alerts fired -> unified, tagged with WHICH STRATEGY, one-click prefill
    if acounts:
        cnt = ' &#183; '.join('%s %d' % (k, v) for k, v in sorted(acounts.items(), key=lambda x: -x[1]))
    else:
        cnt = 'none yet'
    if sigs:
        sr = ''
        for s in sigs:
            strat = s.get('strategy', 'A/B')
            payload = json.dumps({'date': s.get('date', ''), 'strategy': strat, 'setup': s.get('setup', ''),
                                  'side': 'LONG' if str(s.get('side', '')).upper().startswith('L') else 'SHORT',
                                  'entry': s.get('entry', '')}).replace("'", '&#39;')
            sr += ("<tr><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td><button onclick='pref(%s)'>log this &#8593;</button></td></tr>"
                   % (_h.escape(str((s.get('date', '') + ' ' + (s.get('time', '') or '')).strip())),
                      _h.escape(str(strat)), _h.escape(str(s.get('side', ''))),
                      _h.escape(str(s.get('setup', ''))), _h.escape(str(s.get('entry', '') if s.get('entry') is not None else '')),
                      "'%s'" % payload))
        sigtbl = ("<div class='sub'>alerts logged by strategy: %s</div>"
                  "<div class='wrap'><table><thead><tr><th>When</th><th>Strategy</th><th>Side</th><th>Setup</th>"
                  "<th>Entry</th><th></th></tr></thead><tbody>%s</tbody></table></div>" % (cnt, sr))
    else:
        sigtbl = ("<div class='empty'>No alerts logged yet. Point each strategy service at "
                  "<code>POST /pnl/fire?strategy=F&amp;side=SHORT&amp;setup=NYPMH&amp;entry=20450</code> "
                  "(A/B signals appear here automatically).</div>")

    # trades table
    if rows:
        tr = ''
        for r in rows:
            chk = ' checked' if r.get('taken') else ''
            takencell = ("<form class='inl' method='post' action='/pnl/toggle/%s'>"
                         "<input type='checkbox' title='counts in your P&amp;L when ticked' "
                         "onchange='this.form.submit()'%s></form>" % (r['id'], chk))
            cur = str(r.get('strategy', '') or '')
            o = "<option value=''%s>&mdash; pick &mdash;</option>" % (" selected" if not cur else "")
            for s in STRATEGIES:
                o += "<option%s>%s</option>" % (" selected" if s == cur else "", s)
            need = "" if cur else " style='border-color:#b45309;background:#2a1c05'"
            stratcell = ("<form class='inl' method='post' action='/pnl/setstrat/%s'>"
                         "<select name='strategy' onchange='this.form.submit()'%s>%s</select></form>" % (r['id'], need, o))
            tr += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s</td><td>%s</td><td style='text-align:center'>%s</td>"
                   "<td><form class='inl' method='post' action='/pnl/del/%s' onsubmit=\"return confirm('Delete this trade?')\"><button class='bin'>del</button></form></td></tr>"
                   % (_h.escape(str(r.get('date', ''))), _h.escape(str(r.get('time', '') or '')),
                      stratcell, _h.escape(str(r.get('setup', '') or '')),
                      _h.escape(str(r.get('side', '') or '')), _h.escape(str(r.get('entry', '') or '')),
                      _h.escape(str(r.get('exit', '') or '')), _money(r.get('pnl_usd')),
                      ('%+.2f' % r['pnl_r']) if r.get('pnl_r') is not None else '-',
                      _h.escape(str(r.get('result', '') or '')), takencell, r['id']))
        tradetbl = ("<div class='wrap'><table><thead><tr><th>Date</th><th>Time</th><th>Strat</th><th>Setup</th>"
                    "<th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L $</th><th>R</th><th>Result</th>"
                    "<th>Taken</th><th></th></tr></thead><tbody>%s</tbody></table></div>" % tr)
    else:
        tradetbl = "<div class='empty'>No trades logged yet.</div>"

    js = ("<script>function pref(p){var o=JSON.parse(p);"
          "document.getElementById('f_date').value=o.date||'';"
          "document.getElementById('f_setup').value=o.setup||'';"
          "document.getElementById('f_entry').value=o.entry||'';"
          "var sv=document.getElementById('f_side');sv.value=o.side||'LONG';"
          "var st=document.getElementById('f_strategy');st.value=o.strategy||'A/B';"
          "document.getElementById('f_pnl').focus();"
          "document.querySelector('.addbox').scrollIntoView({behavior:'smooth'});return false;}</script>")

    return (_CSS + "<h1>P&amp;L Journal - all strategies</h1>"
            "<div class='sub'>real broker fills &#183; Taken toggle &#183; persisted in journal.db (downloadable)</div>"
            + _NAV
            + "<div class='cards'>" + cards + "</div>"
            + "<a class='dl' href='/pnl.csv'>&#8595; download CSV</a><a class='dl' href='/pnl.db'>&#8595; download database (journal.db)</a>"
            + "<h3>Summary by strategy — trades taken &amp; P&amp;L</h3>" + pertbl
            + "<h3>Backtest reference — edge, win rate &amp; profit factor</h3>" + reftbl
            + _charts_html(rows)
            + "<h3>Log a trade (real fill)</h3>" + addf
            + "<h3>Import broker CSV</h3>" + impf
            + "<h3>All logged trades</h3>" + tradetbl
            + "<h3>Alerts fired - which strategy? (did you take any?)</h3>" + sigtbl
            + js)


# ---------- registration ----------
def register(app, DB, render_page=None, wants_html=None):
    from flask import request, jsonify, redirect, Response, send_file
    init(DB)

    def _html(): return wants_html() if wants_html else ('text/html' in request.headers.get('Accept', ''))

    @app.route('/pnl')
    def pnl_dash():
        rows = _all(DB)
        if _html():
            body = _dashboard_html(rows, _recent_alerts(DB), _alert_counts(DB))
            return "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>" + body + "</body></html>"
        overall, per = _stats(rows)
        return jsonify(summary=overall, per_strategy=per, trades=rows,
                       alerts=_recent_alerts(DB), alerts_by_strategy=_alert_counts(DB))

    @app.route('/pnl/fire', methods=['GET', 'POST'])
    def pnl_fire():
        # Optional shared secret: set FIRE_SECRET env and pass ?secret=... (recommended for a public URL)
        sec = os.environ.get('FIRE_SECRET', '')
        if sec and request.values.get('secret', '') != sec and (request.get_json(silent=True) or {}).get('secret') != sec:
            return jsonify(error='bad or missing secret'), 401
        j = request.get_json(silent=True) or {}
        g = request.values
        def pick(*keys):
            for k in keys:
                if j.get(k) not in (None, ''):
                    return j.get(k)
                if g.get(k) not in (None, ''):
                    return g.get(k)
            return ''
        strat = pick('strategy', 'strat', 's') or 'Other'
        side = str(pick('side', 'dir') or '').upper()
        if side.startswith('L') or 'BUY' in side:
            side = 'LONG'
        elif side.startswith('S') or 'SELL' in side:
            side = 'SHORT'
        d = dict(strategy=strat, date=pick('date') or dt.date.today().isoformat(),
                 time=pick('time'), side=side, setup=pick('setup', 'cat'),
                 entry=_f(pick('entry')), sl=_f(pick('sl', 'SL')), tp=_f(pick('tp', 'TP')),
                 key=pick('key'), text=pick('text', 'alert', 'msg'))
        _log_alert(DB, d)
        return jsonify(ok=True, logged=strat, alert=d)

    @app.route('/pnl/save', methods=['POST'])
    def pnl_save():
        d = _row_from_form(request.form)
        rid = request.form.get('id', '').strip()
        if rid:
            _update(DB, int(rid), d)
        else:
            _insert(DB, d)
        return redirect('/pnl')

    @app.route('/pnl/del/<int:rid>', methods=['POST'])
    def pnl_del(rid):
        c = sqlite3.connect(DB); c.execute('DELETE FROM fills WHERE id=?', (rid,)); c.commit(); c.close()
        return redirect('/pnl')

    @app.route('/pnl/setstrat/<int:rid>', methods=['POST'])
    def pnl_setstrat(rid):
        s = request.form.get('strategy', '')
        c = sqlite3.connect(DB); c.execute('UPDATE fills SET strategy=? WHERE id=?', (s, rid)); c.commit(); c.close()
        return redirect('/pnl')

    @app.route('/pnl/toggle/<int:rid>', methods=['POST'])
    def pnl_toggle(rid):
        c = sqlite3.connect(DB)
        c.execute('UPDATE fills SET taken = CASE taken WHEN 1 THEN 0 ELSE 1 END WHERE id=?', (rid,))
        c.commit(); c.close()
        return redirect('/pnl')

    @app.route('/pnl/import', methods=['POST'])
    def pnl_import():
        f = request.files.get('file')
        if not f:
            return redirect('/pnl')
        strat = request.form.get('strategy', 'Other')
        rpt = _f(request.form.get('risk_per_trade'))
        text = f.read().decode('utf-8-sig', errors='replace')
        n, mapped = _import_csv(DB, text, strat, risk_per_trade=rpt)
        if _html():
            return redirect('/pnl')
        return jsonify(imported=n, mapping=mapped)

    @app.route('/pnl.csv')
    def pnl_csv():
        rows = _all(DB)
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(_COLS)
        for r in rows:
            w.writerow([r.get(k, '') for k in _COLS])
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=pnl_journal.csv'})

    @app.route('/pnl.db')
    def pnl_db():
        return send_file(DB, mimetype='application/x-sqlite3', as_attachment=True, download_name='journal.db')

    print('[pnl] unified P&L journal mounted at /pnl (table: fills)', flush=True)
