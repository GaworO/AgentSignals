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

STRATEGIES = ['A/B', 'C', 'F', 'ORB', 'PREM', 'S1', 'S2', 'Other']
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
        return float(str(v).replace(',', '').replace('$', '').strip())
    except Exception:
        return None


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
        per[s] = dict(n=len(srows), resolved=len(sres), win_pct=round(100 * sw / len(sres), 1) if sres else 0.0,
                      total_usd=_sum(sp), total_R=round(sum(sr), 2) if sr else 0.0)
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


def _import_csv(DB, text, strategy, default_setup=''):
    rdr = csv.reader(io.StringIO(text))
    rows = [r for r in rdr if any(c.strip() for c in r)]
    if not rows:
        return 0, 'empty file'
    header = rows[0]
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
        d = dict(date=rec.get('date', ''), time='', strategy=strategy,
                 setup=rec.get('setup', '') or default_setup, side=side, taken=1,
                 entry=_f(rec.get('entry')), exit=_f(rec.get('exit')), size=_f(rec.get('size')),
                 risk_usd=None, pnl_usd=pnl, pnl_r=None, result=_classify(pnl),
                 fees=_f(rec.get('fees')), signal_key='', notes='imported')
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
        "form.inl{display:inline}button{cursor:pointer;background:#1f2937;color:#cbd5e1;border:1px solid #374151;border-radius:4px;padding:3px 8px;font:11px monospace}"
        "button:hover{background:#374151}.bin{background:#3a1414;border-color:#5a1f1f;color:#f2b8b8}"
        ".addbox{background:#111;border:1px solid #262626;border-radius:8px;padding:12px;margin:8px 0 4px}"
        ".addbox input,.addbox select,.addbox textarea{background:#0a0a0a;color:#ebebeb;border:1px solid #333;border-radius:4px;padding:5px 7px;font:12px monospace;margin:3px 6px 3px 0}"
        ".addbox label{font:9px monospace;color:#777;text-transform:uppercase;display:inline-block}"
        ".addbox .fld{display:inline-block;margin-right:4px}.go{background:#134e2a;border-color:#1f7a41;color:#c7f9d8;padding:6px 16px}"
        ".empty{padding:16px;color:#555;font:12px monospace}a.dl{color:#22d3ee;text-decoration:none;margin-right:14px;font:12px monospace}"
        "</style>")

_NAV = ("<div class='nav'><a href='/pnl'>P&amp;L journal</a><a href='/journal'>signals</a>"
        "<a href='/candidates'>candidates</a><a href='/performance'>A&#183;perf(modeled)</a>"
        "<a href='/status'>status</a></div>")


def _money(v):
    if v is None:
        return "<span class='zero'>-</span>"
    cls = 'pos' if v > 0 else ('neg' if v < 0 else 'zero')
    return "<span class='%s'>%s$%s</span>" % (cls, '+' if v > 0 else '', ('%.2f' % v))


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
            pr += ("<tr><td>%s</td><td>%d</td><td>%d</td><td>%.0f%%</td><td>%s</td><td>%+.2fR</td></tr>"
                   % (s, d['n'], d['resolved'], d['win_pct'], _money(d['total_usd']), d['total_R']))
        pertbl = ("<div class='wrap'><table><thead><tr><th>Strategy</th><th>Taken</th><th>Resolved</th>"
                  "<th>Win%</th><th>P&amp;L</th><th>R</th></tr></thead><tbody>%s</tbody></table></div>" % pr)
    else:
        pertbl = "<div class='empty'>No taken trades yet - log one below.</div>"

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

    # import form
    impf = ("<div class='addbox'><form method='post' action='/pnl/import' enctype='multipart/form-data'>"
            "<label>Broker CSV</label> <input type='file' name='file' accept='.csv' required> "
            "<label>as strategy</label> <select name='strategy'>%s</select> "
            "<button class='go' type='submit'>Import fills</button>"
            "<div class='sub'>Auto-maps date/side/entry/exit/qty/P&amp;L/fees from common broker exports. "
            "All rows tagged with the strategy you pick + marked Taken.</div></form></div>" % opts)

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
            takenbadge = "yes" if r.get('taken') else "-"
            tr += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td><form class='inl' method='post' action='/pnl/toggle/%s'><button>%s</button></form> "
                   "<form class='inl' method='post' action='/pnl/del/%s' onsubmit=\"return confirm('Delete this trade?')\"><button class='bin'>del</button></form></td></tr>"
                   % (_h.escape(str(r.get('date', ''))), _h.escape(str(r.get('time', '') or '')),
                      _h.escape(str(r.get('strategy', ''))), _h.escape(str(r.get('setup', '') or '')),
                      _h.escape(str(r.get('side', '') or '')), _h.escape(str(r.get('entry', '') or '')),
                      _h.escape(str(r.get('exit', '') or '')), _money(r.get('pnl_usd')),
                      ('%+.2f' % r['pnl_r']) if r.get('pnl_r') is not None else '-',
                      _h.escape(str(r.get('result', '') or '')), takenbadge,
                      r['id'], 'taken' if r.get('taken') else 'not', r['id']))
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
            + "<h3>Log a trade (real fill)</h3>" + addf
            + "<h3>Import broker CSV</h3>" + impf
            + "<h3>Per-strategy P&amp;L (taken only)</h3>" + pertbl
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
        text = f.read().decode('utf-8-sig', errors='replace')
        n, mapped = _import_csv(DB, text, strat)
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
