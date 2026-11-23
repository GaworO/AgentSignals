"""Saved-candle chart evidence. No illustrative prices, fills or source qualification."""
import json
import statistics
from .common import digest
from .shadow_sessions import session_at,window_checks,correlation

def followup_due(setup,old,ctx,events,at):
    if not old:return False
    own=ctx.get(setup['symbol'],{});last=own.get('latest')
    if not last or last['available_at']!=at:return False
    state=old.get('chart_state',{})
    for z in state.get('zones',[]):
        if z['id'] not in state.get('retested',[]) and last['ts']>=z['available_at'] and last['low']<=z['upper'] and last['high']>=z['lower']:
            return True
        if z['id'] not in state.get('inverted',[]) and any(e.get('kind')=='IFVG' and z['id'] in e.get('evidence_ids',[]) for e in events):return True
    return False

def build_chart(db,packet,setup,ctx,at):
    evidence=packet['evidence'];symbol=setup['symbol'];contract=setup['contract'];annotations=[];derived=[]
    def get(ident):
        if ident not in evidence:
            row=db.execute('SELECT payload FROM events WHERE id=?',(ident,)).fetchone()
            if row is None:row=db.execute('SELECT payload FROM frames WHERE id=?',(ident,)).fetchone()
            if row is None:raise ValueError('CHART_MISSING_OBSERVATION:'+ident)
            evidence[ident]=json.loads(row[0])
        e=evidence[ident]
        if e['available_at']>at:raise ValueError('CHART_FUTURE_OBSERVATION')
        for ref in e.get('evidence_ids',[]):
            if ref not in evidence:get(ref)
        return e
    def ann(kind,e,status='VERIFIED_GEOMETRY',**details):
        if e['symbol']!=symbol or e['contract']!=contract:raise ValueError('CHART_CROSS_INSTRUMENT')
        annotations.append(dict(kind=kind,observation_id=e['id'],available_at=e['available_at'],
            ts=e['ts'],timeframe=e['timeframe'],evidence_ids=e.get('evidence_ids',[]),status=status,**details))
    root=get(setup['root_event']);refs=[get(i) for i in root.get('evidence_ids',[])]
    level=next((r for r in refs if 'price' in r),None);raid=next((r for r in refs if 'close' in r),None)
    sweep_ok=bool(level and raid and level['available_at']<=raid['ts'] and
        ((setup['direction']=='SHORT' and raid['high']>level['price'] and raid['close']<level['price']) or
         (setup['direction']=='LONG' and raid['low']<level['price'] and raid['close']>level['price'])))
    if level:
        source=[get(i) for i in level.get('evidence_ids',[])]
        anchors=[r['id'] for r in source if r.get('high' if level['side']=='HIGH' else 'low')==level['price']]
        ann('LIQUIDITY_REFERENCE',level,price=level['price'],source_candles=anchors,
            meaning='Confirmed historical price reference, not measured resting orders')
    ann('SWEEP',root,'VERIFIED_GEOMETRY' if sweep_ok else 'SWEEP_UNCONFIRMED',
        price=root['price'],extreme=root['extreme'],reclaim=sweep_ok,source_candle=raid['id'] if raid else None)
    zones=[]
    for ident in setup['supporting']:
        e=get(ident);kind=e.get('kind');rs=[get(i) for i in e.get('evidence_ids',[])]
        if kind=='DISPLACEMENT':
            f=rs[0];hist=rs[1:];span=f['high']-f['low'];median=statistics.median(b['high']-b['low'] for b in hist) if hist else 0
            ok=len(hist)==20 and median==e['prior_median'] and span==e['range'] and span>=median*1.5 and span>0 and abs(f['close']-f['open'])>=.6*span
            ann(kind,e,'VERIFIED_RESEARCH_MEASUREMENT' if ok else 'UNCONFIRMED',price=f['close'],
                measurement=dict(range=span,prior20_median=median,body_fraction=abs(f['close']-f['open'])/span if span else None),
                meaning='1.5× range and 60% body are existing engineering thresholds, not universal ICT rules')
        elif kind=='STRUCTURE_BREAK':
            lev=next((r for r in rs if 'price' in r),None);f=next((r for r in rs if 'close' in r),None)
            ok=bool(lev and f and lev['available_at']<=f['ts'] and (f['close']>lev['price'] if e['direction']=='LONG' else f['close']<lev['price']))
            ann(kind,e,'VERIFIED_CLOSE_CROSS' if ok else 'UNCONFIRMED',price=e['price'],reference_id=lev['id'] if lev else None,
                meaning='A close cross is not automatically a source-qualified MSS')
        elif kind=='FVG':
            if len(rs)!=3:raise ValueError('FVG_PARENT_COUNT')
            a,b,c=rs;lo=a['high'] if e['direction']=='LONG' else c['high'];hi=c['low'] if e['direction']=='LONG' else a['low']
            ok=a['end']==b['ts'] and b['end']==c['ts'] and lo<hi and (lo,hi)==(e['lower'],e['upper'])
            ann(kind,e,'VERIFIED_GEOMETRY' if ok else 'UNCONFIRMED',lower=e['lower'],upper=e['upper'],direction=e['direction'])
            zones.append(e)
    # Inversions must be this instrument's actual closed-candle event and cite its own FVG.
    for z in list(zones):
        rows=db.execute("SELECT payload FROM events WHERE symbol=? AND kind='IFVG' AND as_of<=? AND as_of>? ORDER BY as_of",(symbol,at,z['available_at']))
        for row in rows:
            e=json.loads(row[0])
            if e['contract']!=contract or z['id'] not in e['evidence_ids']:continue
            get(e['id']);f=get(e['evidence_ids'][-1])
            ok=f['ts']>=z['available_at'] and (f['close']<z['lower'] if z['direction']=='LONG' else f['close']>z['upper'])
            ann('IFVG',e,'VERIFIED_CLOSING_INVERSION' if ok else 'UNCONFIRMED',lower=e['lower'],upper=e['upper'],direction=e['direction'],original_fvg=z['id'])
            zones.append(e)
    for z in zones:
        # M1 touch is a directly observed zone intersection, not an inferred fill.
        row=db.execute("SELECT payload FROM frames WHERE symbol=? AND contract=? AND timeframe=1 AND ts>=? AND available_at<=? AND json_extract(payload,'$.low')<=? AND json_extract(payload,'$.high')>=? ORDER BY ts LIMIT 1",
            (symbol,contract,z['available_at'],at,z['upper'],z['lower'])).fetchone()
        if row:
            f=json.loads(row[0]);get(f['id'])
            e=dict(kind='ZONE_RETEST',symbol=symbol,contract=contract,timeframe=1,ts=f['ts'],available_at=f['available_at'],
                evidence_ids=[z['id'],f['id']],lower=z['lower'],upper=z['upper'],scope='FIRST_LATER_M1_ZONE_TOUCH_NOT_ENTRY_OR_FILL')
            e['id']=digest(e);evidence[e['id']]=e;derived.append(e)
            ann('RETEST',e,price=max(z['lower'],min(f['close'],z['upper'])),zone_id=z['id'],source_candle=f['id'],
                entry_windows=window_checks(f['available_at'],z['available_at']))
    tr=setup.get('last_transition',{})
    if tr.get('reason')=='CLOSE_BEYOND_SWEEP_EXTREME':
        for ident in tr.get('evidence_ids',[]):
            f=get(ident)
            if 'close' in f:ann('INVALIDATION',f,'RESEARCH_REVERSAL_INVALIDATION',price=f['close'],level=setup['sweep_extreme'])
    latest=ctx[symbol]['latest'];targets=[]
    for l in ctx[symbol]['active_levels']:
        if l['contract']!=contract or l['available_at']>at:continue
        get(l['id']);eligible=(l['side']=='LOW' and l['price']<latest['close']) if setup['direction']=='SHORT' else (l['side']=='HIGH' and l['price']>latest['close'])
        if not eligible:continue
        targets.append(dict(observation_id=l['id'],price=l['price'],side=l['side'],timeframe=l['timeframe'],available_at=l['available_at'],
            distance=abs(l['price']-latest['close']),eligible='DIRECTIONAL_UNCONSUMED_PRICE_REFERENCE',source_preference='UNRESOLVED'))
    targets.sort(key=lambda x:(x['distance'],x['observation_id']))
    for i,t in enumerate(targets):t['numerical_rank']=i+1
    # Numeric targets are plotted without inventing an approved entry or preferred target.
    for t in targets[:5]:ann('TARGET_CANDIDATE',get(t['observation_id']),price=t['price'],rank=t['numerical_rank'],meaning='Numerical proximity only; source preference unresolved')
    frames={}
    for tf,limit in ((1,300),(5,180),(15,100),(60,80),(240,30),(1440,10)):
        rows=db.execute('SELECT payload FROM frames WHERE symbol=? AND contract=? AND timeframe=? AND available_at<=? ORDER BY ts DESC LIMIT ?', (symbol,contract,tf,at,limit)).fetchall()
        frames[str(tf)]=[json.loads(r[0]) for r in reversed(rows)]
    for bars in frames.values():
        for f in bars:
            if f['available_at']>at or f['symbol']!=symbol:raise ValueError('UNSAFE_CHART_CANDLE')
    state=dict(zones=[{k:z[k] for k in ('id','lower','upper','available_at','timeframe')} for z in zones],
               retested=[a['zone_id'] for a in annotations if a['kind']=='RETEST'],
               inverted=[a['original_fvg'] for a in annotations if a['kind']=='IFVG'])
    return dict(version='shadow-real-chart-v1',symbol=symbol,direction=setup['direction'],as_of=at,
        detected_at=setup['created_at'],origin_session=session_at(setup['created_at']),observation_session=session_at(at),
        research_expires_at=setup['expires_at'],expiry_scope='Engineering 90-minute selector expiry, not a source entry window',
        correlation=correlation(setup),source_windows=window_checks(at),annotations=annotations,frames=frames,
        targets=targets,target_coverage='Available retained detector levels; not an exhaustive discretionary liquidity inventory',
        distance_reference=dict(price=latest['close'],observation_id=latest['id'],meaning='Last available close, not entry'),
        entry=None,stop=None,preferred_target=None,source_qualified=False,execution_eligible=False,
        llm_status='PENDING_LLM',chart_state=state,derived_observations=derived)
