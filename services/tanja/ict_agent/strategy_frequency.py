"""Full accessible-archive frequency measurement, never execution simulation.

Reuses the live observer's unchanged incremental detector and candidate selector.
Source-model projections are screening populations, not independently qualified
strategies. All calculations use close-time availability and retain original IDs.
"""
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .common import digest, encode
from .market import load_csv, advance, fresh_state, context, clock
from .scenarios import update_setups, TERMINAL, POLICY
from .shadow_sessions import session_at

VERSION = 'ict-strategy-frequency-v1'
SYMBOLS = ('NQ', 'MNQ', 'ES')


class FrequencyOnlyKnowledge:
    """No retrieval is necessary for detector frequency. This is NOT mock RAG.

    The unchanged selector uses retrieval only to attach descriptive metadata;
    its geometry transitions never depend on returned records. Source screening
    is performed separately against the independently reviewed taxonomy.
    """
    def retrieve(self, *args, **kwargs):
        return {'records': [], 'audit_notes': [],
                'status': 'FREQUENCY_ONLY_NO_RETRIEVAL',
                'market_source_qualification': 'NOT_EVALUATED'}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save_new(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()
    path = Path(path)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('IMMUTABLE_ARTIFACT_CONFLICT:' + path.name)
        return
    with path.open('xb') as f:
        f.write(data)


def exposure_group(symbol, kind, at, direction, tf=0, level_tf=0):
    """Correlated exposure cluster, NOT assertion that own-candle events equal."""
    underlying = 'NASDAQ' if symbol in ('NQ', 'MNQ') else 'SP500'
    return digest([underlying, kind, at // 60, direction, tf, level_tf])[:24]


def verify_event_batch(events, known, at):
    # Detector list order is not intrabar chronology. A newly confirmed pivot
    # may be referenced by EQUAL_EXTREMES earlier in the same close-time batch.
    for event in events:
        if event['available_at'] > at: raise ValueError('FUTURE_EVENT')
        known[event['id']] = event['available_at']
    count = 0
    for event in events:
        for ref in event.get('evidence_ids', []):
            if ref not in known or known[ref] > event['available_at']:
                raise ValueError('MISSING_OR_FUTURE_EVENT_REFERENCE:' + event['id'])
            count += 1
    return count


def inventory(inputs):
    inputs = Path(inputs)
    recovery = json.loads((inputs / 'recovery.json').read_text())
    tapes, info, days = {}, {}, set()
    for symbol in SYMBOLS:
        rel = 'market/' + symbol + '.csv'
        if sha(inputs / rel) != recovery['files'][rel]:
            raise ValueError('MARKET_RECOVERY_HASH_MISMATCH:' + symbol)
        bars, audit = load_csv(inputs / rel, symbol)
        tapes[symbol] = bars
        dates = Counter(clock(b['ts']).date().isoformat() for b in bars)
        days.update(dates)
        gaps = []
        for a, b in zip(bars, bars[1:]):
            if b['ts'] != a['ts'] + 60:
                gaps.append({'after_close': a['ts'] + 60, 'before_open': b['ts'],
                             'absent_minutes': (b['ts'] - a['ts']) // 60 - 1,
                             'classification': 'ARCHIVE_DISCONTINUITY_NOT_AUTOMATIC_FEED_FAILURE'})
        info[symbol] = {**audit, 'first_ny': clock(bars[0]['ts']).isoformat(),
                        'last_ny': clock(bars[-1]['ts']).isoformat(),
                        'calendar_day_rows': dict(dates), 'discontinuities': gaps}
    # Reuse the existing chronological splitter, including its prior-exposure warning.
    from .replay import split_plan
    split = split_plan(sorted(days))
    return tapes, info, split


def initialize(db):
    db.executescript('''
    CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
    CREATE TABLE frames(id TEXT PRIMARY KEY,symbol TEXT,at INTEGER,tf INTEGER,payload TEXT NOT NULL);
    CREATE TABLE events(id TEXT PRIMARY KEY,symbol TEXT,at INTEGER,kind TEXT,tf INTEGER,direction TEXT,
      session TEXT,month TEXT,partition TEXT,exposure_group TEXT,payload TEXT NOT NULL);
    CREATE INDEX events_filter ON events(kind,symbol,session,month,at);
    CREATE TABLE candidates(id TEXT PRIMARY KEY,symbol TEXT,created_at INTEGER,direction TEXT,session TEXT,
      month TEXT,partition TEXT,state TEXT,complete_geometry INTEGER,engineering_plan INTEGER,
      exposure_group TEXT,payload TEXT NOT NULL);
    CREATE INDEX candidates_filter ON candidates(symbol,session,month,created_at);
    CREATE TABLE transitions(id TEXT PRIMARY KEY,candidate_id TEXT,at INTEGER,payload TEXT NOT NULL);
    CREATE TABLE model_occurrences(model_id TEXT,entity_kind TEXT,entity_id TEXT,screen_status TEXT,
      reason TEXT,PRIMARY KEY(model_id,entity_kind,entity_id));
    CREATE INDEX occurrences_model ON model_occurrences(model_id,entity_kind);
    CREATE TABLE validation(key TEXT PRIMARY KEY,value INTEGER);
    ''')


def partition_for(at, split):
    day = clock(at).date().isoformat()
    return next((name for name in ('development', 'validation', 'retrospective_test')
                 if day in split[name]), 'UNPARTITIONED')


def persist_candidate(db, setup, split):
    at = setup['created_at']
    complete = all(k in setup['sequence'] for k in ('DISPLACEMENT', 'STRUCTURE_BREAK', 'FVG'))
    group = exposure_group(setup['symbol'], 'CANDIDATE', at, setup['direction'])
    db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
               'state=excluded.state,complete_geometry=excluded.complete_geometry,'
               'engineering_plan=excluded.engineering_plan,payload=excluded.payload',
               (setup['id'], setup['symbol'], at, setup['direction'], session_at(at),
                clock(at).strftime('%Y-%m'), partition_for(at, split), setup['state'],
                int(complete), int(setup['plan'] is not None), group, encode(setup)))


def measure(inputs, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    if (output / 'research.sqlite').exists():
        raise ValueError('RESEARCH_DATABASE_ALREADY_EXISTS_USE_SUMMARIZE_NO_REPLAY')
    tapes, info, split = inventory(inputs)
    root = Path(__file__).parent
    engine_files = ('strategy_frequency.py', 'market.py', 'scenarios.py', 'shadow_sessions.py', 'replay.py')
    manifest = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                'mode': 'HISTORICAL_FREQUENCY_NO_ORDER_SIMULATION', 'inputs': info,
                'engine_hashes': {name: sha(root / name) for name in engine_files},
                'chronological_split': split, 'selector': POLICY,
                'source_qualification': 'NOT_ESTABLISHED_BY_FREQUENCY_SCAN',
                'retrieval': 'NOT_RUN_GEOMETRY_ONLY_SOURCE_TAXONOMY_SEPARATE',
                'outcomes_used_to_select_rules': False, 'orders': False, 'paid_calls': 0,
                'regime': 'UNCLASSIFIED_NO_FROZEN_VALIDATED_REGIME_MODEL',
                'deduplication': {'name': 'CORRELATED_EXPOSURE_CLUSTER_V1',
                    'key': 'underlying, kind, close-minute, direction, timeframe, reference-timeframe',
                    'meaning': 'NQ/MNQ same-minute related evidence grouped for frequency; '
                               'not identical candles or independently tradable opportunities. Raw rows retained.'}}
    save_new(output / 'manifest.json', manifest)
    save_new(output / 'inventory.json', info)
    save_new(output / 'chronological-split.json', split)
    frozen_runtime=output/'measurement-runtime';frozen_runtime.mkdir(exist_ok=True)
    for name in engine_files:
        target=frozen_runtime/name
        if target.exists() and sha(target)!=manifest['engine_hashes'][name]:
            raise ValueError('FROZEN_RUNTIME_CONFLICT:'+name)
        if not target.exists():target.write_bytes((root/name).read_bytes())
    db = sqlite3.connect(output / 'research.sqlite')
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=NORMAL')
    initialize(db)
    db.execute('INSERT INTO metadata VALUES(?,?)', ('manifest', encode(manifest))); db.commit()
    known = {}; checks = Counter(); began = time.monotonic(); knowledge = FrequencyOnlyKnowledge()
    # Separate own-instrument states: correlated symbols never supply missing evidence.
    for symbol, bars in tapes.items():
        state = None; setups = {}; last_day = None
        for index, bar in enumerate(bars):
            at = bar['received_at']; local = clock(at); day = local.date().isoformat()
            if state is None or state['contract'] != bar['contract']:
                state = fresh_state(symbol, bar['contract'])
            frames, events = advance(state, bar)
            for f in frames:
                if f['available_at'] > at: raise ValueError('FUTURE_FRAME')
                known[f['id']] = f['available_at']
                db.execute('INSERT INTO frames VALUES(?,?,?,?,?)',
                           (f['id'], symbol, f['available_at'], f['timeframe'], encode(f)))
            checks['event_references'] += verify_event_batch(events, known, at)
            for event in events:
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (event['id'], symbol, at, event['kind'], event['timeframe'], event.get('direction'),
                     session_at(at), local.strftime('%Y-%m'), partition_for(at, split),
                     exposure_group(symbol, event['kind'], at, event.get('direction'),
                                    event['timeframe'], event.get('level_timeframe', 0)), encode(event)))
            ctx = context({symbol: state}, at)
            before = set(setups)
            transitions, _ = update_setups(setups, events, ctx, at, knowledge, (symbol,))
            changed = (set(setups) - before) | {t['setup_id'] for t in transitions}
            for ident in changed:
                setup = setups[ident]
                for ref in setup['supporting'] + setup['contradicting']:
                    if ref not in known or known[ref] > at: raise ValueError('NONCAUSAL_CANDIDATE')
                    checks['candidate_references'] += 1
                persist_candidate(db, setup, split)
            for t in transitions:
                db.execute('INSERT INTO transitions VALUES(?,?,?,?)',
                           (t['id'], t['setup_id'], t['at'], encode(t)))
            terminal = sorted([s for s in setups.values() if s['state'] in TERMINAL], key=lambda s:s['updated_at'])
            for s in terminal[:-8]:
                persist_candidate(db, s, split); setups.pop(s['id'])
            checks['bars'] += 1
            if index % 1000 == 0: db.commit()
            if day != last_day:
                db.commit()
                print(json.dumps({'symbol':symbol, 'day':day, 'bars':index,
                                  'elapsed_seconds':round(time.monotonic()-began, 1)}), flush=True)
                last_day = day
        for s in setups.values(): persist_candidate(db, s, split)
        db.commit()
    for k, value in checks.items(): db.execute('INSERT INTO validation VALUES(?,?)', (k, value))
    for name, expected in manifest['engine_hashes'].items():
        if sha(root / name) != expected: raise ValueError('ENGINE_CHANGED_DURING_RUN:' + name)
    db.execute('INSERT INTO metadata VALUES(?,?)', ('completed', encode({'at':datetime.now(timezone.utc).isoformat(),
                 'seconds':round(time.monotonic()-began,2), 'checks':checks})))
    db.commit(); db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.close()
    return manifest


def projected(event, row):
    projection = row.get('geometric_projection', {})
    if event['kind'] not in projection.get('event_kinds', []): return False
    directions = row.get('directions', [])
    if directions and event.get('direction') not in directions: return False
    frames = projection.get('timeframes')
    if frames and event['timeframe'] not in frames: return False
    window = projection.get('session_ny')
    if window:
        at = clock(event['available_at']); minute = at.hour * 60 + at.minute
        if not minute_value(window['start']) <= minute < minute_value(window['end']): return False
    return True


def minute_value(value):
    if isinstance(value, int): return value
    hour, minute = value.split(':')
    return int(hour) * 60 + int(minute)


def candidate_projected(setup, row):
    projection = row.get('geometric_projection', {})
    if not projection.get('event_kinds') or row['family'] == 'ote': return False
    if row.get('directions') and setup['direction'] not in row['directions']: return False
    # This screens the originating raid time. It never asserts the entry/retest is in-window.
    window = projection.get('session_ny')
    if window:
        at = clock(setup['created_at']); minute = at.hour * 60 + at.minute
        if not minute_value(window['start']) <= minute < minute_value(window['end']): return False
    if row['family'] == 'fvg_limit' and 'FVG' not in setup.get('sequence', []):
        return False
    # IFVG is NOT present in the selector's candidate chain. Never silently equate with FVG.
    if projection.get('event_kinds') == ['IFVG']: return False
    return True


def summarize(output, taxonomy_path):
    output = Path(output); taxonomy = json.loads(Path(taxonomy_path).read_text())
    db = sqlite3.connect(output/'research.sqlite'); db.row_factory = sqlite3.Row
    manifest = json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
    completed = db.execute("SELECT payload FROM metadata WHERE key='completed'").fetchone()
    if not completed: raise ValueError('FREQUENCY_RUN_INCOMPLETE')
    models = []
    db.execute('DELETE FROM model_occurrences')  # Derived projection only; original evidence remains unchanged.
    for row in taxonomy['research_rows']:
        model_id = row['id']; kinds = row.get('geometric_projection', {}).get('event_kinds', [])
        if kinds:
            marks = ','.join('?' for _ in kinds)
            for r in db.execute('SELECT id,payload FROM events WHERE kind IN ('+marks+')', kinds).fetchall():
                if projected(json.loads(r['payload']), row):
                    db.execute('INSERT INTO model_occurrences VALUES(?,?,?,?,?)',
                        (model_id,'event',r['id'],'GEOMETRIC_COMPONENT_SCREEN_ONLY',
                         'Source-family direction/time projection. Full model conditions NOT established.'))
            for r in db.execute('SELECT id,payload FROM candidates').fetchall():
                if candidate_projected(json.loads(r['payload']), row):
                    db.execute('INSERT INTO model_occurrences VALUES(?,?,?,?,?)',
                        (model_id,'candidate',r['id'],'RESEARCH_SELECTOR_SCREEN_ONLY',
                         'Originating sweep time only; source entry-window, context and parameter choices unresolved.'))
        geometries = db.execute("SELECT count(*) FROM model_occurrences WHERE model_id=? AND entity_kind='event'",(model_id,)).fetchone()[0]
        count = db.execute("SELECT count(*) FROM model_occurrences WHERE model_id=? AND entity_kind='candidate'",(model_id,)).fetchone()[0]
        base = " FROM candidates c JOIN model_occurrences m ON m.entity_id=c.id AND m.entity_kind='candidate' WHERE m.model_id=?"
        complete = db.execute('SELECT coalesce(sum(c.complete_geometry),0)' + base,(model_id,)).fetchone()[0]
        plans = db.execute('SELECT coalesce(sum(c.engineering_plan),0)' + base,(model_id,)).fetchone()[0]
        rejected = db.execute("SELECT count(*)" + base + " AND c.state='INVALIDATED'",(model_id,)).fetchone()[0]
        htf_missing=Counter();selector_reasons=Counter()
        for record in db.execute('SELECT c.payload'+base,(model_id,)):
            setup=json.loads(record['payload']);ranges=setup.get('htf_context_at_creation',{})
            for tf in ('60','240','1440'):
                if tf not in ranges:htf_missing[tf]+=1
            selector_reasons[setup.get('last_transition',{}).get('reason','NO_TERMINAL_TRANSITION')]+=1
        group_count = db.execute("SELECT count(distinct e.exposure_group) FROM events e JOIN model_occurrences m ON m.entity_id=e.id AND m.entity_kind='event' WHERE m.model_id=?",(model_id,)).fetchone()[0]
        breakdowns = {}
        for column, label in [('symbol','instrument'),('session','session'),('month','month'),('partition','partition')]:
            groups = {}
            for r in db.execute('SELECT e.'+column+' AS value,count(*) AS n,count(distinct e.exposure_group) AS clusters FROM events e JOIN model_occurrences m ON m.entity_id=e.id AND m.entity_kind=\'event\' WHERE m.model_id=? GROUP BY e.'+column,(model_id,)):
                groups[r['value']]={'value':r['value'],'geometry_count':r['n'],'exposure_clusters':r['clusters'],'selector_candidates':0,'complete_geometry_candidates':0}
            for r in db.execute('SELECT c.'+column+' AS value,count(*) AS n,sum(c.complete_geometry) AS complete'+base+' GROUP BY c.'+column,(model_id,)):
                groups.setdefault(r['value'],{'value':r['value'],'geometry_count':0,'exposure_clusters':0})
                groups[r['value']].update(selector_candidates=r['n'],complete_geometry_candidates=r['complete'])
            breakdowns[label]=list(groups.values())
        limits=list(row.get('unresolved',[]))+[
            'Geometry and candidate projections overlap across model families; do not add them as separate strategies.',
            'Full source prerequisites not independently evaluated for every occurrence; zero means none established, not all failed.',
            'No source-certified entry/SL/TP selection, dated contract/roll map, fee/slippage policy or OHLC fill policy frozen.',
            'Origin/formation window is not proof that a later entry/retest satisfies the source entry window.']
        limitations = limits + (['OTE anchors/retracement detector unavailable; structure-break components are not OTE patterns.'] if row['family']=='ote' else [])
        no_selector = row['family']=='ote' or kinds==['IFVG']
        if kinds==['IFVG']:
            limitations.append('NO_IFVG_SELECTOR: existing selector never creates an IFVG branch. Zero selector candidates is not absence of market setups.')
        models.append({'id':model_id,'name':row['name'],'family':row['family'],'kind':row['kind'],
             'geometry_count':geometries if kinds and row['family']!='ote' else None,
             'geometry_measurement':'RELEVANT_DETECTED_COMPONENTS_NOT_COMPLETE_MODEL',
             'component_count':geometries if kinds else None,'exposure_cluster_count':group_count if kinds else None,
             'selector_candidates':count if kinds and row['family']!='ote' else None,
             'complete_geometry_candidates':complete if kinds and row['family']!='ote' else None,
             'engineering_price_plans':plans if kinds else None,'market_geometry_rejections':rejected if kinds else None,
             'source_qualified_count':0,'source_prerequisites_verified_count':None,'fully_source_defined_count':None,
             'context_unresolved_count':count if kinds and not no_selector else None,'mandatory_failed_count':None,
             'source_qualification_rate':None,
             'selector_coverage':'NO_SELECTOR' if no_selector else 'ENGINEERING_SWEEP_SELECTOR_ONLY',
             'missing_closed_htf_at_detection':{tf:htf_missing[tf] for tf in ('60','240','1440')} if not no_selector else None,
             'htf_scope':'Available computed trailing ranges only; contextual dealing-range selection is NOT certified.',
             'selector_terminal_reasons':dict(selector_reasons) if not no_selector else None,
             'complete_geometry_definition':'Raid root, later displacement, structural cross and FVG. Retest NOT evaluated; no claim of source-supported MSS.',
             'source_qualification_status':'NOT_INDEPENDENTLY_ESTABLISHED','simulation_eligible_count':0,
             'simulated_trade_count':0,'pf_status':'PF_NOT_ESTIMABLE','pf':None,'win_rate':None,'average_r':None,
             'expectancy':None,'max_drawdown':None,'net_pnl':None,'source_rule_ids':row['source_rule_ids'],
             'defined_coordinates':row.get('defined_coordinates'),'limitations':limitations,'breakdowns':breakdowns})
    db.commit()
    totals=dict(db.execute('SELECT kind,count(*) FROM events GROUP BY kind').fetchall())
    state_counts=dict(db.execute('SELECT state,count(*) FROM candidates GROUP BY state').fetchall())
    summary={'version':VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),
       'manifest_sha256':sha(output/'manifest.json'),'taxonomy_sha256':sha(taxonomy_path),
       'scope':{'first_ny':min(x['first_ny'] for x in manifest['inputs'].values()),
                'last_ny':max(x['last_ny'] for x in manifest['inputs'].values()),
                'bars_by_symbol':{s:x['rows'] for s,x in manifest['inputs'].items()},
                'mode':manifest['mode'],'contracts_verified':False,'regime':'UNCLASSIFIED'},
       'totals':{'events':sum(totals.values()),'events_by_kind':totals,'candidates':sum(state_counts.values()),
                 'candidate_states':state_counts,'complete_engineering_sequences':db.execute('SELECT sum(complete_geometry) FROM candidates').fetchone()[0],
                 'engineering_price_plans':db.execute('SELECT sum(engineering_plan) FROM candidates').fetchone()[0],
                 'candidate_exposure_clusters':db.execute('SELECT count(distinct exposure_group) FROM candidates').fetchone()[0],
                 'source_qualified':0,'simulation_eligible':0,'simulated_trades':0},
       'metric_definitions':{'complete_engineering_sequences':'Sweep root → later displacement → structural cross → FVG; retest not evaluated. Geometry is not a source-qualified setup.',
          'geometry_count':'Raw relevant component events after directional/frame/time projection; not complete model occurrences.',
          'source_qualified': 'Zero independently established; not an assertion that every historical pattern fails.',
          'source_qualification_rate':'Unassessed source denominator: null, not 0%.',
          'candidate_exposure_clusters':'Same-minute, same-direction NASDAQ NQ/MNQ exposure groups plus separate ES groups; own contract evidence retained.'},
       'models':models,'chronological_split':manifest['chronological_split'],'deduplication':manifest['deduplication'],
       'validation':dict(db.execute('SELECT key,value FROM validation').fetchall()),
       'limitations':['Only accessible recovered Apr20–May31 archive is measured; prior June/November research is outside these inputs.',
           'All available archive has prior research exposure; retrospective_test is chronological, not an untouched OOS profitability result.',
           'Confirmed detector geometry does not establish source-defined MSS, selected dealing range or a qualified trade.',
           'Existing selector engineering policy caps six active candidates/instrument and excludes M1-only sweep references.',
           'Existing simulator accepts explicit quote/fill events, not inferred OHLC fills; no paper/live orders created.',
           'Archive gaps may include maintenance, weekends and holidays. No interpolated candles or asserted entitlement.',
           'No validated regime classifier or dated historical session calendar is added; regime breakdown remains UNCLASSIFIED.',
           'No live observer files or production services were modified. LLM interpretation remains pending.'],
       'research_confidence':'MEASURED_GEOMETRY_AND_SELECTOR_FREQUENCY_ONLY_NO_SOURCE_QUALIFIED_PERFORMANCE',
       'paid_calls':0,'orders_sent':0}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
    db.execute('PRAGMA journal_mode=DELETE');db.close()
    publication={'generated_at':summary['generated_at'],'manifest_sha256':summary['manifest_sha256'],
                 'summary_sha256':sha(output/'summary.json'),'taxonomy_sha256':summary['taxonomy_sha256'],
                 'reporter_sha256':sha(__file__),'database_sha256':sha(output/'research.sqlite'),
                 'measurement_runtime':'measurement-runtime (hashes recorded by manifest; current reporter may add explicit labels only)',
                 'no_new_market_replay':True}
    (output/'publication.json').write_text(json.dumps(publication,indent=2,sort_keys=True)+'\n')
    return summary
