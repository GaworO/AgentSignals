"""Read-only local presentation of frozen Episode 6 qualification evidence."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Response, abort, jsonify, request

STATUSES = ('VERIFIED', 'FAILED', 'NOT_YET_OBSERVED', 'DATA_UNAVAILABLE',
            'SOURCE_AMBIGUOUS', 'CONTEXT_UNRESOLVED')

PANEL = '''<section class="panel" id="episode6-panel" hidden>
<p><a href="/ict-shadow?view=ictai&shadow_run=prospective">Obserwacja prospektywna</a> ·
<a href="/ict-shadow?view=ictai&shadow_run=research">Częstotliwość strategii i PF</a> · <b>Episode 6 — kwalifikacja</b></p>
<h2>Episode 6 SHORT · warunki źródłowe</h2>
<p class="notice"><b>HISTORYCZNE BADANIE WARUNKÓW · PENDING_LLM · BEZ ZLECEŃ</b><br>
Geometria, interpretacja źródła i gotowość wykonania są oceniane osobno. Retest świecy nie oznacza realizacji zlecenia.</p>
<p id="episode6-state">Odczyt zamrożonego wyniku…</p>
<div id="episode6-provenance"></div><div class="research-cards" id="episode6-funnel"></div>
<div id="episode6-metrics"></div>
<h3>Każdy warunek — potwierdzenia, niepowodzenia i braki</h3>
<div class="shadow-scroll" id="episode6-conditions"></div>
<details><summary>Rozdzielenie wariantów i ograniczenia pomiaru</summary><div id="episode6-limitations"></div></details>
<h3>Historyczne przypadki i dowody</h3>
<p><label for="episode6-symbol">Instrument</label> <select id="episode6-symbol"><option value="">Wszystkie</option><option>NQ</option><option>MNQ</option><option>ES</option></select>
<label for="episode6-condition">Warunek</label> <select id="episode6-condition"><option value="">Wszystkie</option></select>
<label for="episode6-status">Status warunku</label> <select id="episode6-status"><option value="">Wszystkie</option>
''' + ''.join(f'<option>{s}</option>' for s in STATUSES) + '''</select>
<button id="episode6-first">Początek</button> <button id="episode6-more">Następne 25</button> <span id="episode6-page"></span></p>
<div class="shadow-scroll"><table><thead><tr><th>Instrument / czas NY</th><th>Klasyfikacja</th><th>Dowody</th></tr></thead><tbody id="episode6-cases"></tbody></table></div>
<div id="episode6-detail"></div>
</section>'''


def register(app, runtime):
    runtime = Path(runtime).resolve()

    def directory():
        cfg = runtime / 'episode6-research.json'
        if not cfg.is_file():
            abort(503, 'EPISODE6_RESEARCH_NOT_AVAILABLE')
        root = Path(json.loads(cfg.read_text())['directory']).resolve()
        if not root.is_relative_to(runtime.parent):
            abort(503, 'INVALID_LOCAL_ARTIFACT_PATH')
        return root

    def read_json(name, limit=4_000_000):
        path = directory() / name
        if not path.is_file() and name == 'source/model.json':
            path = parent_directory() / name
        if not path.is_file():
            abort(503, 'EPISODE6_BUILD_PENDING')
        if path.stat().st_size > limit:
            abort(503, 'ARTIFACT_EXCEEDS_HTTP_BOUND')
        return json.loads(path.read_text())

    def parent_directory():
        with closing(database()) as db:
            present=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            row=db.execute("SELECT payload FROM metadata WHERE key='original_directory'").fetchone() if present else None
        if not row:abort(503,'ORIGINAL_EPISODE6_NOT_CONFIGURED')
        cfg=runtime/'episode6-research.json'
        configured=json.loads(cfg.read_text()).get('parent_directory')
        parent=Path(configured or row[0]).resolve()
        if not parent.is_relative_to(runtime.parent):abort(503,'INVALID_PARENT_ARTIFACT_PATH')
        return parent

    def database():
        path = directory() / 'qualification.sqlite'
        if not path.is_file():
            abort(503, 'EPISODE6_BUILD_PENDING')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
        return db

    base = '/ict-shadow/shadow-mvp/episode6'

    @app.get(base + '/summary', endpoint='episode6_summary')
    def summary():
        return jsonify(read_json('summary.json'))

    @app.get(base + '/model', endpoint='episode6_model')
    def model():
        return jsonify(read_json('source/model.json'))

    @app.get(base + '/cases', endpoint='episode6_cases')
    def cases():
        symbol = request.args.get('symbol', '')
        condition = request.args.get('condition', '')
        status = request.args.get('status', '')
        try:
            offset = int(request.args.get('offset', '0'))
        except ValueError:
            abort(400)
        if symbol not in ('', 'NQ', 'MNQ', 'ES') or status not in ('', *STATUSES) or not 0 <= offset <= 100_000:
            abort(400)
        with closing(database()) as db:
            if condition and not db.execute('SELECT 1 FROM conditions WHERE condition_id=? LIMIT 1', (condition,)).fetchone():
                abort(400)
            where = "(?='' OR c.symbol=?) AND ((?='' AND ?='') OR EXISTS (SELECT 1 FROM conditions t WHERE t.case_id=c.id AND (?='' OR t.condition_id=?) AND (?='' OR t.status=?)))"
            params = (symbol, symbol, condition, status, condition, condition, status, status)
            total = db.execute('SELECT count(*) FROM cases c WHERE ' + where, params).fetchone()[0]
            rows = db.execute('SELECT c.id,c.candidate_id,c.symbol,c.decision_at,c.status,c.chain_id,c.exposure_group FROM cases c WHERE ' + where + ' ORDER BY c.decision_at,c.id LIMIT 25 OFFSET ?', (*params, offset)).fetchall()
        keys = ('id', 'candidate_id', 'symbol', 'decision_at', 'status', 'chain_id', 'exposure_group')
        return jsonify(items=[dict(zip(keys, row)) for row in rows], offset=offset, total=total,
                       has_more=offset + len(rows) < total)

    @app.get(base + '/cases/<ident>', endpoint='episode6_case')
    def case(ident):
        if len(ident) > 128:
            abort(400)
        with closing(database()) as db:
            row = db.execute('SELECT payload FROM cases WHERE id=?', (ident,)).fetchone()
        if row is None:
            abort(404)
        if len(row[0]) > 4_000_000:
            abort(503, 'CASE_EXCEEDS_HTTP_BOUND')
        value=json.loads(row[0])
        if value.get('version')=='episode6-preraid-witness-v2':
            from .common import digest
            with closing(sqlite3.connect((parent_directory()/'qualification.sqlite').as_uri()+'?mode=ro',uri=True,timeout=2)) as old:
                prior=old.execute('SELECT payload FROM cases WHERE id=?',(ident,)).fetchone()
            if not prior or len(prior[0])>4_000_000:abort(503,'ORIGINAL_CASE_MISSING_OR_TOO_LARGE')
            original=json.loads(prior[0])
            if digest(original)!=value['original_case_sha256']:abort(503,'ORIGINAL_CASE_HASH_MISMATCH')
            original.update(value);value=original
            # Retain old annotation as diagnostic history, not the corrected MSS.
            for mark in value['chart']['annotations']:
                if mark['kind']=='STRUCTURE_BREAK':
                    mark['kind']='ORIGINAL_CROSS_REFERENCE';mark['status']='ORIGINAL_SELECTED_REFERENCE_UNDER_REVIEW'
            for w in value['structural_review']['witnesses']:
                value['chart']['annotations'].append({'kind':'PRE_RAID_CROSS_WITNESS',
                    'observation_id':w['id'],'available_at':w['available_at'],'ts':w['ts'],
                    'price':w['price'],'status':'GEOMETRY_ONLY_MSS_UNRESOLVED','evidence_ids':w['evidence_ids']})
        return jsonify(value)

    @app.get(base + '/assets/app.js', endpoint='episode6_script')
    def script():
        return Response(Path(__file__).with_name('static').joinpath('episode6-research.js').read_text(), mimetype='text/javascript')
