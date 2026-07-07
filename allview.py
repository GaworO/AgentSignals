#!/usr/bin/env python3
"""
allview.py — JOINED "All trades" + "All candidates" across strategies A/B, C, F, ORB.

Isolated add-on (same pattern as pnl.py / forex_pnl.py): adds read-only routes on the MAIN agent:
    /all/trades      — every strategy's modeled trades in one table + a PER-DAY Pine export
    /all/candidates  — every strategy's live candidates in one table

It NEVER touches detector logic. A/B is read locally (outcomes.json + signals key for /chart);
C / F / ORB are fetched over HTTP from their own services (server-side, no CORS) using env URLs:

    STRAT_C_URL   = https://<model-c service>        (reads  <url>/journal , <url>/candidates)
    STRAT_F_URL   = https://<strategy-f service>     (reads  <url>/performance_f , <url>/candidates?format=json)
    STRAT_ORB_URL = https://<orb service>            (reads  <url>/trades , <url>/api/state)

Any URL left unset is skipped — the view degrades gracefully to whatever is reachable.

Wire into agent.py (next to the other registers):
    import allview ; allview.register(app)
"""
import os, json, datetime as dt

try:
    import requests
except Exception:
    requests = None
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except Exception:
    _ET = None

# ---- where A/B keeps its resolved outcomes (same file manage.py writes) ----
_DATA_DIR = os.environ.get('DATA_DIR', '/data') or '.'
_OUTCOMES = os.path.join(_DATA_DIR, 'outcomes.json')

# strategy -> (Pine color, HTML chip color)
STRAT_COLORS = {
    'AB':  ('color.aqua',    '#22d3ee'),
    'C':   ('color.lime',    '#4ade80'),
    'F':   ('color.orange',  '#f59e0b'),
    'ORB': ('color.fuchsia', '#c084fc'),
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


def _orb_ms(date, brk):
    try:
        h, m = str(brk).split(':')
        d = dt.date.fromisoformat(str(date))
        naive = dt.datetime(d.year, d.month, d.day, int(h), int(m))
        if _ET is not None:
            return int(naive.replace(tzinfo=_ET).timestamp() * 1000)
        return int(naive.timestamp() * 1000)          # fallback: no tz (approx)
    except Exception:
        return 0


def _orb_trades():
    j = _get(os.environ.get('STRAT_ORB_URL', ''), '/trades')
    if not isinstance(j, list):
        return []
    res = []
    for t in j:
        if not isinstance(t, dict):
            continue
        e = _f(t.get('entry')); sl = _f(t.get('SL', t.get('sl')))
        if e is None or sl is None:
            continue
        ts = _orb_ms(t.get('date'), t.get('brk_time'))
        res.append(_norm('ORB', ts, t.get('dir'), 'ORB', e, sl, t.get('R'),
                         t.get('result', t.get('status'))))
    return res


def _all_trades():
    out = []
    for fn in (_ab_trades, _c_trades, _f_trades, _orb_trades):
        try:
            out += fn()
        except Exception:
            pass
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
    order = [s for s in ('AB', 'C', 'F', 'ORB') if s in present]
    boxes = ''.join(
        "<label style='display:inline-flex;align-items:center;gap:5px'>"
        "<input type='checkbox' class='fstrat' value='%s' checked onchange='rebuild()'>%s</label>"
        % (s, _chip(s)) for s in order)
    return ("<div class='bar'><b>Strategy:</b>" + boxes +
            "<a href='#' onclick=\"document.querySelectorAll('.fstrat').forEach(c=>c.checked=true);rebuild();return false\">all</a>"
            "<a href='#' onclick=\"document.querySelectorAll('.fstrat').forEach(c=>c.checked=false);rebuild();return false\">none</a></div>")


def render_trades():
    import html as _h
    rows = _all_trades()
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
        trs += ("<tr data-strat='%s'><td class='mut'>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s<td>%s</td><td>%s</td></tr>") % (
            r['strat'], r['day'], r['time'], _chip(r['strat']), r['dir'], r['cat'], _num(r['entry']), _num(r['sl']),
            _rcell(r['r']), chart, pine)

    body = (CSS +
            "<h1>All trades — A/B · C · F · ORB</h1>"
            "<div class='sub'>modeled outcomes across every strategy · read-only · " + _reach_note() + "</div>"
            + _strat_filter_bar(present) +
            "<div class='bar'><b>Per-day Pine for TradingView:</b>"
            "<select id='daysel' onchange='rebuild()'>" + opts + "</select>"
            "<button class='copy' onclick=\"navigator.clipboard.writeText(document.getElementById('psbox').value);"
            "this.textContent='Copied ✓';setTimeout(()=>this.textContent='Copy day script',1200)\">Copy day script</button>"
            "<span class='mut'>obeys the strategy filter · pick a day → copy → TradingView → Pine Editor → paste → Add to chart</span></div>"
            "<textarea id='psbox' readonly></textarea>"
            "<table><thead><tr><th>When (UTC)</th><th>Strat</th><th>Dir</th><th>Cat</th><th>Entry</th><th>SL</th>"
            "<th>Result</th><th>Chart</th><th>Pine</th></tr></thead><tbody>" + (trs or
            "<tr><td colspan=9 class='mut'>no trades yet (or no service URLs set)</td></tr>") + "</tbody></table>"
            "<script>var TRADES=" + json.dumps(js_trades) + ";"
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
    for lbl, env in (('C', 'STRAT_C_URL'), ('F', 'STRAT_F_URL'), ('ORB', 'STRAT_ORB_URL')):
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
    body = (CSS + "<h1>All candidates — live funnel</h1>"
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
def register(app):
    try:
        from flask import Response, request
    except Exception:
        return app

    def _trades():
        if request.args.get('format') == 'json':
            from flask import jsonify
            return jsonify(trades=_all_trades())
        return Response(render_trades(), mimetype='text/html')

    def _cands():
        return Response(render_candidates(), mimetype='text/html')

    app.add_url_rule('/all/trades', 'all_trades', _trades)
    app.add_url_rule('/all/candidates', 'all_candidates', _cands)
    return app


if __name__ == '__main__':
    open('allview_trades_preview.html', 'w').write(render_trades())
    print('wrote allview_trades_preview.html')
