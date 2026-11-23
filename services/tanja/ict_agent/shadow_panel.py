"""Native presentation only; never imports the observer or knowledge index."""
from pathlib import Path
from flask import has_request_context,request
STATIC=Path(__file__).with_name('static')
NAV='<nav class="shadow-nav" aria-label="ICT research navigation"><a href="/ict-shadow?view=ictai&shadow_run=prospective">ICT Shadow · obserwator</a><a href="/ict-shadow?view=ictai&shadow_run=replay">Replay</a><a href="/ict-shadow?view=ictai&shadow_run=research">Częstotliwość i PF</a><a href="/ict-shadow?view=ictai&shadow_run=episode6">Episode 6</a><a href="/ict-shadow?view=ictai">AI ICT Trader</a><a href="/ict-shadow?view=ictknowledge">ICT Knowledge</a></nav>'
def render(fragment):
    mode=request.args.get('shadow_run') if has_request_context() else None
    if mode not in ('prospective','replay','research','episode6'):return NAV+fragment
    name,script={'research':('research-panel.html','strategy-research.js'),'episode6':('episode6-panel.html','episode6-research.js')}.get(mode,('shadow-panel.html','shadow-evidence.js'))
    route={'strategy-research.js':'research/assets/app.js','episode6-research.js':'episode6/assets/app.js','shadow-evidence.js':'assets/app.js'}[script]
    return NAV+'<link rel="stylesheet" href="/ict-shadow/shadow-assets/shadow-native.css">'+(STATIC/name).read_text()+'<script defer src="/ict-shadow/shadow-mvp/'+route+'"></script>'
