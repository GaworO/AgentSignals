#!/usr/bin/env python3
"""
how_f.py — the Strategy F "how it works" page, styled exactly like how_ab.py / the ORB /how page.

ISOLATED add-on (same pattern as how_ab.py): it only adds a GET /how route to an existing Flask app.
Touches nothing else — no detector, no intake, no journal.

Wire it into strategy_f_live.py with two lines (inside the flask block, next to the other routes):

    import how_f
    how_f.register(app)                      # serves the F page at /how

Then in dashboard.py point the F 'How it works' tab at the F service:
    ['how','How it works',F+'/how','help']   # instead of {html:F_HOW}
"""

def example_svg_f():
    G, R, BL, GR, AM = '#17864a', '#c0392b', '#1565c0', '#888', '#a07800'
    P = ['<svg viewBox="0 0 820 300" width="100%" xmlns="http://www.w3.org/2000/svg" '
         'style="background:#fff;border:1px solid #eee;border-radius:8px">']

    def cd(x, ytop, ybot, bull):
        col = G if bull else R
        P.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.4"/>' % (x, ytop - 6, x, ybot + 6, col))
        P.append('<rect x="%d" y="%d" width="11" height="%d" fill="%s"/>' % (x - 5, ytop, max(ybot - ytop, 2), col))

    bars = [
        (196, 206, 0), (194, 204, 1), (198, 208, 0),        # opening range (the first NY-AM FVG forms here)
        (184, 200, 1),                                      # a 1-min candle CLOSES through the level
        (162, 184, 1), (144, 164, 1), (128, 148, 1),        # displacement up (leaves an FVG ~148-164)
        (128, 146, 0), (146, 158, 0), (150, 160, 1),        # first pull-back into the gap = the entry
        (120, 140, 1), (102, 120, 1),                       # continuation to 2R (no second BOS — the A/B step F drops)
    ]
    xs = []; x = 55
    for (yt, yb, bull) in bars:
        cd(x, yt, yb, bool(bull)); xs.append(x); x += 28
    LX = 470
    P.append('<rect x="%d" y="148" width="%d" height="16" fill="#f2b705" opacity="0.22"/>' % (xs[4] - 8, (xs[9] + 8) - (xs[4] - 8)))
    P.append('<text x="%d" y="144" fill="%s" font-size="11">FVG (traded gap)</text>' % (xs[9] + 12, AM))

    def lvl(y, col, dash, label):
        P.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-dasharray="%s" stroke-width="1"/>' % (xs[3] - 8, y, LX, y, col, dash))
        P.append('<text x="%d" y="%d" fill="%s" font-size="11.5">%s</text>' % (LX + 6, y + 4, col, label))
    lvl(200, GR, '4 4', 'first NY-AM FVG level')
    lvl(166, R,  '5 4', 'SL &#183; just past the gap (1R)')
    lvl(150, BL, '5 4', 'entry &#183; near edge (first touch)')
    lvl(104, G,  '5 4', 'TP = 2R')
    P.append('<circle cx="%d" cy="150" r="4" fill="%s"/>' % (xs[8], BL))
    for lbl, x, y in [('1', xs[3], 192), ('2', xs[6], 134), ('3', xs[8], 152), ('4', xs[11], 108)]:
        P.append('<circle cx="%d" cy="%d" r="10" fill="#22304a"/>' % (x, y))
        P.append('<text x="%d" y="%d" fill="#fff" font-size="11" text-anchor="middle" font-weight="700">%s</text>' % (x, y + 4, lbl))
    P.append('<text x="52" y="30" fill="%s" font-size="12" font-weight="700">LONG (example)</text>' % G)
    P.append('<text x="52" y="288" fill="%s" font-size="11">1 first NY-AM FVG (level)  &#183;  2 displacement leaves FVG  &#183;  3 first touch = entry  &#183;  4 TP 2R (SL past gap, BE @ +1R)</text>' % GR)
    P.append('</svg>')
    return ''.join(P)


def render_how_f():
    css = ("body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:26px;color:#222;"
           "background:#fafafa;max-width:860px} h1{font-size:21px} h3{margin-top:22px} a{color:#1565c0} "
           ".small{color:#888;font-size:13px} ol{line-height:1.7} "
           ".warn{background:#fff8e1;border:1px solid #f0d98a;border-radius:8px;padding:10px 14px;font-size:13px;margin-top:16px} "
           "table{border-collapse:collapse;font-size:13px;margin-top:6px} td,th{border-bottom:1px solid #eee;padding:5px 10px;text-align:left}")
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Strategy F - how it works</title><style>" + css + "</style></head><body>"
        "<p><a href='/candidates'>candidates</a> &#183; <a href='/log'>log</a> &#183; <a href='/performance_f'>performance</a></p>"
        "<h1>&#127349; Strategy F - Displacement &rarr; FVG &rarr; first touch</h1>"
        "<div class=small>A momentum-continuation cousin of A/B. The first strong NY-AM move leaves a "
        "Fair Value Gap; instead of waiting for a second break of structure (A/B), F enters the "
        "<b>first pullback</b> into that gap, in the direction of the move, for 2R. Same ICT engine, but "
        "the tested edge is <b>momentum, not the ICT narrative</b> - so it is the same family as A/B/C "
        "(correlated), not a diversifier like ORB.</div>"
        "<h3>Example trade</h3>" + example_svg_f() +
        "<h3>The rules, step by step</h3><ol>"
        "<li><b>Catalyst.</b> The <b>first Fair Value Gap of the NY-AM session</b> (09:30-11:59 ET) - a "
        "3-candle gap. Its edges become the level to watch.</li>"
        "<li><b>Continuation break.</b> A 1-min candle <b>closes through</b> that level (a close, not a "
        "wick) - this arms the setup in that direction.</li>"
        "<li><b>Displacement.</b> An impulse &ge; 1.5&times; the 5-min ATR that breaks the prior 15-bar "
        "structure (a run of &ge; 3 same-colour candles) and <b>leaves its own FVG</b> - that fresh gap is "
        "what you trade.</li>"
        "<li><b>First touch = entry.</b> A limit at the <b>near edge</b> of that gap fills on the first "
        "pullback. No second break-of-structure is required (that is the difference from A/B). The setup is "
        "<b>cancelled if a candle body closes back through the gap</b> before the fill.</li>"
        "<li><b>Order:</b> limit at the fresh FVG edge - stop <b>just past the gap</b> (~14pt = 1R) - "
        "target = <b>2R</b> - break-even at +1R. Stops wider than 40 pts are dropped. <b>Continuation "
        "only</b>, first setup of the day.</li>"
        "</ol>"
        "<h3>What to expect</h3>"
        "<div class=small>Honest cut (first-continuation-of-day, near-edge entry, realistic 1-tick fill): "
        "<b>+0.42 R/trade</b> (band +0.33 to +0.48), ~160 trades/yr, ~<b>37% win</b> to a 2R target, "
        "<b>positive every year</b> 2022-2026 (+0.38 / +0.50 / +0.40 / +0.37R). Out-of-sample (14 June-2026 "
        "sessions): nothing broke, but only 4 fills - not yet proof. Correlation with A/B: high (same family).</div>"
        "<table><tr><th>Idea (tested this cycle)</th><th>Verdict</th></tr>"
        "<tr><td>Higher-timeframe bias filter</td><td>minor / not a lever</td></tr>"
        "<tr><td>Liquidity-pool target (vs fixed 2R)</td><td>worse than 2R</td></tr>"
        "<tr><td>A/B liquidity catalyst under the leg</td><td>worse, not better</td></tr>"
        "<tr><td>Sweep-and-reclaim before the leg</td><td>worse at every lookback</td></tr>"
        "<tr><td>Reversal direction (fade the level)</td><td>dead (+0.07R) &rarr; dropped</td></tr>"
        "<tr><td>Deeper retrace / 50%-of-leg entry</td><td>dead</td></tr></table>"
        "<div class=small style='margin-top:6px'><b>F is a clean one-shot momentum trade</b> - the ICT "
        "confluence you would expect to help does not. Trade the clean leg, protect at BE@1R, move on. "
        "No averaging in, no re-entry.</div>"
        "<div class=warn><b>In-sample.</b> Discovered on 4 years of the same data; per-year consistency is "
        "the robustness check, not a true hold-out. The only real fragility is <b>fill quality</b> on the "
        "~14pt stop, and by design it <b>misses no-retrace runner days</b>. <b>Gate 0: prove &ge; +0.15R "
        "over 30-50 live trades</b> before sizing up. Not financial advice.</div>"
        "<p class=small><a href='/candidates'>&larr; candidates</a></p>"
        "</body></html>")


def register(app, path='/how'):
    """Add the GET /how route to an existing Flask app. Returns app."""
    try:
        from flask import Response
    except Exception:
        return app
    def _how_f():
        return Response(render_how_f(), mimetype='text/html')
    app.add_url_rule(path, 'how_f', _how_f)
    return app


if __name__ == '__main__':
    open('how_f_preview.html', 'w').write(render_how_f())
    print('wrote how_f_preview.html (', len(render_how_f()), 'bytes )')
