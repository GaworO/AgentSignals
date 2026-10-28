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

VERSION = 'ab-q1-exact-ict-review-v1'
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


def candidate(signal, candidate_id, config):
    """Freeze the already generated canonical signal, not create a new setup."""
    if (signal.get('_strat', 'A/B') != 'A/B' or signal.get('_v3_directional')
            or signal.get('model') == 'A/B Directional'):
        raise ValueError('q1_original_ab_required')
    quality = signal.get('_ab_quality')
    fresh = ab_quality.classify(signal)
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
    signal=dict(source,_strat='A/B',dir=order['direction'],entry=order['entry_price'],SL=order['stop_price'],TP=order['target_price'],_ab_quality=order['q1_quality'])
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
    if service is None or not enabled(service.config):return dict(state='DISABLED',executable=False)
    try:
        order=candidate(signal,candidate_id,service.config)
        result=service.queue_setup(order)
        return dict(result,order_id=order['order_id'],executable=False)
    except (KeyError,ValueError,TypeError,OverflowError):
        return dict(state='INELIGIBLE',reason='q1_frozen_evidence_invalid',executable=False)


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
