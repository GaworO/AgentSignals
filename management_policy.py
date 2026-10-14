"""Settled core management semantics. Offline proposals, never orders/fills.
The caller must supply actual position/quote state. No broker state is inferred.
"""
import hashlib
import math

VERSION='tanja-management-core-v2'

def contract():
    return dict(version=VERSION,local_partial=dict(fraction_of_original=.2,rounding='floor',requires_reached_objective=True,news_alone_triggers=False,skip_below_one=True,preserve_runner=True),
        remainder=['latest_confirmed_M1_swing_stop','selected_final_target'],
        stop_convention=dict(left_bars=1,right_closed_bars=1,buffer_ticks=1,verified_tanja_convention=False),
        news_stop=dict(seconds_before=60,adverse_entry_offset_points=1.5,never_loosen=True),
        extra_es_atr_partial=dict(requested=True,status='RESEARCH_PROPOSAL_IMPLEMENTED',enabled=True,fraction_of_original=.2,rounding='floor',max_occurrences=1,es_objective='new_current_NY_RTH_extreme',mirror_short=True,atr_enabled=False,detection='fully_closed_post_entry_ES_M1',preserve_runner=True),
        additions=dict(requested=True,status='RESEARCH_PROPOSAL_IMPLEMENTED',enabled=True,fraction_of_original=.2,rounding='floor',maximum=1,trigger='fresh_same_direction_gated_M5_setup_after_entry',shares_original_risk_budget=True),
        separate_wick_rejection_exit=False,broker_orders_enabled=False)

def local_partial_proposal(*,position_id,direction,original_quantity,remaining_quantity,entry,opened_at,
                           objective,final_target,now,market_price,price_known_at,
                           first_partial_done,partial_pending):
    """Current quote touching a causally selected objective; no past-high inference.
    An explicit filled/pending flag prevents repeated proposals in the caller's
    lifecycle. The stable intent ID lets a future reconciler deduplicate retries.
    """
    def no(reason):return dict(action='NONE',reason=reason,is_order=False,policy_version=VERSION)
    if not isinstance(position_id,str) or not position_id.strip() or len(position_id)>200:return no('POSITION_ID_REQUIRED')
    if direction not in ('long','short'):return no('INVALID_DIRECTION')
    if type(first_partial_done) is not bool or type(partial_pending) is not bool:return no('PARTIAL_STATUS_REQUIRED')
    if first_partial_done or partial_pending:return no('PARTIAL_ALREADY_DONE_OR_PENDING')
    if any(type(v) is not int or v<0 for v in (now,opened_at,price_known_at)) or opened_at>now:return no('INVALID_CLOCK')
    if price_known_at<opened_at or not 0<=now-price_known_at<=2:return no('NEEDS_FRESH_QUOTE')
    if any(type(v) is not int or v<1 for v in (original_quantity,remaining_quantity)) or remaining_quantity>original_quantity+original_quantity//5:return no('INVALID_POSITION_QUANTITY')
    q=original_quantity//5
    if q<1:return no('PARTIAL_LESS_THAN_ONE_CONTRACT')
    if q>=remaining_quantity:return no('PARTIAL_MUST_PRESERVE_RUNNER')
    if not isinstance(objective,dict) or not isinstance(objective.get('source'),str) or not objective['source'].strip():return no('SELECTED_LOCAL_OBJECTIVE_REQUIRED')
    known=objective.get('available_at')
    if type(known) is not int or not 0<=known<=price_known_at:return no('OBJECTIVE_NOT_KNOWN_AT_QUOTE')
    prices=(entry,market_price,objective.get('price'),final_target)
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 or v*4!=round(v*4) for v in prices):return no('INVALID_PRICE')
    sign=1 if direction=='long' else -1
    if sign*(final_target-entry)<=0 or sign*(objective['price']-entry)<=0 or sign*(final_target-objective['price'])<0:return no('INVALID_OBJECTIVE_GEOMETRY')
    if sign*(market_price-final_target)>=0:return no('FINAL_TARGET_TAKES_PRIORITY')
    if sign*(market_price-objective['price'])<0:return no('LOCAL_OBJECTIVE_NOT_REACHED')
    identity=f'{VERSION}:{position_id}:{opened_at}:first-local-partial'
    return dict(action='REVIEW_PARTIAL_PROPOSAL',intent_id=hashlib.sha256(identity.encode()).hexdigest(),quantity=q,
                remaining_after_fill=remaining_quantity-q,objective=dict(objective),observed_at=now,
                is_order=False,requires_confirmed_fill_and_bracket_resize=True,policy_version=VERSION)


def _partial_quantity(original,remaining,done,pending):
    if type(done) is not bool or type(pending) is not bool:return None,'PARTIAL_STATUS_REQUIRED'
    if done or pending:return None,'PARTIAL_ALREADY_DONE_OR_PENDING'
    if any(type(v) is not int or v<1 for v in (original,remaining)) or remaining>original+original//5:return None,'INVALID_POSITION_QUANTITY'
    q=original//5
    if q<1:return None,'PARTIAL_LESS_THAN_ONE_CONTRACT'
    if q>=remaining:return None,'PARTIAL_MUST_PRESERVE_RUNNER'
    return q,None


def es_session_partial_proposal(*,position_id,direction,original_quantity,remaining_quantity,opened_at,
                                es_bars,now,first_es_partial_done,partial_pending):
    """One extra 20% reduction on a new ES RTH extreme, observed on closed M1.
    Only complete post-entry candles can trigger, so a pre-entry intrabar high
    cannot be retroactively treated as a fresh event. This introduces bar-close
    latency and is not an intrabar fill simulation.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    def no(reason):return dict(action='NONE',reason=reason,is_order=False,policy_version=VERSION)
    if not isinstance(position_id,str) or not position_id.strip() or len(position_id)>200:return no('POSITION_ID_REQUIRED')
    if direction not in ('long','short'):return no('INVALID_DIRECTION')
    if direction=='short' and not contract()['extra_es_atr_partial'].get('mirror_short',False):return no('SHORT_ES_RULE_NOT_SELECTED')
    if any(type(v) is not int or v<0 for v in (opened_at,now)) or opened_at>now:return no('INVALID_CLOCK')
    q,error=_partial_quantity(original_quantity,remaining_quantity,first_es_partial_done,partial_pending)
    if error:return no(error)
    day=datetime.fromtimestamp(now,ZoneInfo('America/New_York'))
    start=int(day.replace(hour=9,minute=30,second=0,microsecond=0).timestamp())
    end=int(day.replace(hour=16,minute=0,second=0,microsecond=0).timestamp())
    if day.weekday()>4 or not start<now<end:return no('OUTSIDE_NEW_YORK_RTH')
    closed=[]
    for b in es_bars:
        t=b.get('time')
        if type(t) is not int or t<0 or t%60:return no('INVALID_ES_M1_CLOCK')
        if t<start or t+60>now:continue
        received=b.get('received_at',t+60)
        if type(received) not in (int,float) or not math.isfinite(received) or received<t+60:return no('INVALID_ES_RECEIPT')
        if received>now:continue
        vals=[b.get(k) for k in ('open','high','low','close')]
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 or v*4!=round(v*4) for v in vals):return no('INVALID_ES_PRICE')
        if not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:return no('INVALID_ES_OHLC')
        closed.append(b)
    if len(closed)<2 or [b['time'] for b in closed]!=list(range(start,closed[-1]['time']+1,60)):return no('COMPLETE_ES_SESSION_HISTORY_REQUIRED')
    latest=closed[-1]
    if latest['time']<opened_at:return no('ES_EVENT_NOT_FULLY_AFTER_ENTRY')
    if not 0<=now-(latest['time']+60)<=15:return no('ES_EVENT_STALE')
    side='high' if direction=='long' else 'low';sign=1 if direction=='long' else -1
    previous=max(sign*b[side] for b in closed[:-1])*sign
    if sign*(latest[side]-previous)<=0:return no('NO_NEW_ES_SESSION_EXTREME')
    identity=f'{VERSION}:{position_id}:{opened_at}:first-es-partial'
    return dict(action='REVIEW_PARTIAL_PROPOSAL',intent_id=hashlib.sha256(identity.encode()).hexdigest(),quantity=q,
                remaining_after_fill=remaining_quantity-q,trigger='ES_NEW_SESSION_HIGH' if direction=='long' else 'ES_NEW_SESSION_LOW',
                previous_extreme=previous,observed_extreme=latest[side],bar_time=latest['time'],available_at=latest['time']+60,
                observed_at=now,is_order=False,requires_confirmed_fill_and_bracket_resize=True,policy_version=VERSION)


def add_on_proposal(*,position_id,direction,original_quantity,remaining_quantity,opened_at,current_stop,
                    original_risk_budget_usd,committed_risk_usd,decision,now,additions_done,add_pending):
    """One 20%-of-base add from a NEW gated M5 plan, sharing existing risk.
    Supplied committed risk includes open/pending account exposure, never netted
    against unrealized profits. No additional fresh 0.5% budget is granted.
    """
    from risk_sizing import contracts_for_stop
    def no(reason):return dict(action='NONE',reason=reason,is_order=False,policy_version=VERSION)
    if not isinstance(position_id,str) or not position_id.strip() or len(position_id)>200:return no('POSITION_ID_REQUIRED')
    if direction not in ('long','short'):return no('INVALID_DIRECTION')
    if type(additions_done) is not int or additions_done<0 or type(add_pending) is not bool:return no('ADD_STATUS_REQUIRED')
    if additions_done>=1 or add_pending:return no('ADD_ALREADY_DONE_OR_PENDING')
    if any(type(v) is not int or v<1 for v in (original_quantity,remaining_quantity)) or remaining_quantity>original_quantity:return no('INVALID_POSITION_QUANTITY')
    q=original_quantity//5
    if q<1:return no('ADD_LESS_THAN_ONE_CONTRACT')
    if any(type(v) is not int or v<0 for v in (opened_at,now)) or opened_at>now:return no('INVALID_CLOCK')
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (original_risk_budget_usd,committed_risk_usd)):return no('INVALID_SHARED_RISK')
    try:
        if decision['state']!='PLAN_READY' or decision['orders_enabled'] is not False:return no('GATED_RESEARCH_PLAN_REQUIRED')
        if decision['ai_context']['state']!='PERMITTED' or decision['scheduled_news']['state']!='CLEAR' or decision.get('news_entry_reasons'):return no('CURRENT_ENTRY_GATES_REQUIRED')
        g=decision['account_guard'];p=decision['compiled']['plan'];s=decision['selected']['selection']
        if g['state']!='REPORTED_LIMITS_CLEAR':return no('ACCOUNT_CHECKS_REQUIRED')
        if s['confirmation_policy']!='closed_m5_v1' or s['timeframe_minutes']!=5 or s['swing_break_mode']!='close':return no('M5_CLOSE_REQUIRED')
        if p['direction']!=direction or s['direction']!=direction or decision['ai_context']['direction']!=direction:return no('ADD_DIRECTION_CONFLICT')
        trigger=p['trigger_available_at'];freeze=decision['frozen_at'];expiry=p['context_valid_until']
        if any(type(v) is not int for v in (trigger,freeze,expiry)):return no('INVALID_DECISION_CLOCK')
        if not opened_at<trigger<=freeze<=now<expiry or now-trigger>15:return no('FRESH_POST_ENTRY_SETUP_REQUIRED')
        if p['plan_id']==position_id:return no('ORIGINAL_ENTRY_CANNOT_TRIGGER_ADD')
        entry=p['entry_reference']['price']
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 or v*4!=round(v*4) for v in (entry,current_stop)):return no('INVALID_PRICE')
        sign=1 if direction=='long' else -1
        if sign*(entry-current_stop)<=0:return no('CURRENT_STOP_INVALID_FOR_ADD')
        committed=max(committed_risk_usd,g['reported_values']['committed_risk_usd'])
        budget=min(original_risk_budget_usd-committed,g['budget_usd'])
        if budget<=0:return no('NO_SHARED_RISK_AVAILABLE')
        allowed=contracts_for_stop(entry,current_stop,direction,budget,g['max_contracts'])
        q=min(q,allowed)
        if q<1:return no('SHARED_RISK_CANNOT_FUND_ONE_CONTRACT')
    except (KeyError,TypeError,ValueError,IndexError):return no('INCOMPLETE_GATED_PLAN')
    identity=f'{VERSION}:{position_id}:{opened_at}:only-add'
    return dict(action='REVIEW_ADD_PROPOSAL',intent_id=hashlib.sha256(identity.encode()).hexdigest(),quantity=q,
                trigger_plan_id=p['plan_id'],reference_entry=entry,shared_stop=current_stop,shared_budget_available=budget,
                planned_add_risk=q*abs(entry-current_stop)*2,observed_at=now,is_order=False,
                requires_confirmed_fill_and_bracket_resize=True,policy_version=VERSION)
