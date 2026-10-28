"""Original Q1 -> exact AI -> shared native LIMIT/OSO execution.

Opt-in routing only. No environment/policy mutation, history replay, recovery,
partials or manager writes. Uses the existing dispatch and native journals.
"""
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone

import ab_ai_context
import ab_q1_review as review
import ab_v3_live as live
import builder_crosstrade as broker
import continuation_live as dispatches
import execution_policy
import execution_safety
import execution_trace
import execution_validation
import guardrails

MODE = 'DIRECTIONAL_Q1_ONLY'
LIFECYCLE = 'AB_Q1_NATIVE_FIXED_BRACKET_V1'


def is_order(order):
    try:
        review.describe(order, order['contract'])
        return True
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def permission(order, *, clock=time.time):
    reason = execution_safety.entry_blocker()
    if reason:return reason
    if execution_policy.mode() != MODE:return 'q1_shared_route_disabled'
    if os.environ.get('AB_Q1_ENTRY_ARMED', '0') != '1':return 'q1_entry_not_armed'
    if not review.enabled(os.environ):return 'q1_ai_disabled'
    if not is_order(order):return 'q1_exact_original_setup_required'
    if not broker.selected():return 'q1_native_provider_required'
    if len(os.environ.get('GUARD_TOKEN', '')) < 32:return 'q1_guard_token_required'
    if (order['contract'] != live.contract() or order['contract'] != os.environ.get('EXEC_TICKER')
            or os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED') != '1'
            or os.environ.get('AB_V3_TV_MAPPING_VERIFIED') != '1'):return 'q1_contract_unverified'
    if os.environ.get('EXEC_FX', '0') == '1':return 'q1_futures_only'
    if (os.environ.get('AB_MANAGER_MODE', 'shadow') != 'shadow'
            or os.environ.get('V3_EXIT_MODE', 'shadow') != 'shadow'
            or os.environ.get('V3_EXIT_REAL_EXECUTION', 'false') != 'false'
            or os.environ.get('V3_RUNNER_ENABLED', 'false') != 'false'):
        return 'q1_fixed_bracket_only'
    try:
        if (float(os.environ.get('PRICE_OFFSET','0')) != 0 or float(os.environ.get('POINT_VALUE','2')) != 2
                or float(os.environ.get('EXEC_TICK','.25')) != .25):return 'q1_geometry_config_mismatch'
        now=int(clock()*1000)
        if not order['activation_ms'] <= now < order['expiry_ms']:return 'q1_setup_expired'
        if (not review.session_at(now/1000,os.environ)
                or not review.session_at(order['activation_ms']/1000,os.environ)):return 'q1_outside_session'
    except (ValueError, TypeError, OverflowError):return 'q1_invalid_config'
    return None


def signal(order):
    if not is_order(order):raise ValueError('q1_exact_original_setup_required')
    when=datetime.fromtimestamp(order['activation_ms']/1000,review.NY)
    session=review.session_at(order['activation_ms']/1000,os.environ)
    source=order['q1_source']
    return dict(source, _strat='A/B', model=source.get('model'), dir=order['direction'],
        entry=order['entry_price'],SL=order['stop_price'],TP=order['target_price'],
        date=when.date().isoformat(),bos=when.strftime('%H:%M'),bos_ms=source['bos_ms'],
        sess=session['name'] if session else 'UNKNOWN',_ab_quality=order['q1_quality'],
        _q1_order=order,_continuation_order_id=order['order_id'],
        _disable_partial=True,_strict_risk_budget=True,_v3_directional=False)


def observe(raw, candidate_id, service, *, clock=time.time):
    """Only newly emitted scanner setups can enter this ledger; never replay AI history."""
    if execution_policy.mode()!=MODE or os.environ.get('AB_Q1_ENTRY_ARMED','0')!='1':return
    try:
        order=review.candidate(raw,candidate_id,service.config)
        reason=permission(order,clock=clock)
        if reason:return
        with live.connect() as db:
            bos=db.execute("SELECT bar_json FROM bars WHERE tf='M1' AND contract=? AND ts_ms=?",
                           (order['contract'],order['q1_source']['bos_ms'])).fetchone()
        if not bos:
            execution_trace.record(order['order_id'],'q1_forward_setup_blocked',reason='q1_original_bos_missing')
            return
        order['q1_bos_bar']=json.loads(bos[0])
        now=datetime.fromtimestamp(clock(),timezone.utc).isoformat()
        dispatches._init_db()
        if not dispatches._claim(order,now):return
        dispatches._finish(order['order_id'],dict(state='WAITING_AI',reason='q1_exact_review_pending'),now)
        execution_trace.record(order['order_id'],'q1_forward_setup_registered')
    except (KeyError,ValueError,TypeError,OverflowError):return


def resume(dispatcher, service, *, clock=time.time):
    dispatches._init_db()
    with dispatches._connect() as db:
        rows=db.execute("SELECT source_json FROM live_dispatches WHERE strategy='A/B' AND state='WAITING_AI' ORDER BY activation_ms,order_id").fetchall()
    for row in rows:
        order=json.loads(row[0]);why,approved=review.check(order,service)
        reason=permission(order,clock=clock)
        if not reason and approved and approved.get('state')=='PENDING':continue
        now=datetime.fromtimestamp(clock(),timezone.utc).isoformat()
        with dispatches._connect() as db:
            claimed=db.execute("UPDATE live_dispatches SET state='DISPATCHING',updated_at=? WHERE order_id=? AND state='WAITING_AI'",(now,order['order_id'])).rowcount
        if not claimed:continue
        if reason or why:result=dict(state='BLOCKED',reason=reason or why)
        else:
            try:result=dispatcher(order)
            except Exception:result=dict(state='SUBMISSION_UNKNOWN',reason='q1_dispatch_exception_no_retry')
        dispatches._finish(order['order_id'],result,now)
        execution_trace.record(order['order_id'],'q1_dispatch_decision',state=result['state'],reason=result.get('reason'))


def thesis(order, clock, *, pending=False):
    proof=dict(order_id=order['order_id'],valid=False,checked_ms=int(clock()*1000))
    try:
        if not is_order(order):return dict(proof,reason='q1_original_setup_invalid')
        with dispatches._connect() as db:
            row=db.execute('SELECT source_json,state FROM live_dispatches WHERE order_id=?',(order['order_id'],)).fetchone()
        if not row or row['state'] not in (('SENT','SUBMISSION_UNKNOWN','DISPATCHING') if pending else ('DISPATCHING',)):
            return dict(proof,reason='q1_owned_dispatch_missing')
        source=json.loads(row['source_json'])
        if review.describe(source,order['contract']) != review.describe(order,order['contract']):
            return dict(proof,reason='q1_original_geometry_changed')
        now=proof['checked_ms'];first=order['activation_ms'];bos=source['q1_source']['bos_ms']
        if not first<=now<order['expiry_ms']:return dict(proof,reason='q1_setup_expired')
        with live.connect() as db:
            rows=db.execute("SELECT ts_ms,bar_json FROM bars WHERE tf='M1' AND contract=? AND ts_ms>=? AND ts_ms+60000<=? ORDER BY ts_ms",(order['contract'],bos,now)).fetchall()
        bars={r['ts_ms']:json.loads(r['bar_json']) for r in rows}
        if bars.get(bos)!=source.get('q1_bos_bar') or not bars.get(bos):
            return dict(proof,reason='q1_bos_reference_missing_or_changed')
        if any(t not in bars for t in range(first,now//60000*60000,60000)):
            return dict(proof,reason='q1_post_setup_candle_gap')
        for at,b in bars.items():
            if at<first:continue
            touched=(b['low']<=order['stop_price'] or b['high']>=order['target_price']) if order['direction']=='LONG' else (b['high']>=order['stop_price'] or b['low']<=order['target_price'])
            if touched:return dict(proof,reason='q1_original_bracket_touched')
        service=ab_ai_context.INSTANCE
        if service is None:return dict(proof,reason='q1_review_unavailable')
        packet=service.packet(service.reader(),clock());end=int(clock())//60*60
        if packet['as_of']!=end:return dict(proof,reason='q1_context_not_current')
        if getattr(service,'trigger',None)=='HYBRID':ab_ai_context.hybrid_lifecycle.refresh(service,packet)
        why,approved=review.check(order,service)
        if why:return dict(proof,reason=why)
        for symbol in ('NQ','MNQ'):
            times={e['available_at'] for e in packet['evidence'].values() if e['kind']=='bar' and e['timeframe']==1 and e['symbol']==symbol}
            if any(t not in times for t in range(int(approved['cutoff'])+60,end+1,60)):
                return dict(proof,reason='q1_post_review_context_gap')
        service.invalidate(packet)
        why,latest=review.check(order,service)
        if why or latest['review_id']!=approved['review_id']:return dict(proof,reason=why or 'q1_review_changed')
        if int(clock()*1000)//60000!=now//60000 or clock()*1000>=order['expiry_ms']:
            return dict(proof,reason='q1_expired_during_validation')
        evidence=json.dumps([source,bars,packet['packet_id'],latest['review_id']],sort_keys=True)
        return dict(proof,valid=True,evidence_id=hashlib.sha256(evidence.encode()).hexdigest())
    except Exception:return dict(proof,reason='q1_thesis_evidence_unavailable')


def dispatch(order, *, budget_builder, feed_age, market_open, news_flags, calendar_age):
    """One shared-account reservation and native submit; no blanket close or retry."""
    import directional_risk_revalidation as sizing
    reason=permission(order)
    if reason:return dict(state='BLOCKED',reason=reason)
    why,approved=review.check(order,ab_ai_context.INSTANCE)
    if why:return dict(state='BLOCKED',reason=why)
    try:p=execution_validation.policy_for(order)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(state='BLOCKED',reason='q1_validated_execution_policy_unavailable')
    why=live.native_bracket_readiness(p)
    if why:return dict(state='BLOCKED',reason=why)
    profile=guardrails.account_profile();budget=budget_builder(profile)
    if not profile.get('config_ok') or not math.isfinite(budget) or budget<=0:
        return dict(state='BLOCKED',reason='q1_account_budget_unknown')
    x=signal(order);x.update(_risk_budget_usd=budget,_planned_group_risk_usd=budget)
    capacity=sizing.size_for_entry(x)
    if not capacity['allowed']:return dict(state='BLOCKED',reason=capacity['reason'])
    qty=min(capacity['max_quantity'],int(os.environ['EXEC_MAX_QTY']),p['max_quantity'])
    if qty<1:return dict(state='BLOCKED',reason='q1_risk_below_one_contract')
    if guardrails.exec_mode()!='auto' or os.environ.get('AUTO_SUBMIT')!='1':
        return dict(state='BLOCKED',reason='automatic_entries_disabled')
    _,news_hard=news_flags(x)
    allowed,why=guardrails.guard_ok(x,feed_age_min=feed_age(),market_open=market_open(),news_hard=news_hard,cal_age_h=calendar_age())
    if not allowed:return dict(state='BLOCKED',reason=why)
    native=dict(order,quantity=qty,contract=live.contract(),route_id=live.route(),
        account_label=os.environ.get('ACCOUNT_LABEL','account'),lifecycle=LIFECYCLE,
        baseline_target_price=order['target_price'],runner_enabled=False,risk_points=abs(order['entry_price']-order['stop_price']))
    with live.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM orders WHERE order_id=?',(order['order_id'],)).fetchone():
            return dict(state='BLOCKED',reason='q1_already_prepared_no_retry')
        if db.execute("SELECT 1 FROM native_orders WHERE state NOT IN ('NO_FILL','CLOSED')").fetchone():
            return dict(state='BLOCKED',reason='q1_account_owned')
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(order['order_id'],'PREPARED',json.dumps(native),None,live.now_ms()))
    gid='ab_q1_'+order['order_id']
    result=None
    try:
        if not guardrails.begin_sibling_batch(gid,budget,['A/B']):
            result=dict(sent=False,status=422,reason='q1_batch_reservation_failed')
        else:
            execution_trace.record(order['order_id'],'q1_entry_sizing',entry=native['entry_price'],structural_sl=native['stop_price'],target=native['target_price'],quantity=qty,capacity=capacity)
            payload=dict(ticker=native['contract'],action='buy' if native['direction']=='LONG' else 'sell',orderType='limit',
                limitPrice=native['entry_price'],quantity=qty,stopLoss=dict(stopPrice=native['stop_price']),
                takeProfit=dict(limitPrice=native['target_price']),timeInForce='gtd',
                cancelAfter=max(0,(native['expiry_ms']-live.now_ms())//1000))
            result=broker.submit(payload,order['order_id'])
    except broker.TransportError as exc:
        result=dict(sent=False,status=422,reason=str(exc))
    except Exception:
        result=dict(sent=False,submission_unknown=True,reason='q1_submission_unknown_no_retry')
    unknown=bool(result.get('submission_unknown') or result.get('accepted_any'))
    state='SENT' if result.get('sent') else 'SUBMISSION_UNKNOWN' if unknown else 'BLOCKED'
    local='WAITING_BROKER_FILL' if result.get('sent') else 'SUBMISSION_UNKNOWN' if unknown else 'REJECTED'
    with live.connect() as db:
        db.execute("UPDATE orders SET state=?,updated_ms=? WHERE order_id=? AND state='PREPARED'",(local,live.now_ms(),order['order_id']))
    if result.get('sent') or unknown:guardrails.touch_sibling_batch(gid,'A/B',result.get('status'))
    if not result.get('sent'):guardrails.finish_sibling_batch(gid,'native_reconciliation_pending' if unknown else 'failed_before_accept')
    guardrails.note(x,'sent' if result.get('sent') else 'blocked',result.get('reason'))
    return dict(state=state,reason=result.get('reason','ok'),quantity=qty,route_id=native['route_id'],account_label=native['account_label'],broker=result)


def status():
    blockers=[]
    if execution_policy.mode()!=MODE:blockers.append('q1_shared_route_disabled')
    if os.environ.get('AB_Q1_ENTRY_ARMED','0')!='1':blockers.append('q1_entry_not_armed')
    if not review.enabled(os.environ):blockers.append('q1_ai_disabled')
    try:
        p=execution_validation.policy('A/B')
        reason=live.native_bracket_readiness(p)
        if reason:blockers.append(reason)
    except (OSError,ValueError,TypeError,KeyError):blockers.append('q1_validated_execution_policy_unavailable')
    return dict(implemented=True,mode=execution_policy.mode(),entry_armed=os.environ.get('AB_Q1_ENTRY_ARMED','0')=='1',
        static_ready=not blockers,blockers=blockers,nofill_recovery_allowed=False,
        note='Every order still requires fresh exact AI, account, market and broker checks; one account position at a time.')
