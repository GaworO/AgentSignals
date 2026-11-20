"""Reproduce stored OHLC uncertainty and report conditional price-path bounds."""
import json
from collections import Counter
from pathlib import Path
from .common import module,save,digest
from .store import Store
from .market import clock

CAUSES={
 'ENTRY_IN_ACTIVATION_MINUTE':'The minute crosses the assumed decision-to-order latency; no event timestamps inside it.',
 'CANCEL_EVENT_DURING_LATENCY':'An invalidation or target touch and order activation may occur in either order inside the latency minute.',
 'ENTRY_CANCEL_ORDER_UNKNOWN':'Entry and an invalidating level are touched in the same minute; their order is unknown.',
 'EVENT_IN_EXPIRY_MINUTE':'Order expiration and a price touch occur inside the same OHLC minute.'}

def audit(run_folder,destination):
    root=Path(run_folder);store=object.__new__(Store);store.path=root/'observer.sqlite3'
    original=json.loads((root/'evaluation.json').read_text());engine=module('ict_frozen_ambiguity_engine',root/'baseline-engine.py')
    with store.readonly() as db:
        raws=[json.loads(x[0]) for x in db.execute("SELECT payload FROM bars WHERE symbol='NQ' ORDER BY ts")]
        snapshots=[json.loads(x[0]) for x in db.execute("SELECT payload FROM snapshots WHERE decision='TRADE_CANDIDATE' ORDER BY as_of")]
    tapes={}
    for b in raws:
        tapes.setdefault(clock(b['ts']).date().isoformat(),[]).append(engine.Bar(b['ts'],b['contract'].split(':',1)[1],b['open'],b['high'],b['low'],b['close'],b['received_at']))
    plans={}
    for snap in snapshots:
        for s in snap['setups']:
            if s.get('plan') and s['id'] not in plans:plans[s['id']]=s
    results=[];reproduced=0;envelopes=[]
    for old in original['outcomes']:
        if old['family']!='EXPLORATORY_SEQUENCE' or old['latency_ms']!=1000:continue
        s=plans[old['candidate_id']];p=s['plan'];day=old['day'];direction=1 if s['direction']=='LONG' else -1
        c=dict(candidate_id=s['id'],variant='EXPLORATORY_SEQUENCE',side=s['direction'],entry=p['entry'],stop=p['stop'],target=p['target'],decision_ts=p['available_at'],expiry_ts=p['expires_at'],deadline_ts=p['expires_at'],instrument_id=s['contract'].split(':',1)[1])
        new=engine.replay_ohlc(c,tapes[day],1000,1)
        if any(new[k]!=old[k] for k in ('state','reason','gross_points_interval','fill_ts','exit_ts')):raise ValueError('FROZEN_OUTCOME_NOT_REPRODUCED:'+s['id'])
        reproduced+=1
        if new.get('ambiguities'):envelopes.append(dict(candidate_id=s['id'],ambiguities=new['ambiguities'],gross_points_interval=new['gross_points_interval']))
        if old['state']!='AMBIGUOUS':continue
        future=[b for b in tapes[day] if c['decision_ts']<=b.ts<=c['deadline_ts'] and b.instrument==c['instrument_id']]
        complete=bool(future) and future[0].ts==c['decision_ts'] and future[-1].ts==c['deadline_ts'] and all(b.ts-a.ts==60 for a,b in zip(future,future[1:]))
        bounds=None
        if complete:
            # Includes no-fill at zero. These deliberately broad stress bounds use
            # every observed price up to deadline, NOT an asserted feasible fill.
            low=min(b.l for b in future);high=max(b.h for b in future)
            moves=[direction*(x-c['entry']) for x in (low,high)]
            bounds=[min(0,min(moves)-.25),max(0,max(moves))]
        results.append(dict(candidate_id=s['id'],day=day,side=s['direction'],plan=p,
            reason=old['reason'],cause=CAUSES.get(old['reason'],'Unresolved OHLC ordering'),ambiguous_minute=old['exit_ts'],
            fill_confirmed=False,resolved=False,price_path_stress_bounds_points=bounds,
            bound_scope='Conditional fill at LIMIT, at most one adverse tick beyond observed price range, exit by deadline; includes zero/no fill. Not a realizable-path or net-PnL estimate.',
            timeline_complete=complete,missing=['timestamped quotes/trades','order acknowledgement/queue evidence','actual fees and slippage'],
            order_state_inconsistency_detected=False,source='FROZEN_OHLC_MODEL_NOT_ACTUAL_ORDER_JOURNAL'))
    report=dict(status='REPRODUCED_WITH_UNRESOLVED_EXECUTION',run_id=root.name,reproduced_primary_candidates=reproduced,
        ambiguous_count=len(results),reason_counts=dict(Counter(r['reason'] for r in results)),resolved_count=0,cases=results,
        other_same_bar_exit_envelopes=envelopes,net_pnl=None,actual_fills=0,
        conclusion='Minute OHLC cannot establish event order. All original uncertainties preserved; no ambiguous case counted as a win.',
        source_evaluation_sha256=__import__('hashlib').sha256((root/'evaluation.json').read_bytes()).hexdigest())
    save(destination,report);return report
