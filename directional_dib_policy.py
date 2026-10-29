"""Opt-in DIB 50% geometry before exact setup review. No execution permissions.

Defaults OFF. Other scanner families and their original 62% geometry are untouched.
Uses the original causal impulse, existing signed one-point offset and saved SL.
"""
import hashlib
import json
import math
import os

VERSION = 'DIRECTIONAL_DIB_FIBO50_EXACT_AI_V1'
FLAG = 'DIRECTIONAL_DIB_FIBO50_ENABLED'

def enabled():
    return os.environ.get(FLAG, 'false') == 'true'

def is_dib(category):
    return isinstance(category, str) and 'DIB' in {x.strip().upper() for x in category.split('+')}

def tick(x):
    return round(round(float(x)/.25)*.25, 2)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _num(x):
    if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x):
        raise ValueError('DIB_INVALID_PRICE')
    return float(x)

def calculate(evidence):
    """Validate independently reproducible causal price evidence, without OHLC outcomes."""
    if evidence.get('version') != VERSION or evidence.get('source') != 'ORIGINAL_DIRECTIONAL_SCANNER':
        raise ValueError('DIB_PRICE_EVIDENCE_MISSING')
    side=evidence['direction'];sign={'LONG':1,'SHORT':-1}[side]
    activation=evidence['activation_ms'];expiry=evidence['expiry_ms']
    if type(activation) is not int or activation%60000 or expiry!=activation+600000:
        raise ValueError('DIB_INVALID_VALIDITY')
    anchors=[]
    for name in ['origin','extreme']:
        b=evidence[name]
        if (type(b['time']) is not int or b['time']%60 or b['available_at']!=b['time']+60
                or b['available_at']*1000>activation or b['instrument_id']!=evidence['instrument_id']):
            raise ValueError('DIB_ANCHOR_NOT_CAUSAL_OR_WRONG_INSTRUMENT')
        op,hi,lo,cl=[_num(b[k]) for k in ['open','high','low','close']]
        if not 0<lo<=min(op,cl)<=max(op,cl)<=hi:
            raise ValueError('DIB_INVALID_ANCHOR')
        anchors.append(lo if name=='origin' and sign==1 or name=='extreme' and sign==-1 else hi)
    origin,extreme=anchors
    if evidence['origin']['time']>evidence['extreme']['time'] or sign*(extreme-origin)<=0:
        raise ValueError('DIB_INVALID_IMPULSE')
    saved=_num(evidence['original_raw_entry'])
    reconstructed=round(extreme+.62*(origin-extreme),2)
    if abs(reconstructed-saved)>.010001 or tick(reconstructed+sign)!=tick(saved+sign):
        raise ValueError('DIB_ORIGINAL_FIBONACCI_NOT_REPRODUCIBLE')
    entry=tick(round(extreme+.5*(origin-extreme),2)+sign)
    stop=tick(_num(evidence['original_stop_price']))
    risk=sign*(entry-stop)
    if risk<=0:raise ValueError('DIB_INVALID_FINAL_GEOMETRY')
    return dict(entry_price=entry,stop_price=stop,target_price=tick(entry+2*(entry-stop)),
                risk_points=round(risk,8),fraction=.5,offset_points=sign,
                activation_ms=activation,expiry_ms=expiry,direction=side)

def derive(raw, row, side):
    """Freeze original scanner impulse; hh_bar is confirmation, not the extreme."""
    if row.get('kind')!='FIBO' or row.get('dir')!=side:
        raise ValueError('DIB_ORIGINAL_FIBONACCI_REQUIRED')
    origin,bos=int(row['origin_bar']),int(row['bos_bar']);iid=int(row['instrument_id'])
    if not 0<=origin<=bos<len(raw):raise ValueError('DIB_SCANNER_INDICES_UNAVAILABLE')
    if int(raw.iloc[bos].ts_event.value//1000000)!=int(row['bos_ms']):
        raise ValueError('DIB_SCANNER_INDEX_TIMESTAMP_MISMATCH')
    bars=[]
    for i in range(origin,bos+1):
        b=raw.iloc[i];at=int(b.ts_event.value//1000000)
        if int(b.instrument_id)!=iid or at+60000>row['entry_ms']:
            raise ValueError('DIB_ANCHOR_NOT_CAUSAL_OR_WRONG_INSTRUMENT')
        if bars and at//1000!=bars[-1]['available_at']:
            raise ValueError('DIB_IMPULSE_HISTORY_GAP')
        bars.append(dict(time=at//1000,available_at=at//1000+60,instrument_id=iid,
                         **{k:float(b[k]) for k in ['open','high','low','close']}))
    extreme=(max(bars,key=lambda b:b['high']) if side=='LONG' else min(bars,key=lambda b:b['low']))
    proof=dict(version=VERSION,source='ORIGINAL_DIRECTIONAL_SCANNER',direction=side,
               instrument_id=iid,activation_ms=int(row['entry_ms']),expiry_ms=int(row['entry_ms'])+600000,
               origin=bars[0],extreme=extreme,original_raw_entry=float(row['entry']),
               original_stop_price=tick(row['SL']))
    proof['geometry']=calculate(proof)
    proof['fingerprint']=digest(proof)
    return proof

def dispatch_blocker(order, evidence):
    if not enabled():return 'DIB_DISABLED'
    if (os.environ.get('V3_AI_REVIEW_REQUIRED')!='true'
            or os.environ.get('AB_AI_ENABLED')!='true'
            or os.environ.get('AB_AI_TRIGGER') not in ('SETUP','HYBRID')):
        return 'DIB_EXACT_AI_REVIEW_REQUIRED'
    try:
        proof=evidence['dib_entry'];raw={k:v for k,v in proof.items() if k!='fingerprint'}
        if digest(raw)!=proof['fingerprint']:return 'DIB_PRICE_EVIDENCE_CHANGED'
        calc=calculate(proof)
        if calc!=proof['geometry']:return 'DIB_PRICE_EVIDENCE_CHANGED'
        for k in ('direction','activation_ms','expiry_ms','entry_price'):
            if order.get(k)!=calc[k]:return 'DIB_GEOMETRY_MISMATCH'
        for key,alias in [('stop_price','structural_sl_price'),('target_price','policy_B_target')]:
            if order.get(key,order.get(alias))!=calc[key]:return 'DIB_GEOMETRY_MISMATCH'
        if order.get('instrument_id') is not None and order['instrument_id']!=proof['instrument_id']:
            return 'DIB_INSTRUMENT_MISMATCH'
    except (KeyError,ValueError,TypeError,OverflowError):return 'DIB_PRICE_EVIDENCE_INVALID'
    return None
