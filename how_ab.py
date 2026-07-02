#!/usr/bin/env python3
"""
how_ab.py — the A/B "how it works" page, styled like the ORB /how page.

ISOLATED add-on (same pattern as pnl.py / model_c_live.register_routes): it only adds a GET /how
route to an existing Flask app. Touches nothing else — no detector, no intake, no journal.

Wire it into agent.py with two lines (next to `pnl.register(app, ...)`):

    import how_ab
    how_ab.register(app)                     # serves the page at /how

Then add a nav link in _VIEW_NAV (optional):  <a href='/how'>how</a>
"""

def example_svg_ab():
    G, R, BL, GR = '#17864a', '#c0392b', '#1565c0', '#888'
    P = ['<svg viewBox="0 0 820 300" width="100%" xmlns="http://www.w3.org/2000/svg" '
         'style="background:#fff;border:1px solid #eee;border-radius:8px">']
    def cd(x, ytop, ybot, bull):
        col = G if bull else R
        P.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.4"/>' % (x, ytop-6, x, ybot+6, col))
        P.append('<rect x="%d" y="%d" width="11" height="%d" fill="%s"/>' % (x-5, ytop, max(ybot-ytop, 2), col))
    bars = [
        (196,206,0),(194,204,1),(198,208,0),                 # range
        (188,206,1),(166,188,1),(146,166,1),(128,146,1),     # displacement staircase (up)
        (126,140,0),(140,158,0),(150,162,1),                 # retrace into FVG, hold CE
        (140,120,1),(120,104,1),                             # BOS break up
    ]
    xs = []; x = 55
    for (yt, yb, bull) in bars:
        cd(x, yt, yb, bool(bull)); xs.append(x); x += 28
    LX = 470
    P.append('<rect x="%d" y="150" width="%d" height="16" fill="#f2b705" opacity="0.22"/>' % (xs[3]-8, (xs[9]+8)-(xs[3]-8)))
    P.append('<text x="%d" y="146" fill="#a07800" font-size="11">FVG</text>' % (xs[3]-8))
    def lvl(y, col, dash, label):
        P.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-dasharray="%s" stroke-width="1"/>' % (xs[3]-8, y, LX, y, col, dash))
        P.append('<text x="%d" y="%d" fill="%s" font-size="11.5">%s</text>' % (LX+6, y+4, col, label))
    lvl(158, R,  '5 4', 'SL = CE (holds 50%)')
    lvl(150, BL, '5 4', 'entry &#183; limit at FVG edge')
    lvl(128, G,  '4 3', 'BOS &#183; break of structure')
    lvl(104, G,  '5 4', 'TP = 2R')
    P.append('<circle cx="%d" cy="150" r="4" fill="%s"/>' % (xs[9], BL))
    for lbl, x, y in [('1', xs[5], 118), ('2', xs[8], 176), ('3', xs[11], 94), ('4', xs[9], 138)]:
        P.append('<circle cx="%d" cy="%d" r="10" fill="#22304a"/>' % (x, y))
        P.append('<text x="%d" y="%d" fill="#fff" font-size="11" text-anchor="middle" font-weight="700">%s</text>' % (x, y+4, lbl))
    P.append('<text x="52" y="30" fill="%s" font-size="12" font-weight="700">LONG (example)</text>' % G)
    P.append('<text x="52" y="288" fill="%s" font-size="11">1 displacement + FVG  &#183;  2 retrace holds 50%%  &#183;  3 BOS  &#183;  4 enter (SL = CE, TP = 2R)</text>' % GR)
    P.append('</svg>')
    return ''.join(P)


def render_how_ab():
    css = ("body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:26px;color:#222;"
           "background:#fafafa;max-width:860px} h1{font-size:21px} h3{margin-top:22px} a{color:#1565c0} "
           ".small{color:#888;font-size:13px} ol{line-height:1.7} "
           ".warn{background:#fff8e1;border:1px solid #f0d98a;border-radius:8px;padding:10px 14px;font-size:13px;margin-top:16px} "
           "table{border-collapse:collapse;font-size:13px;margin-top:6px} td,th{border-bottom:1px solid #eee;padding:5px 10px;text-align:left}")
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Strategy A/B - how it works</title><style>" + css + "</style></head><body>"
        "<p><a href='/'>&larr; back to dashboard</a></p>"
        "<h1>&#127344;&#127345; Strategy A/B - Displacement &rarr; FVG &rarr; 50% hold &rarr; BOS</h1>"
        "<div class=small>Your core <b>ICT</b> setup. A violent move breaks structure and leaves a "
        "Fair Value Gap; price pulls back into the gap, the candle bodies <b>hold the 50% line (CE)</b>, "
        "then break structure again - and you enter the pullback. A/B, C and F are the retracement family; "
        "ORB is the momentum opposite that diversifies them.</div>"
        "<h3>Example trade</h3>" + example_svg_ab() +
        "<h3>The rules, step by step</h3><ol>"
        "<li><b>Liquidity tap.</b> A draw gets hit - session H/L, PDH/PDL, PWH/PWL, equal highs/lows, "
        "the first NY-AM FVG, or a premarket H/L. That arms the search.</li>"
        "<li><b>Displacement.</b> An impulse &ge; 1.5&times; the 5-min ATR that breaks recent structure and "
        "leaves an <b>FVG</b>. Default = a run of &ge; 3 same-colour candles (single big candles tested = no net gain).</li>"
        "<li><b>Retrace holds 50%.</b> Price pulls back into the FVG; the candle <b>bodies must hold the CE</b> "
        "(the gap midpoint). A body closing through the CE kills the setup - this hold is the whole edge.</li>"
        "<li><b>BOS.</b> Price breaks the local swing formed during the retrace, in the original direction. "
        "No break of structure &rarr; no trade.</li>"
        "<li><b>Order:</b> limit entry at the fresh FVG edge - stop = the <b>displacement CE</b> (= 1R) - "
        "target = <b>2R</b> - break-even at +1R. Stops wider than 40 pts are dropped.</li>"
        "</ol>"
        "<h3>What to expect</h3>"
        "<div class=small>Broad detector (all catalysts, 4yr): <b>+0.264 R/trade</b>, +1274R, positive every year 2022-2026. "
        "Live selective config (session-filtered, realistic fills): <b>&asymp; +0.19-0.20 R/trade</b>, ~1 setup/day, "
        "~30-34% win to a 2R target. Walk-forward holds (+0.227R). Correlation with ORB ~0.10.</div>"
        "<table><tr><th>Management idea (tested this cycle)</th><th>Verdict</th></tr>"
        "<tr><td>Bias-alignment sizing</td><td>weak / not robust</td></tr>"
        "<tr><td>Single big candle stacked on</td><td>wash (+7R / 4yr)</td></tr>"
        "<tr><td>Pyramiding (add on continuation)</td><td>&minus;EV at every stop</td></tr>"
        "<tr><td>Re-entry after a stop</td><td>&minus;EV every year</td></tr></table>"
        "<div class=small style='margin-top:6px'><b>A/B is a clean one-shot trade</b> - take the signal once, "
        "protect at BE@1R, move on. No averaging in, no re-entry.</div>"
        "<div class=warn><b>In-sample.</b> Discovered on 4 years of the same data; per-year consistency is the "
        "robustness check, not a true hold-out. The live log is the number that counts - "
        "<b>Gate 0: prove &ge; +0.15R over 30-50 live trades</b> before sizing up. Not financial advice.</div>"
        "<p class=small><a href='/'>&larr; back to dashboard</a></p>"
        "</body></html>")


def register(app, path='/how'):
    """Add the GET /how route to an existing Flask app. Returns app (idempotent-ish)."""
    try:
        from flask import Response
    except Exception:
        return app
    def _how_ab():
        return Response(render_how_ab(), mimetype='text/html')
    app.add_url_rule(path, 'how_ab', _how_ab)
    return app


if __name__ == '__main__':
    open('how_ab_preview.html', 'w').write(render_how_ab())
    print('wrote how_ab_preview.html (', len(render_how_ab()), 'bytes )')
