"""Exact original A/B Q1 review in the shared HYBRID worker; no broker writes.

Opt-in collection only. An ALLOW is selection evidence, never permission to
bypass the account-local execution policy or Directional-only broker route.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ab_quality

VERSION = 'ab-q1-exact-ict-review-v3'
NY = ZoneInfo('America/New_York')
DEFAULT_SESSIONS = 'ASIA,PREM,NYAM,NYPM'
GEOMETRY = ('direction','entry_price','stop_price','target_price','activation_ms','expiry_ms')


def enabled(config):
    return config.get('AB_Q1_AI_REVIEW_ENABLED', 'false') == 'true'


def session_at(timestamp, config):
    local = datetime.fromtimestamp(timestamp, NY)
    allowed = {s.strip() for s in config.get('AB_Q1_AI_SESSIONS', DEFAULT_SESSIONS).split(',')}
    if not allowed or not allowed <= {'ASIA','PREM','NYAM','NYPM'}:
        return None
    minute = local.hour*60+local.minute
    if 'ASIA' in allowed and ((minute >= 1080 and local.weekday() in (6,0,1,2,3))
                             or (minute < 120 and local.weekday() in (0,1,2,3,4))):
        end = local.replace(hour=2, minute=0, second=0, microsecond=0)
        if minute >= 1080:end += timedelta(days=1)
        return dict(name='ASIA', ends_at=end.timestamp())
    if local.weekday() < 5:
        for name,start,end in [('PREM',300,570),('NYAM',570,660),('NYPM',810,960)]:
            if name in allowed and start <= minute < end:
                return dict(name=name,ends_at=local.replace(hour=end//60,minute=end%60,second=0,microsecond=0).timestamp())
    return None


# Preserve the original scanner classification across the existing entry offset.
# Only causal scanner inputs are retained, never outcomes/account information.
SCANNER_FIELDS = ('model','cat','dir','s','u','bos_bar','bos_ms','entry','SL','TP',
                  'tp_src','fvg_lo','fvg_hi','atr5','signal_close','_signal_close')


def apply_entry_offset(signal, offset):
    """Existing offset, plus a frozen Q1 source and corrected original 2R target."""
    offset=float(offset)
    if not math.isfinite(offset):raise ValueError('q1_entry_offset_invalid')
    quality=signal.get('_ab_quality') or ab_quality.classify(signal)
    is_q1=quality.get('tier')=='Q1' and quality.get('complete') is True
    original={k:signal.get(k) for k in SCANNER_FIELDS}
    sign={'LONG':1,'SHORT':-1}[signal['dir']]
    if is_q1:
        signal['_q1_scanner_input']=original
        signal['_q1_entry_offset_points']=offset
        signal['_ab_quality']=quality
    if offset:signal['entry']=round(float(signal['entry'])+sign*offset,2)
    if is_q1 and original.get('tp_src')=='2R':
        risk=sign*(float(original['entry'])-float(original['SL']))
        expected=float(original['entry'])+sign*2*risk
        if risk<=0 or abs(float(original['TP'])-expected)>.021:
            raise ValueError('q1_original_2r_target_inconsistent')
        risk=sign*(float(signal['entry'])-float(signal['SL']))
        signal['TP']=round(float(signal['entry'])+sign*2*risk,2)
    return signal


def input_reason(exc):
    if isinstance(exc,KeyError):return 'q1_required_field_missing:'+str(exc.args[0])
    value=str(exc)
    return value if value.startswith('q1_') else 'q1_invalid_input:'+type(exc).__name__


def record_intake(candidate_id, state, **facts):
    import execution_trace
    execution_trace.record(candidate_id,'q1_review_intake',state=state,**facts)


def candidate(signal, candidate_id, config):
    """Freeze the already generated canonical signal, not create a new setup."""
    if (signal.get('_strat', 'A/B') != 'A/B' or signal.get('_v3_directional')
            or signal.get('model') == 'A/B Directional'):
        raise ValueError('q1_original_ab_required')
    quality = signal.get('_ab_quality')
    scanner=signal.get('_q1_scanner_input')
    if scanner is not None and not isinstance(scanner,dict):raise ValueError('q1_scanner_input_invalid')
    fresh = ab_quality.classify(scanner if scanner is not None else signal)
    if scanner is not None:
        expected=dict(scanner,_ab_quality=quality)
        apply_entry_offset(expected,signal['_q1_entry_offset_points'])
        if any(expected.get(k)!=signal.get(k) for k in SCANNER_FIELDS):
            raise ValueError('q1_scanner_offset_binding_changed')
    if (not isinstance(quality,dict) or quality.get('version') != ab_quality.VERSION
            or quality.get('tier') != 'Q1' or quality.get('score') != 1
            or quality.get('complete') is not True
            or any(quality.get(k) != fresh.get(k) for k in ('tier','score','complete','checks','features'))):
        raise ValueError('q1_complete_verified_quality_required')
    direction = signal['dir']; sign = {'LONG':1,'SHORT':-1}[direction]
    entry, stop, target = (float(signal[k]) for k in ('entry','SL','TP'))
    if any(not math.isfinite(v) or v <= 0 for v in (entry,stop,target)) or sign*(entry-stop)<=0 or sign*(target-entry)<=0:
        raise ValueError('q1_original_geometry_required')
    bos = signal['bos_ms']
    if type(bos) is not int or bos<=0:raise ValueError('q1_bos_timestamp_required')
    activation = bos+60000  # BOS cannot be reviewed before its M1 candle closes.
    expiry = activation+int(config.get('EXEC_CANCEL_AFTER_SEC','600'))*1000
    if not 0 < expiry-activation <= 600000:raise ValueError('q1_expiry_outside_original_window')
    contract=config.get('AB_V3_CONTRACT')
    if not contract:raise ValueError('q1_contract_required')
    if not candidate_id:raise ValueError('q1_candidate_identity_required')
    identity=json.dumps([contract,str(candidate_id)],separators=(',',':'))
    # Only original scanner fields; never serialize signal's outcomes/account fields.
    frozen={k:signal.get(k) for k in ('model','cat','s','u','bos_bar','bos_ms','fvg_lo','fvg_hi','atr5','signal_close','_signal_close')}
    frozen['original_levels'] = dict(entry=entry, SL=stop, TP=target)
    if scanner is not None:
        frozen['scanner_input']=scanner
        frozen['entry_offset_points']=signal['_q1_entry_offset_points']
    align = math.floor if direction == 'LONG' else math.ceil
    entry, stop, target = (align(v*4)/4 for v in (entry, stop, target))
    if scanner is not None and scanner.get('tp_src')=='2R':
        target=entry+sign*2*(sign*(entry-stop))
    if sign*(entry-stop)<=0 or sign*(target-entry)<=0:
        raise ValueError('q1_tick_geometry_invalid')
    if sign*(target-entry)+1e-8 < 2*sign*(entry-stop):
        raise ValueError('q1_gross_rr_below_2')
    return dict(order_id='ABQ1_'+hashlib.sha256(identity.encode()).hexdigest(),candidate_id=str(candidate_id),
        strategy='A/B',direction=direction,entry_price=entry,stop_price=stop,target_price=target,
        activation_ms=activation,expiry_ms=expiry,contract=contract,
        q1_quality=quality,q1_source=frozen,review_only=True,
        nofill_recovery_allowed=False,maximum_entry_attempts=1)


def describe(order, contract):
    import directional_setup_review
    if order.get('strategy')!='A/B' or not str(order.get('order_id','')).startswith('ABQ1_'):
        raise ValueError('q1_exact_identity_required')
    if (order.get('recovery_version') or order.get('parent_opportunity_id')
            or order.get('nofill_recovery_allowed') is not False
            or order.get('maximum_entry_attempts') != 1):
        raise ValueError('q1_recovery_not_permitted')
    source=order['q1_source']
    signal=dict(source,_strat='A/B',dir=order['direction'],**source['original_levels'],_ab_quality=order['q1_quality'])
    if 'scanner_input' in source:
        signal.update(_q1_scanner_input=source['scanner_input'],
                      _q1_entry_offset_points=source['entry_offset_points'])
        # Fields needed to verify the original scanner/offset binding.
        signal['tp_src']=source['scanner_input'].get('tp_src')
    # Recompute Q1 against the original recorded inputs, including pre-offset
    # classification source fields, rather than accepting a supplied tier string.
    expected=candidate(signal,order['candidate_id'],dict(AB_V3_CONTRACT=contract,
        EXEC_CANCEL_AFTER_SEC=str((order['expiry_ms']-order['activation_ms'])//1000)))
    if any(expected.get(k)!=order.get(k) for k in ('order_id','contract',*GEOMETRY)):
        raise ValueError('q1_identity_or_geometry_changed')
    result=directional_setup_review.describe_geometry(order,contract)
    result.update(strategy='A/B',quality=order['q1_quality'],setup_category=source.get('cat'),
        original_ab_source=source,review_version=VERSION,
        nofill_recovery_allowed=False,maximum_entry_attempts=1)
    result['frozen_context']={'original_ab_setup':dict(status='RECORDED',value=source,
        available_at_ms=order['activation_ms'],source=dict(order_id=order['order_id'],path='original_ab_scanner'))}
    return result


def queue_signal(signal, candidate_id, service):
    if service is None or not enabled(service.config):
        record_intake(candidate_id,'DISABLED',reason='q1_review_service_unavailable_or_disabled')
        return dict(state='DISABLED',executable=False)
    try:
        order=candidate(signal,candidate_id,service.config)
        result=service.queue_setup(order)
        record_intake(candidate_id,result.get('state','UNKNOWN'),order_id=order['order_id'])
        return dict(result,order_id=order['order_id'],executable=False)
    except (KeyError,ValueError,TypeError,OverflowError) as exc:
        reason=input_reason(exc)
        record_intake(candidate_id,'INELIGIBLE',reason=reason)
        return dict(state='INELIGIBLE',reason=reason,executable=False)


def check(order, service):
    """Mandatory exact approval check; no optional bypass of WAIT or missing AI."""
    if service is None or not enabled(service.config):return 'q1_ai_disabled',None
    try:expected=describe(order,service.config.get('AB_V3_CONTRACT'))
    except (KeyError,ValueError,TypeError,OverflowError):return 'q1_setup_invalid',None
    review=service.setup_annotation(order['order_id'],order['direction'])
    if review.get('owner')!='A/B':return 'q1_ai_owner_mismatch',review
    if review.get('state')!='ALLOW':return 'q1_ai:'+str(review.get('state','UNAVAILABLE')).lower(),review
    if review.get('setup')!=expected:return 'q1_ai_setup_changed',review
    return None,review
