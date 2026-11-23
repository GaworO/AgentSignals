"""Observation-only connector for the committed Observer, RAG and Store.

No provider/broker transport. Research geometry is never source qualification.
The existing engine selects NQ hypotheses; all three instruments supply events.
"""
import fcntl
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from .common import digest, encode, save
from .market import load_csv, clock, context, NY
from .observer import Observer
from .store import Store
from .knowledge import Knowledge
from .ai import packet as evidence_packet
from .shadow_chart import build_chart,followup_due
from .shadow_sessions import scoped_rules,session_at,correlation

VERSION = 'ict-observation-shadow-mvp-v2'
BASELINE = 'fafc6271b2c8fcf0ac9db0f34a9e67b7962310a7'
MISSING = ['SOURCE_MODEL_VARIANT_QUALIFICATION', 'DISCRETIONARY_HTF_RANGE_SELECTION',
           'RULE_PREREQUISITES_AND_EXCEPTIONS', 'SOURCE_ENTRY_STOP_TARGET_SELECTION']

def inputs_check(folder):
    folder=Path(folder).resolve(); manifest=json.loads((folder/'recovery.json').read_text())
    for rel, sha in manifest['files'].items():
        p=(folder/rel).resolve()
        if not p.is_relative_to(folder) or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:
            raise ValueError('SHADOW_INPUT_HASH_MISMATCH:'+rel)
    return manifest

def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS shadow_records(
            id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,as_of INTEGER NOT NULL,
            stage TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(candidate_id,as_of,stage));
        CREATE INDEX IF NOT EXISTS shadow_candidate_time ON shadow_records(candidate_id,as_of);
        CREATE TABLE IF NOT EXISTS shadow_checkpoints(batch_id TEXT PRIMARY KEY,as_of INTEGER NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shadow_evidence(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shadow_observations(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS shadow_observations_no_update BEFORE UPDATE ON shadow_observations BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_observations_no_delete BEFORE DELETE ON shadow_observations BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_records_no_update BEFORE UPDATE ON shadow_records BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_records_no_delete BEFORE DELETE ON shadow_records BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_evidence_no_update BEFORE UPDATE ON shadow_evidence BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_evidence_no_delete BEFORE DELETE ON shadow_evidence BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_checkpoints_no_update BEFORE UPDATE ON shadow_checkpoints BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS shadow_checkpoints_no_delete BEFORE DELETE ON shadow_checkpoints BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        ''')

def append(db, candidate, at, stage, body):
    value=dict(candidate_id=candidate,as_of=at,stage=stage,**body)
    ident=digest(value);encoded=encode(value)
    old=db.execute('SELECT payload FROM shadow_records WHERE candidate_id=? AND as_of=? AND stage=?',
                   (candidate,at,stage)).fetchone()
    if old and old[0]!=encoded:raise ValueError('SHADOW_IMMUTABILITY_CONFLICT')
    db.execute('INSERT OR IGNORE INTO shadow_records VALUES(?,?,?,?,?)',(ident,candidate,at,stage,encoded))

def assessment(setup, ctx):
    own=ctx.get(setup['symbol'],{}); missing=list(MISSING)
    if not own or own.get('stale') or setup.get('last_transition',{}).get('reason')=='MISSING_PRICE_PATH':
        return 'DATA_INSUFFICIENT',missing+['CONTIGUOUS_CURRENT_INSTRUMENT_M1'],'UNRESOLVED'
    if setup['state']=='INVALIDATED' and setup.get('last_transition',{}).get('reason')=='CLOSE_BEYOND_SWEEP_EXTREME':
        return 'REJECTED',missing,'RESEARCH_REVERSAL_GEOMETRY_INVALIDATED'
    return 'WAIT',missing+setup.get('missing',[]),'UNRESOLVED_CONTEXT'

def sync_latest(store, knowledge, start, end):
    """Recover an Observer commit not yet projected after a crash. No next bar is
    ingested before this function completes. Both projections are durable.
    """
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch=db.execute('SELECT id,as_of FROM batches ORDER BY as_of DESC LIMIT 1').fetchone()
        if not batch or db.execute('SELECT 1 FROM shadow_checkpoints WHERE batch_id=?',(batch[0],)).fetchone():return False
        memory=json.loads(db.execute('SELECT payload FROM memory WHERE id=1').fetchone()[0]);at=batch[1]
        if memory['cursor']!=at:raise ValueError('SHADOW_CURSOR_MISMATCH')
        ctx=context(memory['markets'],at)
        setups=[json.loads(x[0]) for x in db.execute('SELECT payload FROM setups')]
        processed=0
        current_events=[json.loads(x[0]) for x in db.execute('SELECT payload FROM events WHERE as_of=?',(at,))]
        for s in setups:
            if not start<=s['created_at']<=end:continue
            previous=db.execute("SELECT payload FROM shadow_records WHERE candidate_id=? AND stage='ASSESSMENT' ORDER BY as_of DESC LIMIT 1",(s['id'],)).fetchone()
            old=json.loads(previous[0]) if previous else None
            signature=digest({k:s.get(k) for k in ('state','sequence','supporting','contradicting','missing','last_transition')})
            follow=followup_due(s,old,ctx,current_events,at)
            if old and old['signature']==signature and not follow:continue
            state,missing,hypothesis=assessment(s,ctx)
            ids=list(dict.fromkeys(s['supporting']+s['contradicting']))
            # Reuse the real recursive evidence materializer; exact available
            # parents, all context anchors and retrieved source records.
            rid=memory['retrieval_ids'].get(s['id'])
            # Retired hypotheses may keep being observed after selector expiry.
            if not rid and old:
                prior_packet=json.loads(db.execute('SELECT payload FROM shadow_evidence WHERE id=?',(old['evidence_packet_id'],)).fetchone()[0])
                prior_ids=prior_packet['snapshot']['retrieval_ids'];rid=prior_ids[0] if prior_ids else None
            snap={'id':digest([s['id'],at]),'as_of':at,'context':ctx,'events':[],
                  'setups':[s],'retrieval_ids':[rid] if rid else [],'intermarket':{}}
            # A second read connection is safe: Observer already committed;
            # current write transaction has no changes needed by materializer.
            packet=evidence_packet(store,snap)
            packet['source_records']=scoped_rules(knowledge,packet['source_records'],s['symbol'])
            chart=build_chart(db,packet,s,ctx,at);packet['chart']=chart
            for observation in chart['derived_observations']:
                db.execute('INSERT OR IGNORE INTO shadow_observations VALUES(?,?)',(observation['id'],encode(observation)))
            packet.update(semantic_status='PENDING_LLM',execution_readiness='BLOCKED_RESEARCH_ONLY',
                          mode='SIMULATED_REPLAY',source_qualified=False)
            eid=digest(packet);db.execute('INSERT OR IGNORE INTO shadow_evidence VALUES(?,?)',(eid,encode(packet)))
            common={'symbol':s['symbol'],'direction':s['direction'],'model':s['research_policy'],
                    'model_scope':'RESEARCH_GEOMETRY_NOT_SOURCE_QUALIFIED_MODEL',
                    'evidence_packet_id':eid,'evidence_ids':ids,'source_rule_ids':s['source_rule_ids'],
                    'semantic_status':'PENDING_LLM','origin':'DETERMINISTIC_OBSERVATION',
                    'source_qualified':False,'execution_eligible':False,'orders_enabled':False,
                    'trade_parameters':None,'chart_state':chart['chart_state'],
                    'session':session_at(s['created_at']),'correlation':correlation(s)}
            if not old:
                append(db,s['id'],at,'DETECTED',dict(common,root_event=s['root_event']))
                append(db,s['id'],at,'CONTEXT_ASSESSED',dict(common,
                    context_status='COMPUTED_RANGES_NOT_SELECTED_ICT_DEALING_RANGE',
                    timeframes_available=sorted(ctx[s['symbol']]['ranges']),missing_requirements=missing))
                append(db,s['id'],at,'SOURCE_EVALUATED',dict(common,
                    evaluations=[{'rule_id':r['id'],'status':'RELEVANT_ONLY',
                        'source_reference_verified':r['id'] in knowledge.verified,
                        'market_applicability':'UNRESOLVED_REQUIRES_SEMANTIC_REVIEW',
                        'references':r['references'],'instrument_scope':r.get('instrument_scope_assessment')} for r in packet['source_records']],
                    meaning='Retrieval and source provenance checked; conditions have NOT been declared satisfied.'))
            append(db,s['id'],at,'ASSESSMENT',dict(common,decision=state,hypothesis_status=hypothesis,
                signature=signature,detector_state=s['state'],observed_sequence=s['sequence'],
                missing_requirements=missing,
                next_confirmation=[x for x in ('DISPLACEMENT','STRUCTURE_BREAK','FVG') if x not in s['sequence']],
                invalidation={'condition':'LATER_M1_CLOSE_BEYOND_RECORDED_SWEEP_EXTREME',
                    'level':s['sweep_extreme'],'direction':s['direction'],'scope':'RESEARCH_REVERSAL_ONLY'},
                rejection_reason=s.get('last_transition',{}).get('reason') if state=='REJECTED' else None))
            if old:
                added=[x for x in s['sequence'] if x not in old['observed_sequence']]
                terminal=s['state'] in ('EXPIRED','INVALIDATED','REJECTED','CANCELLED')
                if added or terminal or follow:
                    outcome='INVALIDATED_RESEARCH_GEOMETRY' if state=='REJECTED' else 'UNRESOLVED' if terminal else 'CONFIRMATION_GEOMETRY_OBSERVED'
                    append(db,s['id'],at,'OUTCOME',dict(common,status=outcome,
                        forecast_as_of=old['as_of'],added_confirmations=added,
                        newly_observed_retests=sorted(set(chart['chart_state']['retested'])-set(old.get('chart_state',{}).get('retested',[]))),
                        newly_observed_inversions=sorted(set(chart['chart_state']['inverted'])-set(old.get('chart_state',{}).get('inverted',[]))),
                        reason=s.get('last_transition',{}).get('reason'),
                        source_setup_confirmed=False,fill=None,pnl=None,profit_factor=None,
                        meaning='Later observation of a research hypothesis; not a fill or trade result.'))
            processed+=1
        db.execute('INSERT INTO shadow_checkpoints VALUES(?,?,?)',(batch[0],at,encode({'candidates_updated':processed})))
    return True

def finish(store, end):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ids=[r[0] for r in db.execute('SELECT DISTINCT candidate_id FROM shadow_records')]
        for ident in ids:
            last=json.loads(db.execute("SELECT payload FROM shadow_records WHERE candidate_id=? AND stage='ASSESSMENT' ORDER BY as_of DESC LIMIT 1",(ident,)).fetchone()[0])
            previous=db.execute("SELECT 1 FROM shadow_records WHERE candidate_id=? AND stage='OUTCOME' AND as_of=?",(ident,end)).fetchone()
            if previous:continue
            append(db,ident,end,'SESSION_END',{'status':'INVALIDATED_RESEARCH_GEOMETRY' if last['decision']=='REJECTED' else 'UNRESOLVED',
                'last_assessment_as_of':last['as_of'],'semantic_status':'PENDING_LLM',
                'reason':'OBSERVATION_SESSION_ENDED_NOT_A_SOURCE_INVALIDATION','fill':None,'pnl':None})

def summary(store):
    with store.readonly() as db:
        rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM shadow_records WHERE stage='ASSESSMENT' ORDER BY as_of")]
        latest={r['candidate_id']:r for r in rows}
        manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
        results={'mode':'SIMULATED_REPLAY','semantic_status':'PENDING_LLM','feed_status':'LIVE_NOT_VERIFIED',
            'session':manifest['session'],'selected_hypothesis_instruments':manifest.get('symbols',['NQ']),
            'detected_event_instruments':['NQ','MNQ','ES'], 'candidates':len(latest),'candidates_by_instrument':dict(Counter(r['symbol'] for r in latest.values())),
            'candidates_by_session':dict(Counter(r.get('session','UNSPECIFIED') for r in latest.values())),
            'decisions':dict(Counter(r['decision'] for r in latest.values())),
            'decision_records':len(rows),'stages':dict(db.execute('SELECT stage,count(*) FROM shadow_records GROUP BY stage')),
            'outcome_records':db.execute("SELECT count(*) FROM shadow_records WHERE stage IN ('OUTCOME','SESSION_END')").fetchone()[0],
            'events':dict(db.execute('SELECT kind,count(*) FROM events WHERE as_of BETWEEN ? AND ? GROUP BY kind',(manifest['start'],manifest['end']))),
            'checkpoints':db.execute('SELECT count(*) FROM shadow_checkpoints').fetchone()[0],
            'source_qualified':0,'execution_eligible':0,'openai_calls':0,'broker_orders':0,
            'llm_judgments':0,'pnl':None,'profit_factor':None,'validation_failures':[],
            'legacy_code_baseline':BASELINE,'latest_research_restored':False,
            'limitation':'Own-candle research geometry across NQ/MNQ/ES; source qualification and LLM interpretation unresolved. Later uncommitted research is not restored.',
            'current_as_of':db.execute('SELECT max(as_of) FROM batches').fetchone()[0],
            'input_coverage':manifest['coverage']}
    return results

def audit(store):
    counts=Counter()
    with store.readonly() as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('JOURNAL_INTEGRITY')
        for ident,raw in db.execute('SELECT id,payload FROM shadow_records'):
            x=json.loads(raw)
            if digest(x)!=ident:raise ValueError('JOURNAL_RECORD_HASH')
            counts['append_only_records']+=1
            if x.get('source_qualified') or x.get('execution_eligible') or x.get('orders_enabled'):raise ValueError('UNAUTHORIZED_QUALIFICATION')
            if x.get('forecast_as_of',x['as_of'])>x['as_of']:raise ValueError('OUTCOME_LOOKAHEAD')
        for ident,raw in db.execute('SELECT id,payload FROM shadow_evidence'):
            x=json.loads(raw)
            if digest(x)!=ident:raise ValueError('EVIDENCE_HASH')
            at=x['snapshot']['as_of']
            for item in x['evidence'].values():
                if item['available_at']>at:raise ValueError('LOOKAHEAD')
                if set(item.get('evidence_ids',[]))-x['evidence'].keys():raise ValueError('BROKEN_EVIDENCE_CHAIN')
                counts['causal_evidence_links']+=1
            if 'chart' in x:
                for bars in x['chart']['frames'].values():
                    for f in bars:
                        if f['available_at']>at:raise ValueError('CHART_LOOKAHEAD')
                for a in x['chart']['annotations']:
                    if a['observation_id'] not in x['evidence'] or a['available_at']>at:raise ValueError('CHART_ANNOTATION_REFERENCE')
            if x['future_outcomes_included'] or x['execution_permission']:raise ValueError('UNSAFE_PACKET')
            counts['materialized_evidence_packets']+=1
        if db.execute('SELECT count(*) FROM ai_reviews').fetchone()[0]:raise ValueError('UNEXPECTED_LLM_REVIEW')
        if db.execute('SELECT count(*) FROM batches').fetchone()[0]!=db.execute('SELECT count(*) FROM shadow_checkpoints').fetchone()[0]:raise ValueError('UNPROJECTED_BATCH')
    return dict(status='PASS',**counts,semantic_interpretation='NOT_TESTED_PENDING_LLM')

def replay(inputs, output, session='2026-05-20', stop_at=None):
    inputs=Path(inputs).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    lock=(output/'observer.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    source=inputs_check(inputs)
    start=int(datetime.fromisoformat(session+'T02:00:00').replace(tzinfo=NY).timestamp())
    end=int(datetime.fromisoformat(session+'T16:00:00').replace(tzinfo=NY).timestamp())
    warmup=int((datetime.fromtimestamp(start,NY)-timedelta(days=3)).replace(hour=18,minute=0).timestamp())
    stop=int(datetime.fromisoformat(session+'T'+stop_at+':00').replace(tzinfo=NY).timestamp()) if stop_at else end
    batches=defaultdict(list);coverage={}
    for symbol in ('NQ','MNQ','ES'):
        bars,_=load_csv(inputs/'market'/(symbol+'.csv'),symbol)
        present={b['received_at'] for b in bars if start<b['received_at']<=end}
        expected=set(range(start+60,end+1,60));missing=sorted(expected-present)
        coverage[symbol]={'session_m1_bars':len(present),'expected':len(expected),'missing_closes':missing,
            'contract':source['archives'][symbol]['contract_identifiers'], 'contract_mapping_verified':False,
            'source_delay':'NOT_MEASURED','availability':'ASSUMED_AT_BAR_CLOSE'}
        for b in bars:
            if warmup<=b['ts'] and b['received_at']<=end:batches[b['ts']].append(b)
    manifest={'version':VERSION,'mode':'SIMULATED_REPLAY','session':session,'start':start,'end':end,
        'connector_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'component_hashes':{n:hashlib.sha256(Path(__file__).with_name(n+'.py').read_bytes()).hexdigest() for n in ('shadow_chart','shadow_sessions','scenarios','observer')},
        'baseline':BASELINE,'recovery_sha256':hashlib.sha256((inputs/'recovery.json').read_bytes()).hexdigest(),
        'coverage':coverage,'symbols':['NQ','MNQ','ES'],'orders_enabled':False,'paid_calls_enabled':False,
        'interpretation':'PENDING_LLM','source_qualification':'NOT_ESTABLISHED',
        'replay_date_selection':'FIRST_PREDECLARED_SESSION_FOR_INTEGRATION_NOT_SELECTED_ON_OUTCOME',
        'session_calendar':'America/New_York ZoneInfo; 02:00–16:00 observation; LO 02–05, NYAM 08:30–12, NYPM 13–16 engineering windows. No live exchange calendar/entitlement verification.'}
    store=Store(output/'observer.sqlite3',manifest);initialize(store)
    knowledge=Knowledge(corpus=inputs/'knowledge/corpus',atlas=inputs/'knowledge/atlas')
    observer=Observer(store,knowledge,symbols=('NQ','MNQ','ES'));sync_latest(store,knowledge,start,end)
    with store.readonly() as db:cursor=db.execute('SELECT coalesce(max(as_of),-1) FROM batches').fetchone()[0]
    processed=0
    for ts,batch in sorted(batches.items()):
        at=max(b['received_at'] for b in batch)
        if at<=cursor or at>stop:continue
        observer.push(batch,emit=at>=start)
        sync_latest(store,knowledge,start,end)
        processed+=1
        if processed%500==0:print(encode({'processed_new_batches':processed,'replay_clock':clock(at).isoformat()}),flush=True)
    if stop>=end:finish(store,end)
    report=summary(store);report['audit']=audit(store);report['new_batches']=processed
    report['completed_session']=report['current_as_of']==end
    save(output/'summary.json',report)
    print(encode(report),flush=True)
    fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
    return report
