"""Versioned, causal context selection. Research-only: no order execution.

Pivot geometry (two left / two right bars) is a candidate-generation convention,
not an assertion about Tanya's swing definition. AI selection remains inference.
"""
from copy import deepcopy
import math
from context_layer import build_packet, canonical_hash, validate_response, review_response
from context_api import output_schema as v2_schema, object_schema

SPEC='tanya-evidence-baseline-2026-10-08-v1'
PROMPT_VERSION='context-selection-v3.0'
STR={'type':'string'}
NSTR={'type':['string','null']}
INT={'type':'integer'}
NINT={'type':['integer','null']}
NUM={'type':'number'}
NBOOL={'type':['boolean','null']}
def nullable(s): return {'anyOf':[s,{'type':'null'}]}
def enum(v): return {'type':'string','enum':v}
def array(s): return {'type':'array','items':s}

def output_schema():
    point=object_schema(dict(evidence_id=STR,field=enum(['high','low','open','close','price','lower','upper'])))
    stop=object_schema(dict(anchor=point,buffer_ticks={'type':'integer'},price=NUM,reason=STR))
    target=object_schema(dict(kind=enum(['level','extension_minus_one']),anchor=point,
        extreme=nullable(point),price=NUM,quantity=NINT,reason=STR))
    swing=object_schema(dict(evidence_id=STR,break_mode=enum(['wick','close']),reason=STR))
    return object_schema(dict(schema_version={'type':'integer','enum':[3]},spec_version={'type':'string','enum':[SPEC]},
        context=v2_schema(),variant_id=nullable(enum(['V01','V02','V03','V04','V05','V06'])),
        analysis_symbol=nullable(enum(['ES','MNQ'])),execution_symbol=nullable(enum(['ES','MES','NQ','MNQ'])),
        timeframe_minutes=NINT,poi_evidence_ids=array(STR),selected_swing=nullable(swing),
        require_swing_break=NBOOL,entry_mode=enum(['immediate_after_confirmation','retracement','unknown']),
        invalidation=nullable(point),initial_stop=nullable(stop),initial_quantity=NINT,
        management=object_schema(dict(policy=enum(['structural_trail','BE_after_first_target',
            'tolerate_small_gap_failure','fixed_bracket_research','unknown']),targets=array(target),
            partial_policy=NSTR,time_exit_at=NINT,reason=STR)),
        reentry_reason=NSTR,missing_fields=array(STR),rationale=STR))

def check_schema(value,schema,path='$'):
    """Validate the exact small JSON-schema subset emitted above, without deps."""
    if 'anyOf' in schema:
        for s in schema['anyOf']:
            try:check_schema(value,s,path);return
            except ValueError:pass
        raise ValueError(path+': no permitted type')
    types=schema.get('type');types=types if isinstance(types,list) else [types]
    valid={'null':value is None,'boolean':type(value) is bool,'integer':type(value) is int,
           'number':type(value) in (int,float) and math.isfinite(value),
           'string':isinstance(value,str),'object':type(value) is dict,'array':type(value) is list}
    if not any(valid.get(t,False) for t in types):raise ValueError(path+': invalid type')
    if 'enum' in schema and value not in schema['enum']:raise ValueError(path+': invalid enum')
    if value is None:return
    if type(value) is dict:
        if set(value)!=set(schema['required']):raise ValueError(path+': missing/extra fields')
        for k,v in value.items():check_schema(v,schema['properties'][k],path+'.'+k)
    if type(value) is list:
        for i,v in enumerate(value):check_schema(v,schema['items'],path+'['+str(i)+']')

def build_v3_packet(markets,as_of,max_bars=80):
    p=build_packet(markets,as_of,max_bars)
    p.pop('packet_id')
    ev=p['evidence'];addition={}
    for symbol in ('ES','MNQ'):
        for tf in (1,2,3,5,15,60,240):
            bars=sorted([(k,e) for k,e in ev.items() if e['kind']=='bar' and e['symbol']==symbol and e['timeframe']==tf],key=lambda x:x[1]['time'])
            for i in range(2,len(bars)-2):
                group=bars[i-2:i+3];b=group[2][1]
                if any(group[n+1][1]['time']-group[n][1]['time']!=tf*60 for n in range(4)):continue
                for side in ('high','low'):
                    cmp=(lambda a,z:a>z) if side=='high' else (lambda a,z:a<z)
                    if all(cmp(b[side],g[1][side]) for n,g in enumerate(group) if n!=2):
                        key=f'{symbol}:PIVOT:{tf}:{b["time"]}:{side}'
                        addition[key]=dict(kind='pivot',symbol=symbol,timeframe=tf,side=side,price=b[side],
                            pivot_time=b['time'],available_at=group[-1][1]['available_at'],evidence_ids=[k for k,e in group],
                            convention='strict two-left/two-right closed bars; not a verified trader selector')
            if tf not in (60,240):continue
            for i in range(2,len(bars)):
                group=bars[i-2:i+1];a,b,c=[x[1] for x in group]
                if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60:continue
                side='bull' if c['low']>a['high'] else 'bear' if c['high']<a['low'] else None
                if side:
                    key=f'{symbol}:HTF_FVG:{tf}:{c["available_at"]}:{side}'
                    # Mitigation/invalidation history is not asserted by merely listing formation.
                    addition[key]=dict(kind='HTF_FVG',symbol=symbol,timeframe=tf,side=side,
                        lower=a['high'] if side=='bull' else c['high'],upper=c['low'] if side=='bull' else a['low'],
                        available_at=c['available_at'],evidence_ids=[k for k,e in group],
                        status='formed; subsequent respect/failure must be assessed from available bars')
    ev.update(addition)
    p['selection_contract_version']=3
    p['conventions']['pivots']='strict two-left/two-right; available only after right-hand bars close; research catalogue, not trader rule'
    p['conventions']['htf_fvg']='formation only; no assumed active status or all-time-high history'
    p['position_state']='not_supplied; isolated context evaluation, not a sequential execution simulation'
    p['packet_id']=canonical_hash(p)
    check_packet(p)
    return p

def check_packet(p):
    if p.get('input_mode')!='market_only' or p.get('labels_included') is not False:
        raise ValueError('Only label-free market packets accepted')
    if canonical_hash({k:v for k,v in p.items() if k!='packet_id'})!=p.get('packet_id'):
        raise ValueError('Packet hash mismatch')
    if type(p['as_of']) is not int:raise ValueError('Cutoff must be an integer')
    for k,e in p['evidence'].items():
        if type(e.get('available_at')) is not int or e['available_at']>p['as_of']:
            raise ValueError('Unknown/future evidence availability: '+k)
        if e['kind']=='bar' and e['time']+60*e['timeframe']!=e['available_at']:
            raise ValueError('Bar must be fully closed')
        for ref in e.get('evidence_ids',[]):
            if ref not in p['evidence'] or p['evidence'][ref]['available_at']>e['available_at']:
                raise ValueError('Evidence depends on a missing/later observation')

def point_price(p,point,symbol):
    e=p['evidence'].get(point['evidence_id'])
    if not e or e['available_at']>p['as_of'] or e['symbol']!=symbol:
        raise ValueError('Price anchor missing, future or wrong instrument')
    allowed={'bar':{'high','low','open','close'},'pivot':{'price'},'level':{'price'},
             'FVG':{'lower','upper'},'IFVG':{'lower','upper'},'HTF_FVG':{'lower','upper'},'window':{'high','low'}}
    if point['field'] not in allowed.get(e['kind'],set()):raise ValueError('Unsupported anchor field')
    if e['kind']=='window' and not e['complete']:raise ValueError('Incomplete window cannot anchor an order plan')
    return e[point['field']]

def validate_decision(p,d):
    check_packet(p);check_schema(d,output_schema())
    c=d['context'];base=validate_response(p,c);missing=[];measured={}
    if not d['rationale'].strip() or not d['management']['reason'].strip():raise ValueError('Reasons required')
    for ref in d['poi_evidence_ids']:
        if ref not in p['evidence']:raise ValueError('Unknown POI reference')
    candidate=c['decision']=='candidate';direction=c['bias'];symbol=d['analysis_symbol']
    if candidate:
        if d['variant_id']!='V01' or symbol!='MNQ' or d['execution_symbol']!='MNQ':
            raise ValueError('This release measures MNQ inversion candidates only; abstain for unsupported variants')
        event=p['evidence'][c['selected_trigger_id']]
        if d['timeframe_minutes']!=event['timeframe']:raise ValueError('Timeframe differs from selected trigger')
        if c['range_selection'] and p['evidence'][c['range_selection']['window_id']]['symbol']!=symbol:
            raise ValueError('Selected range must belong to analysis instrument')
        if not d['poi_evidence_ids']:missing.append('poi_selection')
        if c['range_selection'] is None:missing.append('range_selection')
        if c['requires_smt'] is None:missing.append('requires_smt')
        if c['requires_smt'] is True and c['smt_selection'] is None:missing.append('smt_anchors')
        if d['entry_mode']=='unknown':missing.append('entry_mode')
    if d['selected_swing']:
        s=d['selected_swing'];e=p['evidence'].get(s['evidence_id'])
        if not e or e['kind']!='pivot' or e['symbol']!=symbol:raise ValueError('Selected swing needs a causal same-symbol pivot')
        if direction not in ('long','short') or e['side']!=('high' if direction=='long' else 'low'):raise ValueError('Wrong swing side')
        if candidate:
            trigger=p['evidence'][c['selected_trigger_id']]
            if e['available_at']>trigger['available_at']:raise ValueError('Swing was not confirmed when selected trigger occurred')
            ref=f'{symbol}:M{trigger["timeframe"]}:{trigger["available_at"]-trigger["timeframe"]*60}'
            bar=p['evidence'][ref];v=bar['close'] if s['break_mode']=='close' else bar[e['side']]
            measured['swing_broken']=(v>e['price'] if direction=='long' else v<e['price'])
    if candidate and d['require_swing_break'] is None:missing.append('require_swing_break')
    if candidate and d['require_swing_break'] is True and not measured.get('swing_broken'):missing.append('selected_swing_break')
    latest=sorted([e for e in p['evidence'].values() if e['kind']=='bar' and e['symbol']==symbol and e['timeframe']==1],key=lambda e:e['available_at'])
    reference=latest[-1]['close'] if latest else None
    if d['invalidation'] is not None:measured['invalidation']=point_price(p,d['invalidation'],symbol)
    elif candidate:missing.append('invalidation')
    stop=d['initial_stop']
    if stop is not None:
        if direction not in ('long','short') or reference is None:raise ValueError('Stop requires direction and current reference')
        if stop['buffer_ticks']<0:raise ValueError('Stop buffer must be nonnegative')
        anchor=point_price(p,stop['anchor'],symbol)
        price=anchor+(-1 if direction=='long' else 1)*stop['buffer_ticks']*0.25
        if not math.isclose(price,stop['price'],abs_tol=1e-8,rel_tol=0):raise ValueError('Stop does not match cited anchor and buffer')
        if not math.isclose(price*4,round(price*4),abs_tol=1e-8,rel_tol=0):raise ValueError('Stop off tick grid')
        if (price>=reference if direction=='long' else price<=reference):raise ValueError('Stop is on wrong side of current reference price')
        measured['stop_price']=price
        measured['reference_close_not_fill']=reference
    elif candidate:missing.append('initial_stop')
    qty=d['initial_quantity']
    if qty is not None and qty<=0:raise ValueError('Quantity must be positive')
    if candidate and qty is None:missing.append('initial_quantity_and_risk_budget')
    total=0
    for t in d['management']['targets']:
        if direction not in ('long','short') or reference is None:raise ValueError('Target needs direction and reference')
        price=point_price(p,t['anchor'],symbol)
        if t['kind']=='extension_minus_one':
            if t['extreme'] is None:raise ValueError('Projection requires two anchors')
            extreme=point_price(p,t['extreme'],symbol)
            if price==extreme:raise ValueError('Projection anchors must differ')
            price=2*price-extreme
        elif t['extreme'] is not None:raise ValueError('Simple level target has no second anchor')
        if not math.isclose(price,t['price'],abs_tol=1e-8,rel_tol=0):raise ValueError('Target does not match anchor calculation')
        if (price<=reference if direction=='long' else price>=reference):raise ValueError('Target is on wrong side of reference')
        if t['quantity'] is not None:
            if t['quantity']<=0:raise ValueError('Target quantity must be positive')
            total+=t['quantity']
    if qty is not None and total>qty:raise ValueError('Target quantities exceed initial size')
    management=d['management']
    if management['time_exit_at'] is not None and management['time_exit_at']<=p['as_of']:raise ValueError('Time exit already expired')
    if candidate:
        if management['policy']=='unknown':missing.append('management_policy')
        if not management['targets']:missing.append('target_plan')
        if management['partial_policy'] is None:missing.append('partial_policy')
    shadow=review_response(p,c,True)
    # A geometrically coherent plan still lacks validated contextual fidelity,
    # risk limits, position state and executable order management.
    missing=sorted(set(missing+d['missing_fields']))
    state=('CONTEXT_ONLY_INCOMPLETE_PLAN' if shadow['state']=='CONDITIONS_MET' and missing else shadow['state'])
    return dict(valid=True,executable=False,decision_kind='context_research',
        review_state=state,context_review_state=shadow['state'],context_inference_accepted_for_review_only=True,
        missing_plan_fields=missing,measurements=measured,
        geometry=base['geometry'],semantic_fidelity_verified=False,
        order_readiness='NOT_IMPLEMENTED',account_risk_verified=False,
        selection_recorded_as_of=p['as_of'],historical_selection_not_live_timestamp=True)

def unknown_decision(p):
    """Test/setup template, NEVER a fabricated model response."""
    from context_layer import CLAIMS
    c=dict(schema_version=2,packet_id=p['packet_id'],as_of=p['as_of'],bias='unknown',decision='abstain',
        setup='unknown',requires_smt=None,selected_trigger_id=None,smt_selection=None,range_selection=None,
        claims={k:dict(value=None,evidence_ids=[],reason='Unknown') for k in CLAIMS},
        missing_context=['No model response'],rationale='Schema template only')
    return dict(schema_version=3,spec_version=SPEC,context=c,variant_id=None,analysis_symbol=None,
        execution_symbol=None,timeframe_minutes=None,poi_evidence_ids=[],selected_swing=None,
        require_swing_break=None,entry_mode='unknown',invalidation=None,initial_stop=None,initial_quantity=None,
        management=dict(policy='unknown',targets=[],partial_policy=None,time_exit_at=None,reason='Unknown'),
        reentry_reason=None,missing_fields=['No model response'],rationale='Schema template only')
