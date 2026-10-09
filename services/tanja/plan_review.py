"""Offline directional-entry plan review. Does not select discretionary anchors or send orders.
Caller must explicitly supply causal context, entry mode and same-series levels.
An internally consistent plan remains hypothetical and not broker-authorized.
"""
from decimal import Decimal
import math

MODES={'immediate_after_confirmation','retracement'}

def review_plan(plan, *, now):
    missing=[];errors=[]
    def need(name):
        if plan.get(name) is None:missing.append(name);return None
        return plan[name]
    for k in ('plan_id','symbol','direction','selected_at','context_valid_until','context_supported',
              'trigger_available_at','entry_mode','entry_reference','invalidation','stop_buffer_ticks',
              'target','initial_quantity','risk_budget_usd'):
        need(k)
    if plan.get('direction') not in (None,'long','short'):errors.append('INVALID_DIRECTION')
    direction=plan.get('direction');sign=-1 if direction=='short' else 1
    if plan.get('symbol') not in (None,'MNQ'):errors.append('ONLY_MNQ_MANAGEMENT_REVIEW')
    for k in ('selected_at','trigger_available_at'):
        v=plan.get(k)
        if v is not None and (type(v) is not int or v>now):errors.append('FUTURE_OR_INVALID_'+k.upper())
    expiry=plan.get('context_valid_until')
    if expiry is not None and (type(expiry) is not int or expiry<=now):errors.append('CONTEXT_EXPIRED_OR_INVALID')
    if plan.get('context_supported') is not None and plan['context_supported'] is not True:errors.append('CONTEXT_NOT_SUPPORTED')
    if plan.get('entry_mode') not in MODES|{None}:errors.append('UNKNOWN_ENTRY_MODE')
    if plan.get('entry_mode')=='retracement':
        until=need('entry_valid_until');cancel=need('cancel_condition')
        if until is not None and (type(until) is not int or until<=now):errors.append('ENTRY_EXPIRED_OR_INVALID')
        if cancel is not None and (not isinstance(cancel,str) or not cancel.strip()):errors.append('EMPTY_CANCEL_CONDITION')
    symbol=plan.get('symbol')
    def point(x,label):
        if x is None:return None
        if not isinstance(x,dict):errors.append('INVALID_'+label);return None
        valid=True
        if x.get('symbol')!=symbol:errors.append('CROSS_SERIES_'+label);valid=False
        at=x.get('available_at')
        if type(at) is not int or at>now:errors.append('FUTURE_OR_UNDATED_'+label);valid=False
        if not isinstance(x.get('source'),str) or not x['source'].strip():errors.append('UNSOURCED_'+label);valid=False
        price=x.get('price')
        if type(price) not in (int,float) or not math.isfinite(price) or price<=0:
            errors.append('INVALID_PRICE_'+label);return None
        if Decimal(str(price))%Decimal('0.25'):errors.append('OFF_TICK_'+label);valid=False
        return price if valid else None
    entry=point(plan.get('entry_reference'),'ENTRY_REFERENCE')
    invalidation=point(plan.get('invalidation'),'INVALIDATION')
    targetdef=plan.get('target');target=None
    if targetdef is not None:
        if not isinstance(targetdef,dict):errors.append('INVALID_TARGET')
        elif targetdef.get('kind')=='level':target=point(targetdef.get('anchor'),'TARGET')
        elif targetdef.get('kind')=='extension_minus_one':
            a=point(targetdef.get('anchor'),'TARGET_ANCHOR');b=point(targetdef.get('extreme'),'TARGET_EXTREME')
            if a is None or b is None:errors.append('MISSING_PROJECTION_ANCHOR')
            elif sign*(a-b)<=0:errors.append('INVALID_FALSE_MOVE_ANCHORS')
            else:target=2*a-b
        else:errors.append('TARGET_SELECTION_NOT_DEFINED')
        if isinstance(targetdef,dict) and targetdef.get('anchor') is None:errors.append('MISSING_TARGET_ANCHOR')
    ticks=plan.get('stop_buffer_ticks');stop=None
    if ticks is not None:
        if type(ticks) is not int or ticks<0:errors.append('INVALID_STOP_BUFFER')
        elif invalidation is not None:stop=invalidation-sign*ticks*.25
    if entry is not None and stop is not None and (stop<=0 or sign*(entry-stop)<=0):errors.append('INVALID_DIRECTIONAL_STOP')
    if entry is not None and target is not None and sign*(target-entry)<=0:errors.append('INVALID_DIRECTIONAL_TARGET')
    q=plan.get('initial_quantity');budget=plan.get('risk_budget_usd');risk=None
    if q is not None and (type(q) is not int or q<=0):errors.append('INVALID_QUANTITY')
    if budget is not None and (type(budget) not in (int,float) or not math.isfinite(budget) or budget<=0):errors.append('INVALID_RISK_BUDGET')
    if not errors and q is not None and stop is not None and entry is not None:
        risk=sign*(entry-stop)*q*2
        if budget is not None and risk>budget:errors.append('PLANNED_PRICE_RISK_EXCEEDS_SUPPLIED_BUDGET')
    return dict(state='NEEDS_PLAN' if missing else 'BLOCKED' if errors else 'HYPOTHETICAL_PLAN_COMPLETE',
        missing=sorted(set(missing)),errors=sorted(set(errors)),stop=stop,target=target,
        planned_price_risk_before_costs=risk,executable=False,
        broker_risk_approved=False,semantic_fidelity_verified=False,
        note='Entry reference is not a fill. Account equity, commissions, slippage, shared-account exposure and broker state are not supplied.')
