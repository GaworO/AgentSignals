"""Bounded, local, read-only presentation of versioned frequency research.

No detector/observer imports, journal mutation, providers or order transports.
"""
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from flask import Response, abort, jsonify, request


PANEL = '''<section class="panel" id="strategy-research-panel" hidden>
<p><a href="/ict-shadow?view=ictai&shadow_run=prospective">Obserwacja prospektywna</a> ·
<a href="/ict-shadow?view=ictai&ai_mode=replay">Archiwalny replay</a> · <b>Częstotliwość strategii i PF</b> ·
<a href="/ict-shadow?view=ictai&shadow_run=episode6">Episode 6 — kwalifikacja</a></p>
<h2>ICT · częstotliwość i kwalifikacja strategii</h2>
<p class="notice"><b>BADANIE HISTORYCZNE · BRAK REALIZACJI ZLECEŃ · PENDING_LLM</b><br>
Wzorce i hipotezy nie są pełnymi strategiami. Wspólne zdarzenia mogą występować w kilku rodzinach — wierszy nie należy sumować.</p>
<p id="strategy-research-state">Odczyt wersjonowanego pomiaru…</p>
<div id="strategy-research-totals" class="research-cards"></div>
<div id="strategy-research-coverage"></div>
<p><label>Rodzina <select id="research-family"><option value="">Wszystkie</option></select></label>
<label>Typ <select id="research-kind"><option value="">Wszystkie</option></select></label>
<button id="research-refresh">Odśwież pomiar</button></p>
<p><b>— oznacza nieocenione lub nieustalone, a nie zero.</b> „Kwalifikacje” to liczba wykazanych kwalifikacji źródłowych.
PF_NOT_ESTIMABLE oznacza brak podstaw do symulacji, nie stratną strategię.</p>
<p>Zdarzenia składowe to np. osobno sweep, cross i FVG — nie liczba wystąpień kompletnego modelu.
Łańcuch „do FVG” nie potwierdza retestu ani źródłowego MSS.</p>
<div class="shadow-scroll"><table><thead><tr><th>Model / wariant</th><th>Zdarzenia składowe</th><th>Kandydaci selektora</th><th>Łańcuchy do FVG</th><th>Kwalifikacje</th><th>Dopuszczone do symulacji</th><th>PF / win rate / średnie R / max DD</th></tr></thead><tbody id="research-models"></tbody></table></div>
<div id="research-model-detail"></div>
<details><summary>Wszystkie źródłowe programy w odzyskanej bibliotece</summary><div id="research-source-programs"></div></details>
<details><summary>Ograniczenia, źródła i protokół</summary><div id="research-limitations"></div></details>
<p><a href="/ict-shadow/shadow-mvp/research/summary" target="_blank">Wersjonowany wynik JSON</a> ·
<a href="/ict-shadow/shadow-mvp/research/prospective-report" target="_blank">Osobny raport sesji prospektywnej</a></p>
</section>'''


def register(app, runtime):
    runtime = Path(runtime).resolve()
    report_cache = {'at': 0, 'value': None}
    report_lock = threading.Lock()

    def directory():
        cfg = runtime / 'strategy-research.json'
        if not cfg.is_file():
            abort(503, 'RESEARCH_NOT_AVAILABLE')
        p = Path(json.loads(cfg.read_text())['directory']).resolve()
        if not p.is_relative_to(runtime.parent):
            abort(503, 'INVALID_LOCAL_ARTIFACT_PATH')
        return p

    def read_json(relative, limit=4_000_000):
        p = directory() / relative
        if not p.is_file():
            abort(503, 'RESEARCH_BUILD_PENDING')
        if p.stat().st_size > limit:
            abort(503, 'ARTIFACT_EXCEEDS_HTTP_BOUND')
        return json.loads(p.read_text())

    base = '/ict-shadow/shadow-mvp/research'

    @app.get(base + '/summary')
    def research_summary():
        return jsonify(read_json('summary.json'))

    @app.get(base + '/sources')
    def research_sources():
        # Source review is a bounded artifact, never the entire corpus/index.
        return jsonify(read_json('source/taxonomy.json'))

    @app.get(base + '/m1-review')
    def research_m1_review():
        return jsonify(read_json('source/initial-11-m1-sweeps.json'))

    @app.get(base + '/source/<ident>')
    def research_source(ident):
        for item in read_json('source/source-evidence.json')['rules']:
            if item['id'] == ident:
                return jsonify(item)
        abort(404)

    @app.get(base + '/prospective-report')
    def prospective_report():
        return jsonify(read_json('prospective/latest.json'))

    @app.get(base + '/prospective-current')
    def prospective_current():
        from .shadow_session_report import collect_report
        with report_lock:
            if report_cache['value'] is None or time.monotonic() - report_cache['at'] > 30:
                cfg = runtime / 'prospective-run.json'
                if not cfg.is_file():
                    abort(503)
                run = Path(json.loads(cfg.read_text())['directory']).resolve()
                if not run.is_relative_to(runtime.parent):
                    abort(503)
                try:
                    value = collect_report(run)
                except (sqlite3.Error, OSError, ValueError, KeyError):
                    abort(503, 'PROSPECTIVE_REPORT_READ_FAILURE')
                # HTTP never writes checkpoints or starts a feed worker.
                report_cache.update(value=value, at=time.monotonic())
        return jsonify(report_cache['value'])

    @app.get(base + '/occurrences')
    def research_occurrences():
        model = request.args.get('model', '')
        symbol = request.args.get('symbol', '')
        try:
            offset = int(request.args.get('offset', '0'))
        except ValueError:
            abort(400)
        summary = read_json('summary.json')
        if model not in {x['id'] for x in summary['models']} or symbol not in ('', 'NQ', 'MNQ', 'ES') or not 0 <= offset <= 1_000_000:
            abort(400)
        p = directory() / 'research.sqlite'
        if not p.is_file():
            abort(503)
        with closing(sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('''SELECT o.entity_kind,o.entity_id,o.screen_status,o.reason,
                coalesce(e.symbol,c.symbol),coalesce(e.at,c.created_at),
                coalesce(e.session,c.session),coalesce(e.direction,c.direction)
                FROM model_occurrences o
                LEFT JOIN events e ON o.entity_kind='event' AND e.id=o.entity_id
                LEFT JOIN candidates c ON o.entity_kind='candidate' AND c.id=o.entity_id
                WHERE o.model_id=? AND (?='' OR coalesce(e.symbol,c.symbol)=?)
                ORDER BY coalesce(e.at,c.created_at),o.entity_id LIMIT 51 OFFSET ?''',
                (model, symbol, symbol, offset)).fetchall()
        keys = ('entity_kind', 'id', 'status', 'reason', 'symbol', 'at', 'session', 'direction')
        return jsonify(items=[dict(zip(keys, r)) for r in rows[:50]], has_more=len(rows) > 50, offset=offset)

    @app.get(base + '/assets/app.js')
    def research_script():
        return Response(Path(__file__).with_name('static').joinpath('strategy-research.js').read_text(), mimetype='text/javascript')
