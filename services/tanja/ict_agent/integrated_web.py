"""Authenticated, same-origin, read-only views mounted inside the existing Flask app."""
import json
import sqlite3
import time
from pathlib import Path
from flask import Blueprint, Response, jsonify, request, abort, send_from_directory, redirect
from .store import Store

MODES={'replay':'HISTORICAL_REPLAY','baseline':'DETERMINISTIC_BASELINE','observe':'READ_ONLY_OBSERVATION','shadow':'SHADOW_AI','paper':'PAPER'}

def paths(cfg):
    root=Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai'
    result={k:Path(cfg.get('ICT_AI_'+k.upper()+'_DB',root/('observe.sqlite3' if k=='shadow' else k+'.sqlite3'))) for k in MODES}
    result['baseline']=result['replay']
    return result

def blueprint(cfg,authorize):
    bp=Blueprint('ict_ai_trader',__name__)
    static=Path(__file__).parent/'static'; locations=paths(cfg); cache={}
    def mode():
        value=request.args.get('mode','replay')
        if value not in MODES:abort(400,description='UNKNOWN_OR_UNAUTHORIZED_MODE')
        return value
    def store(required=True):
        path=locations[mode()]
        if not path.is_file():
            if required:abort(503,description='OBSERVER_DATABASE_NOT_CONFIGURED')
            return None
        s=object.__new__(Store);s.path=path;return s
    @bp.before_request
    def auth():
        if not authorize():return Response('Use your existing Tanja dashboard login.',401,{'WWW-Authenticate':'Basic realm="ICT AI Trader"'})
    @bp.after_request
    def secure(r):
        r.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'SAMEORIGIN',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'"})
        return r
    @bp.errorhandler(sqlite3.Error)
    def database_error(exc):return jsonify(error='DATABASE_UNAVAILABLE',reason=type(exc).__name__),503
    @bp.get('')
    @bp.get('/')
    def page():return redirect('/ict-shadow?view=ictai',code=302)
    @bp.get('/assets/<name>')
    def asset(name):
        if name not in ('app.js','native.css'):abort(404)
        return send_from_directory(static,name)
    @bp.get('/api/state')
    def state():
        m=mode();s=store(False)
        notice={'replay':'Rzeczywiste archiwalne OHLC. Dostępność przy zamknięciu jest założeniem; realizacje pozostają symulacją.',
                'baseline':'Deterministyczna analiza zapisanych danych. Reguły badawcze; bez rozumowania LLM.',
                'observe':'Obserwacja zapisanych danych feedu. Komentarz deterministyczny; brak zleceń.',
                'shadow':'Odpowiedzi LLM oznaczane są osobno. Brak odpowiedzi nie oznacza przeprowadzonej analizy AI.',
                'paper':'Wyłącznie symulacja. Brak księgi paper oznacza, że żaden forward test nie został uruchomiony.'}[m]
        body=dict(mode=MODES[m],available=s is not None,latest_id=None,orders_enabled=False,live_enabled=False,
            paid_calls_enabled=cfg.get('ICT_AI_PAID_APPROVED')=='true' and cfg.get('ICT_AI_MODE')=='SHADOW_AI',notice=notice,
            reason=None if s else 'Brak dziennika dla wybranego trybu. Wybierz replay lub skonfiguruj obserwator w tym samym serwisie.',
            capabilities=dict(order_blocks=False,breakers=False,previous_week='FIVE_COMPLETE_SESSIONS_ONLY_NEW_JOURNALS',verified_discretionary_range=False),
            freshness='NOT_AVAILABLE',worker=None)
        if s:
            now=time.time();cached=cache.get(str(s.path))
            if not cached or now-cached[0]>30:
                cached=(now,s.stats());cache[str(s.path)]=cached
            latest=s.snapshot();body.update(stats=cached[1],latest_id=latest['id'] if latest else None,
                as_of=latest['as_of'] if latest else None,freshness='HISTORICAL' if m in ('replay','baseline') else ('FRESH' if latest and 0<=now-latest['as_of']<=120 else 'STALE_OR_EMPTY'))
            if cached[1].get('manifest',{}).get('mode')=='HISTORICAL_REPLAY_WITH_ACTUAL_RECEIPTS':
                body['notice']='Odtworzenie rzeczywistych danych według zapisanych czasów odbioru. Wyłącznie dane dostępne w danym momencie; brak rzeczywistych zleceń.'
        status=Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai/worker-status.json'
        if status.is_file():
            try:body['worker']=json.loads(status.read_text())
            except (OSError,ValueError):body['worker']={'state':'STATUS_UNAVAILABLE'}
        return jsonify(body)
    @bp.get('/api/timeline')
    def timeline():
        try:before=int(request.args['before']) if 'before' in request.args else None
        except ValueError:abort(400)
        return jsonify(store().timeline(limit=160,before=before))
    @bp.get('/api/budget')
    def budget_status():
        from .budget import status
        return jsonify(status(cfg))
    @bp.get('/api/session-report')
    def session_report():
        path=Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai/session-report.json'
        if not path.is_file():return jsonify(status='NOT_COMPLETED')
        return jsonify(json.loads(path.read_text()))
    def snapshot(ident):
        s=store();value=s.snapshot(ident)
        if value is None:abort(404)
        return s,value
    def reasoning_store(s):
        path=Path(cfg.get('ICT_AI_REASONING_DB',s.path))
        if not path.is_file():return s
        target=object.__new__(Store);target.path=path;return target
    @bp.get('/api/snapshots/<ident>')
    def get_snapshot(ident):return jsonify(snapshot(ident)[1])
    @bp.get('/api/snapshots/<ident>/charts')
    def chart(ident):
        s,snap=snapshot(ident);symbol=request.args.get('symbol','NQ')
        try:limit=int(request.args.get('limit',180))
        except ValueError:abort(400)
        if symbol not in ('NQ','MNQ','ES') or not 20<=limit<=500:abort(400)
        return jsonify({str(tf):s.chart(snap,symbol,tf,limit) for tf in (1,5,15,60,240,1440)})
    @bp.get('/api/retrievals/<ident>')
    def retrieval(ident):
        with store().readonly() as db:row=db.execute('SELECT payload FROM retrievals WHERE id=?',(ident,)).fetchone()
        if row is None:abort(404)
        return jsonify(json.loads(row[0]))
    @bp.get('/api/candidates/first')
    def first():
        with store().readonly() as db:row=db.execute("SELECT id FROM snapshots WHERE decision='TRADE_CANDIDATE' ORDER BY as_of LIMIT 1").fetchone()
        return jsonify(id=row[0] if row else None)
    @bp.get('/api/snapshots/<ident>/reasoning')
    def reasoning(ident):
        s,snap=snapshot(ident)
        with reasoning_store(s).readonly() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reasoning_reviews'").fetchone():return jsonify(status='NOT_CALLED',state='NOT_CALLED')
            row=db.execute('SELECT payload FROM reasoning_reviews WHERE snapshot_id=? AND variant=? ORDER BY requested_at DESC LIMIT 1',(ident,'C_LLM_RAG')).fetchone()
        if not row:return jsonify(status='NOT_CALLED',state='NOT_CALLED')
        r=json.loads(row[0]);body={k:r.get(k) for k in ('status','model','answer','usage','elapsed_ms','error','validation','origin','requested_at','received_at','prompt_sha256','reasoning_version')}
        body['state']={'IN_FLIGHT':'RUNNING','VALIDATED_ADVISORY':'COMPLETED','REJECTED_RESPONSE':'VALIDATION_REJECTED','TRANSPORT_OUTCOME_UNKNOWN':'FAILED'}.get(r['status'],'FAILED')
        body['sources']=r.get('packet',{}).get('source_records',[])
        return jsonify(body)
    @bp.get('/api/snapshots/<ident>/reasoning-history')
    def reasoning_history(ident):
        s,snap=snapshot(ident)
        with reasoning_store(s).readonly() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reasoning_transitions'").fetchone():return jsonify(transitions=[],status='NOT_CALLED')
            rows=db.execute('SELECT payload FROM reasoning_transitions WHERE variant=? AND as_of<=? ORDER BY as_of DESC,id LIMIT 150',('C_LLM_RAG',snap['as_of'])).fetchall()
        return jsonify(transitions=[json.loads(row[0]) for row in rows],status='RECORDED_ADVISORY_TRANSITIONS')
    @bp.get('/api/evaluation')
    def evaluation():
        file=locations[mode()].parent/'evaluation.json'
        return jsonify(json.loads(file.read_text()) if file.is_file() else dict(status='NOT_EVALUATED'))
    @bp.get('/api/paper')
    def paper():
        path=Path(cfg.get('ICT_AI_PAPER_JOURNAL',Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai/paper-orders.sqlite3'))
        if not path.is_file():return jsonify(status='NOT_STARTED',orders=[],actual_orders=0)
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            orders=[json.loads(x[0]) for x in db.execute('SELECT payload FROM sim_orders')]
        return jsonify(status='SIMULATION_ONLY',orders=orders,actual_orders=0,cost_basis='EXPLICIT_PAPER_POLICY_FEES_AND_SLIPPAGE_RESERVE')
    @bp.get('/api/health')
    def health():return jsonify(component='AI_ICT_TRADER',http='OK',live_enabled=False,mode=cfg.get('ICT_AI_MODE','OFF'))
    return bp
