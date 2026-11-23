"""Existing GET-only shadow views, mounted ONLY in the isolated worker API."""
import json,time
from contextlib import contextmanager
from pathlib import Path
from flask import abort, jsonify, request, Response
from .store import Store

from .strategy_research_web import register as register_research
from .episode6_web import register as register_episode6

class ReadStore(Store):
    """HTTP connections close at request completion, not eventual garbage collection."""
    @contextmanager
    def readonly(self):
        db=super().readonly()
        try:
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:db.close()

    def connect(self):raise ValueError('HTTP_STORE_IS_READ_ONLY')

def register(app,database,runtime):
    runtime=Path(runtime).resolve()
    store=object.__new__(ReadStore);store.path=Path(database).resolve()
    def prospective_paths():
        config=runtime/'prospective-run.json'
        if not config.is_file():return None
        root=Path(json.loads(config.read_text())['directory']).resolve()
        if not root.is_relative_to(runtime.resolve().parent):raise ValueError('LOCAL_RUN_OUTSIDE_WORKSPACE')
        return root
    def selected_store():
        if request.args.get('run')!='prospective':return store
        root=prospective_paths()
        if root is None or not (root/'observer.sqlite3').is_file():abort(503)
        s=object.__new__(ReadStore);s.path=root/'observer.sqlite3';return s
    def operational_status():
        root=prospective_paths()
        if root and (root/'status.json').exists():
            p=json.loads((root/'status.json').read_text());p.pop('pid',None);p.pop('candidate_ids',None)
            p['enabled']=p['operational_state']=='PROSPECTIVE_SHADOW_RUNNING' and time.time()-p['heartbeat_at']<75 and time.time()<p['stop_at']
            if p['operational_state']=='PROSPECTIVE_SHADOW_RUNNING' and not p['enabled']:p['operational_state']='BLOCKED_STALE_HEARTBEAT_OR_EXPIRED'
            p['classification']=p['feed_classification'];p['blockers']=[] if p['enabled'] else [p['operational_state']]
            p['explanation']='Nowe decyzje zapisane przed następną świecą. Catch-up wykluczony z liczby kandydatów prospektywnych. CME entitlement niezweryfikowane; badanie pasywne.'
            incidents=runtime/'observer-incidents.json'
            if incidents.is_file() and incidents.stat().st_size<100000:
                p['observer_incidents']=json.loads(incidents.read_text())
            return p
        return None
    register_research(app, runtime)
    register_episode6(app, runtime)
    @app.get('/ict-shadow/shadow-mvp/assets/app.js')
    def script():return Response(Path(__file__).with_name('static').joinpath('shadow-evidence.js').read_text(),mimetype='text/javascript')
    @app.get('/ict-shadow/shadow-mvp/feed-status')
    def feed_status():
        file=runtime/'feed-status.json'
        return jsonify(json.loads(file.read_text()) if file.exists() else {'status':'LIVE_NOT_VERIFIED'})
    @app.get('/ict-shadow/shadow-mvp/prospective-status')
    def prospective_status():
        current=operational_status()
        if current:return jsonify(current)
        file=runtime/'prospective-status.json'
        return jsonify(json.loads(file.read_text()) if file.exists() else {
            'enabled':False,'classification':'UNVERIFIED','prospective_candidates':0,
            'blockers':['FEED_ENTITLEMENT_NOT_VERIFIED'],'llm_status':'PENDING_LLM'})
    @app.get('/ict-shadow/shadow-mvp/status')
    def status():
        if request.args.get('run')=='prospective':
            p=operational_status()
            if not p:abort(503)
            return jsonify(mode='PROSPECTIVE_SHADOW',session=p['operational_state'],current_as_of=(p.get('last_bar_start') or 0)+60,candidates=p.get('prospective_candidates',0),candidates_by_instrument=p.get('candidate_counts',{}),decisions=p.get('decisions',{}),outcome_records=p.get('prospective_outcomes',0),source_qualified=0,openai_calls=0,broker_orders=0,catchup_candidates=p.get('catchup_candidates',0))
        with store.readonly() as db:
            latest="WITH last AS (SELECT candidate_id,max(as_of) at FROM shadow_records WHERE stage='ASSESSMENT' GROUP BY candidate_id) SELECT r.payload FROM last l JOIN shadow_records r ON r.candidate_id=l.candidate_id AND r.as_of=l.at AND r.stage='ASSESSMENT'"
            decisions=dict(db.execute("SELECT json_extract(payload,'$.decision'),count(*) FROM ("+latest+") GROUP BY 1"))
            manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
            r=dict(mode='SIMULATED_REPLAY',session=manifest.get('session'),current_as_of=db.execute('SELECT max(as_of) FROM batches').fetchone()[0],candidates=sum(decisions.values()),decisions=decisions,outcome_records=db.execute("SELECT count(*) FROM shadow_records WHERE stage IN ('OUTCOME','SESSION_END')").fetchone()[0],source_qualified=0,openai_calls=0,broker_orders=0)
        return jsonify(r)
    @app.get('/ict-shadow/shadow-mvp/candidates')
    def candidates():
        selected=selected_store();prospective=request.args.get('run')=='prospective'
        try:offset=int(request.args.get('offset',0))
        except ValueError:abort(400)
        if not 0<=offset<=10000:abort(400)
        symbol=request.args.get('symbol','');session=request.args.get('session','')
        if symbol not in ('','NQ','MNQ','ES') or session not in ('','LO','NYAM','NYPM','BETWEEN_WINDOWS'):abort(400)
        with selected.readonly() as db:
            clause=" AND candidate_id IN (SELECT d.candidate_id FROM shadow_records d JOIN passive_log p ON p.key=json_extract(d.payload,'$.persistence_batch') AND p.kind='DURABLE' WHERE d.stage='DETECTED' AND json_extract(p.payload,'$.prospective_timing_verified')=1)" if prospective else ''
            rows=db.execute("SELECT candidate_id,min(as_of),max(as_of) FROM shadow_records WHERE stage='ASSESSMENT' AND (?='' OR json_extract(payload,'$.symbol')=?) AND (?='' OR json_extract(payload,'$.session')=?)"+clause+" GROUP BY candidate_id ORDER BY min(as_of),candidate_id LIMIT 51 OFFSET ?",(symbol,symbol,session,session,offset)).fetchall()
            items=[]
            for ident,created,latest in rows[:50]:
                value=json.loads(db.execute("SELECT payload FROM shadow_records WHERE candidate_id=? AND as_of=? AND stage='ASSESSMENT'",(ident,latest)).fetchone()[0]);value['created_at']=created;items.append(value)
        return jsonify(items=items,has_more=len(rows)>50)
    @app.get('/ict-shadow/shadow-mvp/events')
    def observed_events():
        with selected_store().readonly() as db:
            manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
            start=manifest.get('prospective_after_bar_start',0)+60
            rows=[json.loads(p) for p, in db.execute("SELECT payload FROM events WHERE as_of>=? AND kind IN ('SWEEP','FVG','IFVG','STRUCTURE_BREAK','DISPLACEMENT') ORDER BY as_of DESC LIMIT 30",(start,))]
        for e in rows:
            e['selection_status']='M1_REFERENCE_BELOW_RESEARCH_SELECTOR_MINIMUM_M5' if e['kind']=='SWEEP' and max(e['timeframe'],e.get('level_timeframe',1))<5 else 'OBSERVATION_NOT_SOURCE_QUALIFICATION'
        return jsonify(items=rows)
    @app.get('/ict-shadow/shadow-mvp/events/<ident>')
    def observed_event(ident):
        from .ai import packet as make_packet
        selected=selected_store()
        with selected.readonly() as db:
            row=db.execute('SELECT payload FROM events WHERE id=?',(ident,)).fetchone()
            if row is None:abort(404)
            e=json.loads(row[0])
        snap={'as_of':e['available_at'],'events':[e],'setups':[],'retrieval_ids':[],'context':{e['symbol']:{'contract':e['contract'],'active_levels':[],'active_zones':[],'ranges':{}}}}
        p=make_packet(selected,snap)
        annotations=[]
        for item in p['evidence'].values():
            if item['id']==ident or item.get('kind')=='PIVOT':
                annotations.append(dict(observation_id=item['id'],kind=e['kind'] if item['id']==ident else 'LIQUIDITY_REFERENCE',ts=item['ts'],available_at=item['available_at'],price=item.get('price'),lower=item.get('lower'),upper=item.get('upper'),direction=item.get('direction'),evidence_ids=item.get('evidence_ids',[]),status='DETECTED_GEOMETRY_ONLY_NOT_A_QUALIFIED_SETUP'))
        chart={'symbol':e['symbol'],'as_of':e['available_at'],'frames':{'1':selected.chart(snap,e['symbol'],1,150)},'annotations':annotations}
        return jsonify(event=e,evidence=p['evidence'],chart=chart,qualification=False,interpretation='PENDING_LLM')
    @app.get('/ict-shadow/shadow-mvp/candidates/<ident>')
    def detail(ident):
        with selected_store().readonly() as db:
            rows=db.execute('SELECT payload FROM shadow_records WHERE candidate_id=? AND length(payload)<=100000 ORDER BY as_of,rowid LIMIT 1001',(ident,)).fetchall()
            if len(rows)>1000:abort(413,'CANDIDATE_HISTORY_EXCEEDS_READ_BOUND')
            if not rows:abort(404)
            records=[json.loads(r[0]) for r in rows];latest=[r for r in records if r['stage']=='ASSESSMENT'][-1]
            at=request.args.get('at')
            if at is not None:
                try:wanted=int(at)
                except ValueError:abort(400)
                matching=[r for r in records if r['stage']=='ASSESSMENT' and r['as_of']==wanted]
                if not matching:abort(404)
                latest=matching[0]
            row=db.execute('SELECT payload FROM shadow_evidence WHERE id=? AND length(payload)<=3500000',(latest['evidence_packet_id'],)).fetchone()
            if not row:abort(413,'EVIDENCE_EXCEEDS_READ_BOUND')
            packet=json.loads(row[0])
            durability={}
            if request.args.get('run')=='prospective':
                for r in records:
                    key=r.get('persistence_batch')
                    found=db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE' AND key=?",(key,)).fetchone()
                    if found:durability[key]=json.loads(found[0])
        # Only annotation ancestors, loaded for one selected saved assessment.
        refs=set()
        for item in packet.get('chart',{}).get('annotations',[]):
            refs.update(item.get('evidence_ids',[]));refs.update(item.get('source_candles',[]))
            refs.update(x for x in (item.get('source_candle'),item.get('reference_id')) if x)
        return jsonify(records=records,latest=latest,durability=durability,chart=packet.get('chart'),rules=packet['source_records'],context=packet['snapshot']['context'],evidence_count=len(packet['evidence']),annotation_evidence={key:packet['evidence'][key] for key in refs if key in packet['evidence']})
    @app.get('/ict-shadow/shadow-mvp/evidence/<ident>')
    def evidence(ident):
        with selected_store().readonly() as db:row=db.execute('SELECT payload FROM shadow_evidence WHERE id=? AND length(payload)<=3500000',(ident,)).fetchone()
        if not row:abort(404)
        return jsonify(json.loads(row[0]))
