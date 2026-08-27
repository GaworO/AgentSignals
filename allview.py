#!/usr/bin/env python3
"""
allview.py — JOINED "All trades" + "All candidates" across strategies A/B, C, F.

Isolated add-on (same pattern as pnl.py / forex_pnl.py): adds read-only routes on the MAIN agent:
    /all/trades      — every strategy's modeled trades in one table + a PER-DAY Pine export
    /all/candidates  — every strategy's live candidates in one table

It NEVER touches detector logic. A/B is read locally (outcomes.json + signals key for /chart);
C / F are fetched over HTTP from their own services (server-side, no CORS) using env URLs:

    STRAT_C_URL   = https://<model-c service>        (reads  <url>/journal , <url>/candidates)
    STRAT_F_URL   = https://<strategy-f service>     (reads  <url>/performance_f , <url>/candidates?format=json)
Any URL left unset is skipped — the view degrades gracefully to whatever is reachable.

Wire into agent.py (next to the other registers):
    import allview ; allview.register(app)
"""
import os, json, datetime as dt, sqlite3

try:
    import requests
except Exception:
    requests = None
# ---- where A/B keeps its resolved outcomes (same file manage.py writes) ----
_DATA_DIR = os.environ.get('DATA_DIR', '/data') or '.'
_OUTCOMES = os.path.join(_DATA_DIR, 'outcomes.json')
_ANNOT_DB = os.path.join(_DATA_DIR, 'all_annotations.db')   # user marks: took? + comment (own SQLite table, isolated)
def _annot_conn():
    con = sqlite3.connect(_ANNOT_DB)
    con.execute('CREATE TABLE IF NOT EXISTS annotations (uid TEXT PRIMARY KEY, taken INTEGER DEFAULT 0, comment TEXT DEFAULT "", updated_at TEXT)')
    return con
def _load_annot():
    try:
        con = _annot_conn(); rows = con.execute('SELECT uid, taken, comment FROM annotations').fetchall(); con.close()
        return {u: {'taken': bool(t), 'comment': c or ''} for (u, t, c) in rows}
    except Exception as _e:
        print('annot load err', _e, flush=True); return {}
def _uid(t):
    return t.get('key') or ('%s|%s|%s|%s' % (t.get('strat',''), t.get('ts_ms',0), t.get('dir',''), t.get('entry','')))
def _migrate_json_once():
    """One-time: import any pre-existing all_annotations.json into the SQLite table (no overwrite), then park the file."""
    old = os.path.join(_DATA_DIR, 'all_annotations.json')
    if not os.path.exists(old): return
    try:
        data = json.load(open(old)) or {}
        con = _annot_conn(); nins = 0
        for uid, a in data.items():
            if not uid: continue
            con.execute('INSERT OR IGNORE INTO annotations(uid, taken, comment, updated_at) VALUES(?,?,?,?)',
                        (uid, 1 if a.get('taken') else 0, (a.get('comment') or ''),
                         a.get('ts') or dt.datetime.utcnow().isoformat(timespec='seconds')))
            nins += con.total_changes and 1 or 0
        con.commit(); con.close()
        os.rename(old, old + '.migrated')
        print('annot: migrated %d json rows -> sqlite (%s parked)' % (len(data), os.path.basename(old)), flush=True)
    except Exception as _e:
        print('annot migrate err', _e, flush=True)

# strategy -> (Pine color, HTML chip color)
STRAT_COLORS = {
    'AB':  ('color.aqua',    '#22d3ee'),
    'C':   ('color.lime',    '#4ade80'),
    'F':   ('color.orange',  '#f59e0b'),
}


# ───────────────────────────── helpers ─────────────────────────────
def _get(url, path, timeout=12):
    if not url or requests is None:
        return None
    try:
        r = requests.get(url.rstrip('/') + path, timeout=timeout)
        if getattr(r, 'status_code', 0) != 200:
            return None
        return r.json()
    except Exception:
        return None


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _iso_ms(s):
    try:
        t = str(s)
        if '+' not in t and 'Z' not in t:
            t += '+00:00'
        return int(dt.datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return 0


def _day_of(ms):
    try:
        return dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d')
    except Exception:
        return ''


def _hhmm(ms):
    try:
        return dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime('%H:%M')
    except Exception:
        return ''


def _norm(strat, ts_ms, dir_, cat, entry, sl, r, status, key='', chartable=False):
    """Common trade/candidate record."""
    return dict(strat=strat, ts_ms=int(ts_ms or 0), day=_day_of(ts_ms), time=_hhmm(ts_ms),
                dir=dir_ or '', cat=cat or strat, entry=entry, sl=sl, r=r,
                status=status or '', key=key or '', chartable=bool(chartable))


# ───────────────────────── per-source loaders ─────────────────────────
def _ab_trades():
    try:
        outs = json.load(open(_OUTCOMES))
    except Exception:
        return []
    res = []
    for o in outs:
        e = _f(o.get('entry')); sl = _f(o.get('sl'))
        if e is None or sl is None:
            continue
        res.append(_norm('AB', o.get('bos_ms') or o.get('closed_ms'), o.get('dir'), o.get('cat'),
                         e, sl, o.get('r'), o.get('reason'), key=str(o.get('key', '')), chartable=True))
    return res


def _c_trades():
    j = _get(os.environ.get('STRAT_C_URL', ''), '/journal')
    if not isinstance(j, dict):
        return []
    res = []
    for t in j.values():
        if not isinstance(t, dict):
            continue
        e = _f(t.get('entry')); sl = _f(t.get('SL', t.get('sl')))
        if e is None or sl is None:
            continue
        ts = t.get('fill_ts') or _iso_ms(t.get('alert_ts'))
        res.append(_norm('C', ts, t.get('dir'), 'C', e, sl, t.get('R'), t.get('status')))
    return res


def _f_trades():
    j = _get(os.environ.get('STRAT_F_URL', ''), '/performance_f')
    if not isinstance(j, dict):
        return []
    res = []
    for t in j.get('trades', []) or []:
        e = _f(t.get('entry')); sl = _f(t.get('SL', t.get('sl')))
        if e is None or sl is None:
            continue
        ts = t.get('disp_end_ms') or _iso_ms(t.get('alert_ts'))
        res.append(_norm('F', ts, t.get('dir'), 'F.P.FVG', e, sl, t.get('R'), t.get('status')))
    return res


def _dedup_key(t):
    """Same setup fired under several catalysts/models => identical geometry. Collapse on it.
    (strat, day, dir, entry@0.1, sl@0.1) — two genuinely different trades never share all five."""
    e = _f(t.get('entry')); s = _f(t.get('sl'))
    return (t.get('strat', ''), t.get('day', ''), (t.get('dir') or '').upper(),
            round(e, 1) if e is not None else None,
            round(s, 1) if s is not None else None)


def _dedup(rows):
    """One row per setup. Confluence inflation (~4.3x) logs the same A/B setup once per catalyst
    AND per model; this keeps ONE row, upgraded to carry the fullest catalyst tag AND a chartable
    key if any duplicate had one, and stamps _n = how many raw rows collapsed. Off with ALLVIEW_DEDUP=0."""
    if os.environ.get('ALLVIEW_DEDUP', '1') == '0':
        for t in rows:
            t['_n'] = 1
        return rows
    best = {}
    for t in rows:
        k = _dedup_key(t)
        cur = best.get(k)
        if cur is None:
            t['_n'] = 1
            best[k] = t
            continue
        cur['_n'] = cur.get('_n', 1) + 1                       # remember how many collapsed
        if len(str(t.get('cat') or '')) > len(str(cur.get('cat') or '')):
            cur['cat'] = t.get('cat')                          # keep the fullest catalyst label
        if (t.get('chartable') and t.get('key')) and not (cur.get('chartable') and cur.get('key')):
            cur['chartable'] = True; cur['key'] = t.get('key')  # never drop a chart link on merge
    return list(best.values())


def _all_trades():
    out = []
    for fn in (_ab_trades, _c_trades, _f_trades):
        try:
            out += fn()
        except Exception:
            pass
    out = _dedup(out)                                          # collapse confluence/model duplicates
    out.sort(key=lambda x: x['ts_ms'], reverse=True)
    return out


# ───────────────────────── Pine generation ─────────────────────────
def _pine_lines(rec):
    e = _f(rec.get('entry')); sl = _f(rec.get('sl')); ts = int(rec.get('ts_ms') or 0)
    if e is None or sl is None or not ts:
        return None
    pc = STRAT_COLORS.get(rec['strat'], ('color.gray', '#888'))[0]
    tp = e + 2.0 * (e - sl)
    hi = max(e, sl, tp)
    side = 'LONG' if e > sl else 'SHORT'
    rr = rec.get('r')
    rtxt = ('%+.0fR' % rr) if isinstance(rr, (int, float)) else str(rec.get('status', ''))
    txt = ('%s %s %s %s' % (rec['strat'], rec.get('cat', ''), side, rtxt)).replace('"', '').strip()
    right = ts + 90 * 60 * 1000                        # draw ~90min wide so it's visible
    return ['    box.new(%d, %.2f, %d, %.2f, xloc=xloc.bar_time, border_color=color.new(color.red, 70), bgcolor=color.new(color.red, 85))' % (ts, max(e, sl), right, min(e, sl)),
            '    box.new(%d, %.2f, %d, %.2f, xloc=xloc.bar_time, border_color=color.new(color.green, 70), bgcolor=color.new(color.green, 85))' % (ts, max(e, tp), right, min(e, tp)),
            '    line.new(%d, %.2f, %d, %.2f, xloc=xloc.bar_time, color=%s, width=1, style=line.style_dashed)' % (ts, e, right, e, pc),
            '    line.new(%d, %.2f, %d, %.2f, xloc=xloc.bar_time, color=color.red, width=2)' % (ts, sl, right, sl),
            '    line.new(%d, %.2f, %d, %.2f, xloc=xloc.bar_time, color=color.green, width=2)' % (ts, tp, right, tp),
            '    label.new(%d, %.2f, "%s", xloc=xloc.bar_time, style=label.style_label_down, color=%s, textcolor=color.white, size=size.small)' % (ts, hi, txt, pc)]


def _pine_wrap(bodylines, title):
    head = ['//@version=5',
            'indicator("%s", overlay=true, max_boxes_count=500, max_labels_count=500, max_lines_count=500)' % title,
            'if barstate.islast']
    body = bodylines if bodylines else ['    label.new(bar_index, high, "no trades", style=label.style_label_down)']
    return '\n'.join(head + body)


def _pine_for(recs, title):
    body = []
    for r in recs:
        ln = _pine_lines(r)
        if ln:
            body += ln
    return _pine_wrap(body, title)


# ───────────────────────────── HTML ─────────────────────────────
CSS = ("<style>body{background:#0a0a0a;color:#ebebeb;font-family:system-ui,sans-serif;margin:0;padding:16px}"
       "h1{font-size:18px;margin:0 0 2px}.sub{color:#666;font:11px monospace;margin-bottom:12px}"
       "table{border-collapse:collapse;width:100%;font:13px system-ui}"
       "th{background:#1c1c1c;color:#7a869a;text-align:left;padding:7px 9px;position:sticky;top:0}"
       "td{padding:6px 9px;border-top:1px solid #191919}"
       ".chip{display:inline-block;padding:1px 8px;border-radius:10px;font:11px monospace;font-weight:700;color:#04121a}"
       ".mut{color:#8a8a8a;font:12px monospace}.g{color:#4ade80}.r{color:#f87171}"
       "a{color:#22d3ee;text-decoration:none}button{cursor:pointer;border:0;border-radius:5px;font-weight:700}"
       ".bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}"
       "select{background:#141414;color:#ebebeb;border:1px solid #333;border-radius:6px;padding:6px 10px;font:13px system-ui}"
       ".copy{background:#22d3ee;color:#04202a;padding:8px 14px}"
       "textarea{width:100%;height:38vh;background:#0d0d0d;color:#d6d6d6;border:1px solid #222;border-radius:8px;"
       "padding:10px;font:12px/1.45 monospace;box-sizing:border-box;margin-top:8px}</style>")


def _chip(strat):
    col = STRAT_COLORS.get(strat, ('', '#888'))[1]
    return "<span class='chip' style='background:%s'>%s</span>" % (col, strat)


def _rcell(r):
    if isinstance(r, (int, float)):
        cls = 'g' if r > 0 else ('r' if r < 0 else 'mut')
        return "<td class='%s' style='font-weight:700'>%+.2fR</td>" % (cls, r)
    return "<td class='mut'>—</td>"


def _num(x):
    return ('%.2f' % x) if isinstance(x, (int, float)) else (x if x not in (None, '') else '—')


def _strat_filter_bar(present):
    """Checkbox row (one per strategy present) that filters the table + the day Pine."""
    order = [s for s in ('AB', 'C', 'F') if s in present]
    boxes = ''.join(
        "<label style='display:inline-flex;align-items:center;gap:5px'>"
        "<input type='checkbox' class='fstrat' value='%s' checked onchange='rebuild()'>%s</label>"
        % (s, _chip(s)) for s in order)
    return ("<div class='bar'><b>Strategy:</b>" + boxes +
            "<a href='#' onclick=\"document.querySelectorAll('.fstrat').forEach(c=>c.checked=true);rebuild();return false\">all</a>"
            "<a href='#' onclick=\"document.querySelectorAll('.fstrat').forEach(c=>c.checked=false);rebuild();return false\">none</a></div>")


_NAV = ("<div style='font:12px system-ui,sans-serif;margin:0 0 14px'>"
        "<a href='/all/trades' style='color:#22d3ee;margin-right:16px;text-decoration:none'>all trades</a>"
        "<a href='/all/candidates' style='color:#22d3ee;margin-right:16px;text-decoration:none'>candidates</a>"
        "<a href='/all/reconcile' style='color:#22d3ee;margin-right:16px;text-decoration:none'>reconcile</a>"
        "<a href='/' style='color:#888;text-decoration:none'>&larr; home</a></div>")


def render_trades():
    import html as _h
    rows = _all_trades()
    ann = _load_annot()
    present = {r['strat'] for r in rows}
    days = sorted({r['day'] for r in rows if r['day']}, reverse=True)
    opts = ''.join("<option value='%s'>%s</option>" % (d, d) for d in days) or "<option>—</option>"

    # per-trade Pine body embedded as JS so the day script rebuilds live from the strategy filter
    js_trades = [dict(strat=r['strat'], day=r['day'], body='\n'.join(_pine_lines(r) or []))
                 for r in rows if _pine_lines(r)]

    trs = ''
    for i, r in enumerate(rows):
        chart = ("<a href='/chart?key=%s' target='_blank'>chart</a>" % _h.escape(r['key'])
                 if r['chartable'] and r['key'] else "<span class='mut'>—</span>")
        one = _h.escape(_pine_wrap(_pine_lines(r) or [], '%s %s' % (r['strat'], r['day'])))
        pine = ("<button class='copy' style='padding:3px 9px' "
                "onclick=\"navigator.clipboard.writeText(document.getElementById('one_%d').value);this.textContent='copied'\">Pine</button>"
                "<textarea id='one_%d' style='display:none'>%s</textarea>") % (i, i, one)
        _nn = int(r.get('_n', 1) or 1)
        catd = _h.escape(str(r['cat'])) + (" <span class='mut' title='%d confluences merged into one setup' style='font-size:10px'>&times;%d</span>" % (_nn, _nn) if _nn > 1 else '')
        _u = _h.escape(_uid(r)); _a = ann.get(_uid(r), {}); _cm = _a.get('comment', '') or ''; _ce = _h.escape(_cm)
        took = ("<td style='text-align:center'><input type='checkbox' class='ann-took' data-uid=\"%s\" %s onchange='saveTook(this)'></td>") % (_u, ('checked' if _a.get('taken') else ''))
        cbtn = ("<td style='text-align:center'><button type='button' class='cbtn%s' data-uid=\"%s\" data-comment=\"%s\" title=\"%s\" onclick='openComment(this)'>%s</button></td>") % (
            (' has' if _cm else ''), _u, _ce, _ce, ('\U0001F4DD' if _cm else '\U0001F4AC'))
        trs += ("<tr data-strat='%s'><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s<td>%s</td><td>%s</td>%s%s</tr>") % (
            r['strat'], r['day'], r['time'], _chip(r['strat']), r['dir'], catd, _num(r['entry']), _num(r['sl']),
            _rcell(r['r']), chart, pine, took, cbtn)

    body = (CSS + "<style>.cbtn{background:none;border:1px solid #cfcfcf;border-radius:5px;cursor:pointer;padding:2px 7px;font-size:13px;line-height:1.2}.cbtn.has{border-color:#e0a800;background:#fff3cd}</style>" +
            _NAV + "<h1>All trades — A/B · C · F</h1>"
            "<div class='sub'>modeled outcomes across every strategy · read-only · " + _reach_note() + "</div>"
            + _strat_filter_bar(present) +
            "<div class='bar'><b>Per-day Pine for TradingView:</b>"
            "<select id='daysel' onchange='rebuild()'>" + opts + "</select>"
            "<button class='copy' onclick=\"navigator.clipboard.writeText(document.getElementById('psbox').value);"
            "this.textContent='Copied ✓';setTimeout(()=>this.textContent='Copy day script',1200)\">Copy day script</button>"
            "<span class='mut'>obeys the strategy filter · pick a day → copy → TradingView → Pine Editor → paste → Add to chart</span></div>"
            "<textarea id='psbox' readonly></textarea>"
            "<table><thead><tr><th>When (UTC)</th><th>Strat</th><th>Dir</th><th>Cat</th><th>Entry</th><th>SL</th>"
            "<th>Result</th><th>Chart</th><th>Pine</th><th>Took?</th><th>Comment</th></tr></thead><tbody>" + (trs or
            "<tr><td colspan=11 class='mut'>no trades yet (or no service URLs set)</td></tr>") + "</tbody></table>"
            "<div id='cmodal' style='display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.45);z-index:9999' onclick='if(event.target.id==&quot;cmodal&quot;)closeComment()'>""<div style='background:#fff;max-width:440px;margin:9% auto;padding:18px 20px;border-radius:10px;box-shadow:0 10px 40px rgba(0,0,0,.3)'>""<div style='font-weight:700;font-size:15px;margin-bottom:4px'>Trade comment</div>""<div id='cmeta' class='mut' style='font-size:11px;margin-bottom:8px;word-break:break-all'></div>""<textarea id='ctext' style='width:100%;height:110px;box-sizing:border-box;font:inherit;padding:8px'></textarea>""<div style='margin-top:12px;text-align:right'>""<button onclick='closeComment()' style='padding:6px 14px;margin-right:8px'>Cancel</button>""<button onclick='saveComment()' style='padding:6px 16px;font-weight:700;background:#1565c0;color:#fff;border:0;border-radius:5px;cursor:pointer'>Save</button>""</div></div></div>""<script>var _curBtn=null;""function openComment(b){_curBtn=b;document.getElementById('ctext').value=b.getAttribute('data-comment')||'';document.getElementById('cmeta').textContent=b.getAttribute('data-uid');document.getElementById('cmodal').style.display='block';document.getElementById('ctext').focus();}""function closeComment(){document.getElementById('cmodal').style.display='none';_curBtn=null;}""function saveComment(){if(!_curBtn)return;var cm=document.getElementById('ctext').value,uid=_curBtn.getAttribute('data-uid');""fetch('/all/annotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uid:uid,comment:cm})}).then(function(r){return r.json();}).then(function(j){var h=!!cm.trim();_curBtn.setAttribute('data-comment',cm);_curBtn.title=cm;_curBtn.classList.toggle('has',h);_curBtn.textContent=h?String.fromCodePoint(0x1F4DD):String.fromCodePoint(0x1F4AC);closeComment();}).catch(function(e){alert('save failed');});}""function saveTook(el){fetch('/all/annotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uid:el.getAttribute('data-uid'),taken:el.checked})}).catch(function(e){});}""var TRADES=" + json.dumps(js_trades) + ";"
            "function selStrats(){return Array.from(document.querySelectorAll('.fstrat:checked')).map(c=>c.value);}"
            "function applyRows(){var ss=selStrats();document.querySelectorAll('tr[data-strat]').forEach(function(tr){"
            "tr.style.display=ss.indexOf(tr.getAttribute('data-strat'))>=0?'':'none';});}"
            "function buildDay(){var sel=document.getElementById('daysel');if(!sel)return;var d=sel.value,ss=selStrats();"
            "var head='//@version=5\\nindicator(\"All trades '+d+'\", overlay=true, max_boxes_count=500, max_labels_count=500, max_lines_count=500)\\nif barstate.islast';"
            "var b=TRADES.filter(function(t){return t.day===d&&ss.indexOf(t.strat)>=0;}).map(function(t){return t.body;});"
            "var body=b.length?b.join('\\n'):'    label.new(bar_index, high, \"no trades\", style=label.style_label_down)';"
            "document.getElementById('psbox').value=head+'\\n'+body;}"
            "function rebuild(){applyRows();buildDay();}rebuild();</script>")
    return body


def _reach_note():
    have = ['A/B(local)']
    for lbl, env in (('C', 'STRAT_C_URL'), ('F', 'STRAT_F_URL')):
        have.append(lbl + ('✓' if os.environ.get(env) else '✗'))
    return ' '.join(have)


# ─────────────────────── candidates (best-effort) ───────────────────────
def _ab_candidates():
    try:
        tr = json.load(open(os.path.join(_DATA_DIR, 'trace.json')))
    except Exception:
        return []
    res = []
    for r in tr[-200:]:
        res.append(dict(strat='AB', day=_day_of(r.get('trig_ms')), time=_hhmm(r.get('trig_ms')),
                        dir=r.get('dir', ''), stage=r.get('stage', ''), note=r.get('cat', r.get('note', ''))))
    return res


def _c_candidates():
    j = _get(os.environ.get('STRAT_C_URL', ''), '/candidates')
    cands = (j or {}).get('cands', []) if isinstance(j, dict) else []
    res = []
    for x in cands or []:
        res.append(dict(strat='C', day='', time=x.get('disp_start', ''), dir=x.get('dir', ''),
                        stage=x.get('step', ''), note='FVG %s-%s' % (x.get('fvg_lo', ''), x.get('fvg_hi', ''))))
    return res


def _f_candidates():
    j = _get(os.environ.get('STRAT_F_URL', ''), '/candidates?format=json')
    arr = j if isinstance(j, list) else (j.get('candidates') if isinstance(j, dict) else [])
    res = []
    for x in arr or []:
        res.append(dict(strat='F', day=x.get('date', ''), time=x.get('disp_end', ''), dir=x.get('dir', ''),
                        stage=x.get('status', ''), note='FVG %s-%s' % (x.get('fvg_lo', ''), x.get('fvg_hi', ''))))
    return res


def render_candidates():
    rows = []
    for fn in (_ab_candidates, _c_candidates, _f_candidates):
        try:
            rows += fn()
        except Exception:
            pass
    rows.sort(key=lambda x: (x.get('day', ''), x.get('time', '')), reverse=True)   # newest candidates on top
    present = {x['strat'] for x in rows}
    trs = ''
    for x in rows:
        trs += "<tr data-strat='%s'><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td class='mut'>%s</td></tr>" % (
            x['strat'], x.get('day', ''), x.get('time', ''), _chip(x['strat']), x.get('dir', ''), x.get('stage', ''), x.get('note', ''))
    body = (CSS + _NAV + "<h1>All candidates — live funnel</h1>"
            "<div class='sub'>who is armed / displaced / waiting on BOS, across strategies · " + _reach_note() + "</div>"
            + _strat_filter_bar(present) +
            "<div class='mut' style='margin-bottom:8px'>Candidates are pre-trade — most have no chart/Pine yet; they appear in <b>All trades</b> once they resolve.</div>"
            "<table><thead><tr><th>When</th><th>Strat</th><th>Dir</th><th>Stage</th><th>Note</th></tr></thead><tbody>" +
            (trs or "<tr><td colspan=5 class='mut'>no live candidates (or no service URLs set)</td></tr>") +
            "</tbody></table>"
            "<script>function selStrats(){return Array.from(document.querySelectorAll('.fstrat:checked')).map(c=>c.value);}"
            "function rebuild(){var ss=selStrats();document.querySelectorAll('tr[data-strat]').forEach(function(tr){"
            "tr.style.display=ss.indexOf(tr.getAttribute('data-strat'))>=0?'':'none';});}rebuild();</script>")
    return body


# ───────────────────────────── register ─────────────────────────────

# ───────── broker reconciliation: signals x Took? marks x real broker fills (isolated, read-only) ─────────
_BROKER_CSV   = os.path.join(_DATA_DIR, 'broker_perf.csv')
_RECON_WIN_MIN = int(os.environ.get('RECON_WINDOW_MIN', '180'))     # match fill<->signal within this many minutes
_RECON_TZ_OFF  = int(os.environ.get('RECON_TZ_OFFSET_MIN', '0'))   # minutes added to broker time to reach UTC

def _bt_ms(x):
    x = (x or '').strip()
    if not x: return None
    for f in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try: return int(dt.datetime.strptime(x, f).timestamp()*1000) + _RECON_TZ_OFF*60000
        except Exception: pass
    return None

def _parse_broker(text):
    """Tradovate 'Performance' export -> fills [{ts_ms, side, entry, exit, size, pnl}]. Falls back to a generic map."""
    import csv, io
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows: return []
    hl = [h.strip().lower() for h in rows[0]]; idx = {h: i for i, h in enumerate(hl)}
    def g(raw, k): return raw[idx[k]] if (k in idx and idx[k] < len(raw)) else ''
    perf = ('buyprice' in idx and 'sellprice' in idx and 'boughttimestamp' in idx)
    out = []
    for raw in rows[1:]:
        if perf:
            bp = _f(g(raw,'buyprice')); sp = _f(g(raw,'sellprice'))
            tb = _bt_ms(g(raw,'boughttimestamp')); td = _bt_ms(g(raw,'soldtimestamp'))
            side = ('LONG' if tb <= td else 'SHORT') if (tb is not None and td is not None) else ''
            if side == 'SHORT': entry, exit_, ems = sp, bp, td
            else:               entry, exit_, ems = bp, sp, tb
            out.append(dict(ts_ms=ems or 0, side=side, entry=entry, exit=exit_, size=_f(g(raw,'qty')), pnl=_f(g(raw,'pnl'))))
        else:
            sd = (g(raw,'side') or g(raw,'action') or '').upper()
            side = 'LONG' if ('BUY' in sd or 'LONG' in sd) else ('SHORT' if ('SELL' in sd or 'SHORT' in sd) else '')
            ts = g(raw,'timestamp') or g(raw,'time') or g(raw,'date')
            out.append(dict(ts_ms=_bt_ms(ts) or 0, side=side, entry=_f(g(raw,'price') or g(raw,'entry')),
                            exit=_f(g(raw,'exit')), size=_f(g(raw,'qty') or g(raw,'quantity')), pnl=_f(g(raw,'pnl') or g(raw,'p/l'))))
    return out

def _load_broker():
    try:
        if os.path.exists(_BROKER_CSV):
            return _parse_broker(open(_BROKER_CSV, encoding='utf-8-sig').read())
    except Exception as _e:
        print('broker parse err', _e, flush=True)
    return []

def _reconcile_data():
    sigs = _all_trades(); ann = _load_annot(); fills = _load_broker()
    for s in sigs:
        a = ann.get(_uid(s), {}); s['_taken'] = bool(a.get('taken')); s['_comment'] = a.get('comment','')
    used = [False]*len(fills)
    def match(pool):
        res = []
        for s in pool:
            best = -1; bd = None
            for i, fx in enumerate(fills):
                if used[i]: continue
                if fx['side'] and s.get('dir') and fx['side'] != s['dir']: continue
                if not fx['ts_ms'] or not s.get('ts_ms'): continue
                dd = abs(fx['ts_ms'] - s['ts_ms'])
                if dd <= _RECON_WIN_MIN*60000 and (bd is None or dd < bd): bd = dd; best = i
            if best >= 0: used[best] = True; res.append((s, fills[best]))
            else: res.append((s, None))
        return res
    taken = [s for s in sigs if s['_taken']]
    mt = match(taken)
    matched       = [(s, fx) for (s, fx) in mt if fx]
    taken_no_fill = [s for (s, fx) in mt if not fx]
    filled_unmarked = [(s, fx) for (s, fx) in match([s for s in sigs if not s['_taken']]) if fx]
    no_signal = [fills[i] for i in range(len(fills)) if not used[i]]
    return dict(sigs=sigs, taken=taken, matched=matched, taken_no_fill=taken_no_fill,
                filled_unmarked=filled_unmarked, no_signal=no_signal, fills=fills)

def _slip(s, fx):
    if s.get('entry') is None or fx.get('entry') is None or not s.get('dir'): return None
    return (fx['entry'] - s['entry']) if s['dir'] == 'LONG' else (s['entry'] - fx['entry'])   # + = worse than intended

def render_reconcile():
    import html as _h
    d = _reconcile_data()
    n_sig, n_tk, n_mt = len(d['sigs']), len(d['taken']), len(d['matched'])
    rate = (100.0*n_mt/n_tk) if n_tk else 0.0
    slips = [ _slip(s, fx) for (s, fx) in d['matched'] ]; slips = [x for x in slips if x is not None]
    avg_slip = (sum(slips)/len(slips)) if slips else 0.0
    pnl_mt = sum((fx.get('pnl') or 0) for (s, fx) in d['matched'])
    pnl_ns = sum((fx.get('pnl') or 0) for fx in d['no_signal'])
    upl = ("<form method='post' action='/all/reconcile/upload' enctype='multipart/form-data' style='margin:8px 0 14px'>"
           "<input type='file' name='csv' accept='.csv'> "
           "<button class='copy' type='submit'>Upload broker CSV (Tradovate Performance)</button>"
           "<span class='mut'> &middot; match window %d min &middot; tz offset %d min (set RECON_WINDOW_MIN / RECON_TZ_OFFSET_MIN)</span></form>") % (_RECON_WIN_MIN, _RECON_TZ_OFF)
    if not d['fills']:
        body = (CSS + _NAV + "<h1>Reconciliation - signals vs broker fills</h1>"
                "<div class='sub'>Did the trades you marked Took? actually fill, at what price, for real P&amp;L. read-only</div>"
                + upl + "<div class='bar mut'>No broker CSV loaded yet. Upload a Tradovate <b>Performance</b> export above.</div>")
        return body
    card = ("<div class='bar' style='display:flex;gap:26px;flex-wrap:wrap'>"
            "<div><div class='mut'>signals</div><b style='font-size:19px'>%d</b></div>"
            "<div><div class='mut'>marked taken</div><b style='font-size:19px'>%d</b></div>"
            "<div><div class='mut'>executed (matched)</div><b style='font-size:19px'>%d</b></div>"
            "<div><div class='mut'>execution rate</div><b style='font-size:19px'>%.0f%%</b></div>"
            "<div><div class='mut'>avg entry slippage</div><b style='font-size:19px'>%+.1f pt</b></div>"
            "<div><div class='mut'>broker P&amp;L (matched)</div><b style='font-size:19px'>%s</b></div>"
            "</div>") % (n_sig, n_tk, n_mt, rate, avg_slip, _num(round(pnl_mt,2)))
    def tbl(title, head, rows_html):
        return ("<h3>%s</h3><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                % (title, ''.join('<th>%s</th>' % h for h in head),
                   rows_html or ("<tr><td colspan=%d class='mut'>none</td></tr>" % len(head))))
    # matched
    r1 = ''
    for (s, fx) in d['matched']:
        sp = _slip(s, fx)
        r1 += ("<tr><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
               % (s.get('day',''), s.get('time',''), _chip(s.get('strat','')), s.get('dir',''),
                  _num(s.get('entry')), _num(fx.get('entry')),
                  ('%+.1f' % sp) if sp is not None else '-', _num(round(fx.get('pnl') or 0, 2))))
    # taken no fill
    r2 = ''.join("<tr><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td class='mut'>%s</td></tr>"
                 % (s.get('day',''), s.get('time',''), _chip(s.get('strat','')), s.get('dir',''), _num(s.get('entry')), _h.escape(s.get('_comment','') or ''))
                 for s in d['taken_no_fill'])
    # broker fills with no signal
    r3 = ''.join("<tr><td class='mut'>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (dt.datetime.utcfromtimestamp(fx['ts_ms']/1000).strftime('%Y-%m-%d %H:%M') if fx['ts_ms'] else '-',
                    fx.get('side',''), _num(fx.get('entry')), _num(round(fx.get('pnl') or 0, 2)))
                 for fx in d['no_signal'])
    # filled but not marked taken
    r4 = ''.join("<tr><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (s.get('day',''), s.get('time',''), _chip(s.get('strat','')), s.get('dir',''), _num(fx.get('entry')))
                 for (s, fx) in d['filled_unmarked'])
    body = (CSS + _NAV + "<h1>Reconciliation - signals vs broker fills</h1>"
            "<div class='sub'>read-only &middot; matches your Took? marks against real broker fills</div>" + upl + card
            + tbl("Executed - signal matched to a real fill (entry slippage &amp; broker P&amp;L)",
                  ['When (UTC)','Strat','Dir','Sig entry','Fill entry','Slip pt','Broker $'], r1)
            + tbl("Marked taken - but NO broker fill found (missed / never filled)",
                  ['When','Strat','Dir','Entry','Your note'], r2)
            + tbl("Broker fill - NO matching signal (manual / rogue / dropped)",
                  ['When (UTC)','Dir','Entry','$'], r3)
            + tbl("Filled but NOT marked taken (marking gap)",
                  ['When','Strat','Dir','Fill entry'], r4))
    return body


def register(app):
    try:
        from flask import Response, request
    except Exception:
        return app
    _migrate_json_once()   # bring old JSON annotations into the DB

    def _trades():
        if request.args.get('format') == 'json':
            from flask import jsonify
            _tr = _all_trades(); _an = _load_annot()
            for _t in _tr:
                _aa = _an.get(_uid(_t), {})
                _t['taken'] = bool(_aa.get('taken')); _t['comment'] = _aa.get('comment', '')
            return jsonify(trades=_tr)
        return Response(render_trades(), mimetype='text/html')

    def _annotate():
        from flask import jsonify
        d = request.get_json(force=True, silent=True) or {}
        uid = (d.get('uid') or '').strip()
        if not uid:
            return jsonify(ok=False, err='no uid'), 400
        try:
            con = _annot_conn()
            cur = con.execute('SELECT taken, comment FROM annotations WHERE uid=?', (uid,)).fetchone()
            taken = cur[0] if cur else 0
            comment = cur[1] if cur else ''
            if 'taken' in d: taken = 1 if d.get('taken') else 0
            if 'comment' in d: comment = (d.get('comment') or '').strip()
            con.execute('INSERT OR REPLACE INTO annotations(uid, taken, comment, updated_at) VALUES(?,?,?,?)',
                        (uid, taken, comment, dt.datetime.utcnow().isoformat(timespec='seconds')))
            con.commit(); con.close()
            return jsonify(ok=True, taken=bool(taken), comment=comment)
        except Exception as _e:
            print('annotate err', _e, flush=True); return jsonify(ok=False, err=str(_e)), 500

    def _cands():
        return Response(render_candidates(), mimetype='text/html')

    app.add_url_rule('/all/trades', 'all_trades', _trades)
    app.add_url_rule('/all/annotate', 'all_annotate', _annotate, methods=['POST'])

    def _reconcile_page():
        return Response(render_reconcile(), mimetype='text/html')

    def _reconcile_upload():
        from flask import redirect
        f = request.files.get('csv')
        if f is not None:
            try: f.save(_BROKER_CSV)
            except Exception as _e: print('broker save err', _e, flush=True)
        return redirect('/all/reconcile')

    app.add_url_rule('/all/reconcile', 'all_reconcile', _reconcile_page)
    app.add_url_rule('/all/reconcile/upload', 'all_reconcile_upload', _reconcile_upload, methods=['POST'])
    app.add_url_rule('/all/candidates', 'all_candidates', _cands)
    return app


if __name__ == '__main__':
    open('allview_trades_preview.html', 'w').write(render_trades())
    print('wrote allview_trades_preview.html')
