"""Real archived data replay and chronological evaluation. No return claims from OHLC fills."""
import json
import time
from collections import defaultdict,Counter
from datetime import date,timedelta,datetime
from pathlib import Path
from . import VERSION
from .common import REPO,OUTPUT,CORPUS,ATLAS,save,digest,code_hash,module,read_bytes
from .market import load_csv,clock,NY
from .knowledge import Knowledge
from .observer import Observer
from .store import Store
from .scenarios import POLICY

DATA=REPO/'research_tanja_month_202605_20261014'

def split_plan(days):
    days=sorted(set(days))
    n=len(days);a=max(1,int(n*.6));b=max(a,int(n*.8))
    return dict(development=days[:a],validation=days[a:b],retrospective_test=days[b:],untouched_holdout=[],
        warning='All this archive has prior research exposure; chronological partitions are infrastructure checks, NOT untouched out-of-sample evidence.',
        knowledge_timing='The November corpus includes knowledge collected after the May market data; this is retrospective development.',
        walk_forward=[dict(train=days[:i],validation=days[i:i+4]) for i in range(8,n,4)])

def run(start='2026-05-01',end='2026-05-31',data_dir=DATA,output=OUTPUT,progress=True):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    frozen_code_hash=code_hash()
    knowledge=Knowledge(output/'knowledge-audit.json')
    tapes={};input_audit={}
    for symbol in ('NQ','ES','MNQ'):tapes[symbol],input_audit[symbol]=load_csv(Path(data_dir)/(symbol+'.csv'),symbol)
    import hashlib
    dependencies={str(p.relative_to(REPO)):hashlib.sha256(read_bytes(p)).hexdigest() for p in (
        CORPUS/'knowledge-registry.json',CORPUS/'exhaustive/coverage.json',ATLAS/'reference.json',
        REPO/'research/ict_model_tests_20261119/engine.py')}
    manifest=dict(version=VERSION,code_hash=frozen_code_hash,knowledge_version=knowledge.version,index_version=knowledge.index_version,
        start=start,end=end,inputs=input_audit,dependency_hashes=dependencies,policy=POLICY,mode='REAL_ARCHIVE_REPLAY',live_enabled=False,
        paid_api_calls=0,receipt_assumption='BAR_CLOSE_WITH_ZERO_FEED_LATENCY',execution_evidence='OHLC_ONLY',
        prior_exposure='DEVELOPMENT_ONLY_NOT_UNTOUCHED_HOLDOUT')
    run_id=digest(manifest)[:16];folder=output/'runs'/run_id;folder.mkdir(parents=True,exist_ok=True)
    import shutil
    root=Path(__file__).parent
    for src in root.rglob('*'):
        if src.suffix in ('.py','.js','.html'):
            dst=folder/'code'/src.relative_to(root);dst.parent.mkdir(parents=True,exist_ok=True)
            if not dst.exists():shutil.copyfile(src,dst)
    shutil.copyfile(REPO/'research/ict_model_tests_20261119/engine.py',folder/'baseline-engine.py')
    save(folder/'manifest.json',manifest)
    store=Store(folder/'observer.sqlite3',manifest);observer=Observer(store,knowledge)
    batches=defaultdict(list)
    warmup=(date.fromisoformat(start)-timedelta(days=4)).isoformat()
    for symbol,items in tapes.items():
        for b in items:
            day=clock(b['ts']).date().isoformat()
            if warmup<=day<=end:batches[b['ts']].append(b)
    began=time.perf_counter();last_day=None;days=[]
    for i,(ts,batch) in enumerate(sorted(batches.items())):
        local=clock(ts);day=local.date().isoformat();minute=local.hour*60+local.minute
        emit=start<=day<=end and local.weekday()<5 and day!='2026-05-25' and 570<=minute<960
        observer.push(batch,emit=emit)
        if emit:days.append(day)
        if day!=last_day and progress:
            print(json.dumps(dict(day=day,batches=i,total=len(batches),elapsed_seconds=round(time.perf_counter()-began,1))),flush=True)
            last_day=day
    save(folder/'split-plan.json',split_plan(days))
    report=evaluate(store,tapes,start,end,folder/'baseline-engine.py')
    report['journal_validation']=validate_run(store,knowledge)
    report.update(run_id=run_id,wall_seconds=round(time.perf_counter()-began,2),statistics=store.stats(),
        strict_execution_data_contract='NOT_MET',llm_comparison='NOT_RUN_NO_PAID_CALLS',
        training='NONE_NO_AUTO_PARAMETER_CHANGES',out_of_sample='NONE_THIS_ARCHIVE_ALREADY_EXPOSED',
        source_code_unchanged_during_run=code_hash()==frozen_code_hash)
    save(folder/'evaluation.json',report)
    save(output/'latest.json',dict(run_id=run_id,database=str(store.path),folder=str(folder),evaluation=str(folder/'evaluation.json'),status='COMPLETED_REAL_REPLAY'))
    # Frozen packet for the first chronological research candidate, or first snapshot.
    with store.readonly() as db:
        row=db.execute("SELECT id FROM snapshots ORDER BY CASE WHEN decision='TRADE_CANDIDATE' THEN 0 ELSE 1 END,as_of LIMIT 1").fetchone()
    if row:
        from .ai import packet,request
        p=packet(store,store.snapshot(row[0]));save(folder/'example-market-packet.json',p)
        save(folder/'example-api-request.json',request(p,'EXPLICIT_MODEL_REQUIRED'))
    return report

def validate_run(store,knowledge):
    """Audit every stored decision and anchor, not just a selected successful trade."""
    from .store import ALLOWED
    from .ai import packet
    counts=Counter()
    with store.readonly() as db:
        known={i:t for i,t in db.execute('SELECT id,available_at FROM frames')}
        known.update({i:t for i,t in db.execute('SELECT id,as_of FROM events')})
        for raw, in db.execute('SELECT payload FROM events'):
            e=json.loads(raw)
            for ref in e.get('evidence_ids',[]):
                if ref not in known or known[ref]>e['available_at']:raise ValueError('NONCAUSAL_EVENT_ANCHOR:'+e['id'])
                counts['event_anchors']+=1
        last=-1
        for raw, in db.execute('SELECT payload FROM snapshots ORDER BY as_of'):
            s=json.loads(raw)
            if s['as_of']<=last or digest({k:v for k,v in s.items() if k!='id'})[:24]!=s['id']:raise ValueError('SNAPSHOT_ORDER_OR_HASH')
            last=s['as_of'];counts['immutable_snapshots']+=1
            ids=set(s['decision']['evidence_ids'])
            for c in s['context'].values():
                for r in c['ranges'].values():ids.update(r['evidence_ids'])
                ids.update(e['id'] for e in c['active_levels']+c['active_zones'])
            for setup in s['setups']:
                ids.update(setup['supporting']+setup['contradicting'])
                if set(setup['source_rule_ids'])-knowledge.verified:raise ValueError('UNVERIFIED_DECISION_SOURCE')
            for ident in ids:
                if ident not in known or known[ident]>s['as_of']:raise ValueError('SNAPSHOT_FUTURE_OR_MISSING_ANCHOR')
                counts['snapshot_anchors']+=1
            if any(r['approved'] for r in s['decision']['risk_reviews'].values()):raise ValueError('UNEXPECTED_ORDER_PERMISSION')
        for raw, in db.execute('SELECT payload FROM transitions'):
            t=json.loads(raw)
            if t['after'] not in ALLOWED[t['before']]:raise ValueError('LIFECYCLE_AUDIT')
            counts['legal_transitions']+=1
        for raw, in db.execute('SELECT payload FROM retrievals'):
            r=json.loads(raw)
            for item in r['records']:
                if item['id'] not in knowledge.verified or item['references']!=knowledge.by_id[item['id']]['references']:
                    raise ValueError('RETRIEVAL_REFERENCE_DRIFT')
                counts['retrieved_source_records']+=1
    return dict(status='PASS',**counts,live_orders=0,paid_calls=0,
                scope='All recorded anchors, chronological snapshots, reference identities and transitions; no claim of semantic truth or profitability.')

def evaluate(store,tapes,start,end,baseline_path=None):
    baseline=module('ict_observer_baseline',baseline_path or REPO/'research/ict_model_tests_20261119/engine.py')
    grouped=defaultdict(list)
    for b in tapes['NQ']:
        grouped[clock(b['ts']).date().isoformat()].append(baseline.Bar(b['ts'],b['contract'].split(':',1)[1],b['open'],b['high'],b['low'],b['close'],b['received_at']))
    comparisons=[];candidates=[]
    for day,bars in sorted(grouped.items()):
        if not start<=day<=end or date.fromisoformat(day).weekday()>=5:continue
        missing=set(range(510,961))-{clock(b.ts).hour*60+clock(b.ts).minute for b in bars}
        d=date.fromisoformat(day)-timedelta(days=1)
        while d.weekday()>=5 or d.isoformat()=='2026-05-25':d-=timedelta(days=1)
        prev=grouped.get(d.isoformat(),[]);rth=[b for b in prev if 570<=baseline.minute(b.ts)<960]
        prior=None
        if len(rth)==390 and len({b.instrument for b in bars+rth})==1:
            prior=dict(day=d.isoformat(),high=max(b.h for b in rth),low=min(b.l for b in rth),available_at=rth[-1].available)
        for variant in ('R2022-M1-C2','RIFVG-MOC-SHORT-M1'):
            if missing or day=='2026-05-25':comparisons.append(dict(day=day,variant=variant,status='SKIP_DATA_OR_HOLIDAY'));continue
            detector=baseline.Detector(variant,prior)
            for b in bars:
                if 510<=baseline.minute(b.ts)<960:detector.push(b)
            comparisons.append(dict(day=day,variant=variant,status=detector.reason,candidates=int(bool(detector.candidate))))
            if detector.candidate:candidates.append(detector.candidate)
    with store.readonly() as db:
        # First READY transition freezes the proposed plan; later invalidation cannot
        # retrospectively change that prediction. No last-state selection bias.
        snaps=[json.loads(r[0]) for r in db.execute("SELECT payload FROM snapshots WHERE decision='TRADE_CANDIDATE' ORDER BY as_of")]
        setups={}
        for snap in snaps:
            for s in snap['setups']:
                if s['plan'] and s['id'] not in setups:setups[s['id']]=s
        latencies=[json.loads(r[0])['processing_ms'] for r in db.execute('SELECT payload FROM measurements')]
    advanced=[]
    for s in setups.values():
        p=s['plan'];day=clock(p['available_at']).date().isoformat()
        local=clock(p['available_at']);minute=local.hour*60+local.minute
        if not start<=day<=end or not 570<=minute<=960:continue
        c=dict(candidate_id=s['id'],variant=POLICY['id'],side=s['direction'],entry=p['entry'],stop=p['stop'],target=p['target'],
               decision_ts=p['available_at'],expiry_ts=p['expires_at'],deadline_ts=p['expires_at'],instrument_id=s['contract'].split(':',1)[1])
        # Only raw price path enters this retrospective outcome calculation.
        for latency in (0,1000,5000):
            advanced.append(dict(baseline.replay_ohlc(c,grouped[day],latency,1),family='EXPLORATORY_SEQUENCE',day=day))
    base_results=[dict(baseline.replay_ohlc(c,grouped[c['session']],1000,1),family=c['variant'],day=c['session']) for c in candidates]
    import numpy as np
    return dict(baselines=comparisons,baseline_candidates=len(candidates),exploratory_candidates=len({r['candidate_id'] for r in advanced}),
        observed_candidate_plans=len(setups),
        outcomes=advanced+base_results,primary_exploratory_outcomes=dict(Counter(r['state'] for r in advanced if r['latency_ms']==1000)),
        baseline_outcomes=dict(Counter(r['state'] for r in base_results)),
        baseline_definition='Unmodified existing frozen 2022/IFVG detectors on identical chronological NQ data, plus abstain baseline.',
        abstain_baseline=dict(trades=0,pnl=0,exposure=0),
        actual_trades=0,actual_broker_fills=0,net_profit_factor=None,net_expectancy=None,win_rate=None,max_drawdown=None,
        calibration=None,fees='No historical fee/spread evidence; no net performance estimate.',
        execution_limits='Conditional OHLC path envelopes only. Trade-through is a model assumption, not proof of a fill. Ambiguous minutes remain ambiguous.',
        latency=dict(processing_ms_p50=float(np.percentile(latencies,50)) if latencies else None,
                     processing_ms_p95=float(np.percentile(latencies,95)) if latencies else None,
                     feed_latency='NOT_MEASURED',llm_latency=None,broker_latency=None))
