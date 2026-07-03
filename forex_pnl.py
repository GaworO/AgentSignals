#!/usr/bin/env python3
"""
forex_pnl.py — JOINED forex-only P&L (EUR/USD + USD/JPY), observe-only.

Isolated add-on (same pattern as pnl.py / dashboard.py): adds ONE route /forexpnl on the MAIN agent.
It fetches each forex service's /performance server-side (no CORS) and shows a combined forex summary.
It does NOT read or touch the MNQ /pnl or journal.db — futures P&L is untouched.

Wire into agent.py (next to pnl.register / dashboard.register):
    import forex_pnl ; forex_pnl.register(app)

Env (on the main agent): FX_EUR_URL, FX_JPY_URL  (public URLs of the two forex services).
"""
import os
try:
    import requests
except Exception:
    requests = None

PAIRS = [('EUR/USD', os.environ.get('FX_EUR_URL', '')),
         ('USD/JPY', os.environ.get('FX_JPY_URL', ''))]

CSS = ("<style>body{background:#0a0a0a;color:#ebebeb;font-family:system-ui,sans-serif;margin:0;padding:16px}"
       "h1{font-size:18px;margin:0 0 2px}.sub{color:#555;font:11px monospace;margin-bottom:14px}"
       ".cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}"
       ".card{background:#141414;border:1px solid #222;border-radius:10px;padding:14px 18px;min-width:150px}"
       ".card .lbl{color:#6b7688;font:11px monospace;text-transform:uppercase}"
       ".card .val{font-size:26px;font-weight:700;margin-top:4px}"
       "table{border-collapse:collapse;width:100%;font:13px system-ui}"
       "th{background:#1c1c1c;color:#666;text-align:left;padding:7px 9px}"
       "td{padding:7px 9px;border-top:1px solid #1a1a1a}"
       ".g{color:#4ade80}.r{color:#f87171}.mut{color:#8a8a8a;font:12px monospace}</style>")

def _perf(url):
    if not url or requests is None:
        return None
    try:
        r = requests.get(url.rstrip('/') + '/performance', timeout=12)
        return r.json().get('live_all', {})
    except Exception:
        return None

def _cls(x):
    return 'g' if (isinstance(x, (int, float)) and x > 0) else ('r' if (isinstance(x, (int, float)) and x < 0) else '')

def render():
    rows = []; tot_n = 0; tot_R = 0.0; tot_w = 0
    for name, url in PAIRS:
        la = _perf(url)
        if la is None:
            rows.append((name, None)); continue
        n = la.get('n', 0); R = float(la.get('total_R', 0.0)); exp = float(la.get('exp_R', 0.0)); wp = float(la.get('win_pct', 0.0))
        tot_n += n; tot_R += R; tot_w += round(wp / 100.0 * n)
        rows.append((name, dict(n=n, R=R, exp=exp, wp=wp)))
    cexp = round(tot_R / tot_n, 3) if tot_n else 0.0
    cwp = round(100 * tot_w / tot_n, 1) if tot_n else 0.0
    cards = (f"<div class='card'><div class='lbl'>Forex trades</div><div class='val'>{tot_n}</div></div>"
             f"<div class='card'><div class='lbl'>Total R (would-be P&amp;L)</div><div class='val {_cls(tot_R)}'>{tot_R:+.1f}R</div></div>"
             f"<div class='card'><div class='lbl'>Exp / trade</div><div class='val {_cls(cexp)}'>{cexp:+.3f}R</div></div>"
             f"<div class='card'><div class='lbl'>Win rate</div><div class='val'>{cwp}%</div></div>")
    trs = ""
    for name, d in rows:
        if d is None:
            trs += f"<tr><td>{name}</td><td colspan=4 class='mut'>service unreachable — check FX_*_URL / service up</td></tr>"
        else:
            trs += (f"<tr><td>{name}</td><td>{d['n']}</td><td class='{_cls(d['exp'])}'>{d['exp']:+.3f}R</td>"
                    f"<td>{d['wp']}%</td><td class='{_cls(d['R'])}'>{d['R']:+.1f}R</td></tr>")
    return (f"<!doctype html><html><head><meta charset=utf-8><title>Forex P&amp;L</title>{CSS}</head><body>"
            f"<h1>Forex P&amp;L — joined (observe only)</h1>"
            f"<div class='sub'>EUR/USD + USD/JPY · modeled from confirmed setups (win +2R / BE 0 / loss −1R) · separate from MNQ</div>"
            f"<div class='cards'>{cards}</div>"
            f"<table><thead><tr><th>Pair</th><th>Trades</th><th>Exp/trade</th><th>Win%</th><th>Total R</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>"
            f"<div class='mut' style='margin-top:12px'>Total R × your risk-per-trade = would-be money. "
            f"n counts only resolved setups (TP/SL/timeout); fresh services show 0 until trades close.</div>"
            f"</body></html>")

def register(app, path='/forexpnl'):
    try:
        from flask import Response
    except Exception:
        return app
    def _p():
        return Response(render(), mimetype='text/html')
    app.add_url_rule(path, 'forex_pnl', _p)
    return app

if __name__ == '__main__':
    open('forexpnl_preview.html', 'w').write(render())
    print('wrote forexpnl_preview.html')
