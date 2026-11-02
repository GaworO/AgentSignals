"""Independent ICT proposals on the existing protected native order transport.

Default off. Never grants model execution authority or mutates permissions.
Exact saved evidence, an operator-approved policy and a forward activation epoch
are mandatory. Unknown submission outcomes are reconciled, never resubmitted.
"""
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

VERSION='ict_bracket_opposing_structure_v1'
IFVG_VERSION='ict_ifvg_bracket_reinversion_v1'
GEOMETRY=('order_id','strategy','direction','contract','entry_price','stop_price','target_price','activation_ms','expiry_ms','ict_review_id','ict_fingerprint','ict_packet_id','ict_requested_at','ict_liquidity_target','ict_family','ict_family_contract')
_CALLBACKS=None


def configure(**callbacks):
    global _CALLBACKS
    _CALLBACKS=callbacks


def equity_budget(snapshot):
    equity=snapshot['account']['balance']['netLiq']
    if type(equity) not in (int,float) or not math.isfinite(equity) or equity<=0:
        raise ValueError('ict_current_equity_unknown')
    return float((Decimal(str(equity))*Decimal('0.005')).quantize(Decimal('.01'),rounding=ROUND_FLOOR))


def from_record(record, now, family="ICT2022"):
    from ict_order_handoff import proposal
    p=proposal(record,os.environ.get('AB_V3_CONTRACT'),now,family=family)
    decision=record['decision'] if family=='ICT2022' else record['decision']['ifvg_review']
    version=IFVG_VERSION if family=='IFVG' else VERSION
    key='ifvg_execution_contract' if family=='IFVG' else 'execution_contract'
    if (record['packet'].get(key)!=version
            or decision['plan'].get('invalidation_mode')!=version):
        raise ValueError('ict_machine_invalidation_not_accepted')
    return dict(order_id=p['order_id'],candidate_id=p['review_id'],strategy='ICT_AI',direction=p['direction'],
        contract=p['contract'],order_type='LIMIT',entry_price=p['entry'],stop_price=p['stop'],target_price=p['target'],
        activation_ms=int(p['activation_at']*1000),expiry_ms=int(p['expires_at']*1000),
        ict_review_id=p['review_id'],ict_fingerprint=p['fingerprint'],ict_packet_id=p['packet_id'],
        ict_requested_at=record['requested_at'],ict_liquidity_target=p['ai_liquidity_target'],
        ict_thesis_version=VERSION,runner_enabled=False,
        **(dict(ict_family='IFVG',ict_family_contract=IFVG_VERSION) if family=='IFVG' else {}))


def is_order(order):
    try:
        return (order.get('strategy')=='ICT_AI' and order.get('ict_thesis_version')==VERSION
            and order.get('order_id')=='ICT-'+order['ict_fingerprint'][:40]
            and bool(re.fullmatch(r'[a-f0-9]{64}',order['ict_fingerprint']))
            and bool(re.fullmatch(r'ICT_INDEPENDENT_NYAM_\d{8}_\d+',order['ict_review_id']))
            and order.get('order_type')=='LIMIT' and order.get('runner_enabled') is False
            and (order.get('ict_family') is None or (order.get('ict_family')=='IFVG' and order.get('ict_family_contract')==IFVG_VERSION)))
    except (KeyError,TypeError):return False


def record_for(order):
    if not is_order(order):raise ValueError('ict_exact_order_required')
    rid=order['ict_review_id'];day=rid.split('_')[-2]
    return json.loads((Path(os.environ['DATA_DIR'])/'research'/('ict_2022_nyam_'+day)/(rid+'.json')).read_text())


def review_check(order, *, clock=time.time):
    try:
        now=clock();record=record_for(order);original=from_record(record,now,order.get("ict_family") or "ICT2022")
        if any(original.get(k)!=order.get(k) for k in GEOMETRY):return 'ict_saved_review_geometry_changed',None
        return None,dict(state='ALLOW',decision_origin='VALIDATED_SAVED_ICT_PROPOSAL',review_id=record['id'],
            order_id=order['order_id'],available_at=record['received_at'],expires_at=original['expiry_ms']/1000,
            setup={k:original.get(k) for k in GEOMETRY},packet_id=record['packet']['packet_id'])
    except Exception:return 'ict_saved_review_missing_changed_or_expired',None


def permission(order, *, clock=time.time):
    import execution_safety, execution_policy, builder_crosstrade, ab_v3_live
    from ict_shadow_cloud import session
    why=execution_safety.entry_blocker()
    if why:return why
    if os.environ.get('ICT_EXECUTION_ENABLED','false')!='true':return 'ict_execution_disabled'
    if execution_policy.mode() not in ('V3_ONLY','DIRECTIONAL_Q1_ONLY'):return 'ict_shared_route_disabled'
    if not is_order(order):return 'ict_exact_order_required'
    if order.get('ict_family')=='IFVG':
        why=ifvg_permission(order,clock())
        if why:return why
    try:
        now=clock();after=int(os.environ.get('ICT_EXECUTION_ARMED_AFTER_MS','0'))
        if after<=0 or after>now*1000 or order['ict_requested_at']*1000<=after:
            return 'ict_forward_activation_epoch_required'
        if not order['activation_ms']<=now*1000<order['expiry_ms']:return 'ict_setup_expired'
        if not session(now) or not session(order['activation_ms']/1000):return 'ict_outside_session'
        if (order['contract']!=ab_v3_live.contract() or order['contract']!=os.environ.get('EXEC_TICKER')
                or os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED')!='1'
                or os.environ.get('AB_V3_TV_MAPPING_VERIFIED')!='1'):return 'ict_contract_unverified'
        if (os.environ.get('ICT_SHADOW_ENABLED')!='true' or not builder_crosstrade.selected()
                or len(os.environ.get('GUARD_TOKEN',''))<32):return 'ict_scanner_native_route_required'
        if (os.environ.get('V3_EXIT_MODE','shadow')!='shadow' or os.environ.get('AB_MANAGER_MODE','shadow')!='shadow'
                or os.environ.get('V3_EXIT_REAL_EXECUTION','false')!='false' or os.environ.get('V3_RUNNER_ENABLED','false')!='false'):
            return 'ict_fixed_broker_bracket_only'
        if (os.environ.get('EXEC_FX','0')!='0' or float(os.environ.get('PRICE_OFFSET','0'))!=0
                or float(os.environ.get('POINT_VALUE','2'))!=2 or float(os.environ.get('EXEC_TICK','.25'))!=.25):
            return 'ict_geometry_config_mismatch'
    except (ValueError,KeyError,TypeError,OverflowError):return 'ict_invalid_execution_config'
    return None



def ifvg_policy_check(p):
    scope=p.get('ifvg_execution',{})
    if (scope.get('approved') is not True or scope.get('contract_version')!=IFVG_VERSION
            or not isinstance(scope.get('operator_acceptance_reference'),str)
            or not scope['operator_acceptance_reference'].strip()):
        raise ValueError('ict_ifvg_policy_approval_required')
    return scope


def ifvg_permission(order=None, now=None):
    # Separate persistent operator scope; never inherited from ICT 2022 approval.
    try:
        import execution_validation
        scope=ifvg_policy_check(execution_validation.policy('ICT_AI'))
        after=scope.get('armed_after_ms')
        now=time.time() if now is None else now
        if type(after) is not int or after<=0 or after>now*1000 or (order and order['ict_requested_at']*1000<=after):
            return 'ict_ifvg_forward_activation_epoch_required'
    except Exception:return 'ict_ifvg_policy_approval_required'
    return None


def ifvg_market_reason(record,packet):
    """Closed original-timeframe candles only; missing evidence never proves validity."""
    plan=record['decision']['ifvg_review']['plan'];original=record['packet']
    e=dict(original['evidence']);e.update(packet['evidence'])
    trio=[original['evidence'][k] for k in plan['entry_fvg_bar_ids']]
    long=plan['direction']=='LONG'
    low,high=(trio[2]['high'],trio[0]['low']) if long else (trio[0]['high'],trio[2]['low'])
    step=trio[0]['timeframe']*60;inv=original['evidence'][plan['inversion_bar_id']]
    bars={b['time']:b for b in e.values() if b.get('kind')=='bar' and b.get('symbol')=='MNQ' and b.get('timeframe')==trio[0]['timeframe'] and b.get('available_at',math.inf)<=packet['as_of']}
    for t in range(int(inv['time']+step),int(packet['as_of']-step)+1,step):
        b=bars.get(t)
        if b is None or b.get('available_at')!=t+step:return 'ict_ifvg_post_inversion_data_gap'
        if b['close']<low if long else b['close']>high:return 'ict_ifvg_reinverted_before_fill'
    return None


def signal(order):
    from ict_shadow_cloud import session,NY
    if not is_order(order):raise ValueError('ict_exact_order_required')
    when=datetime.fromtimestamp(order['activation_ms']/1000,NY);s=session(when.timestamp())
    return dict(_strat='ICT AI',model='ICT AI',dir=order['direction'],entry=order['entry_price'],
        SL=order['stop_price'],TP=order['target_price'],date=when.date().isoformat(),bos=when.strftime('%H:%M'),
        bos_ms=order['activation_ms'],sess=s[0] if s else 'UNKNOWN',_ict_order=order,
        _continuation_order_id=order['order_id'],_disable_partial=True,_strict_risk_budget=True)


def market_check(order, record, packet, now):
    from ict_order_handoff import proposal,market_reason
    import directional_hybrid_context as context
    from directional_hybrid_lifecycle import structure_event_reason
    bracket=proposal(record,order['contract'],now,family=order.get('ict_family') or 'ICT2022')
    # Include candles closing while the AI was thinking, not only after its response.
    bracket['activation_at']=record['packet']['as_of']
    why=market_reason(bracket,packet,now)
    if why:return why
    original=record['packet'];cutoff=original['as_of'];end=packet['as_of']
    # Detect revisions and gaps instead of inferring a quiet market from missing data.
    for key,e in original['evidence'].items():
        if e.get('kind')=='bar' and key in packet['evidence'] and e!=packet['evidence'][key]:return 'ict_market_data_revised'
    for symbol in ('MNQ','NQ'):
        times={e['available_at'] for e in packet['evidence'].values()
            if e.get('kind')=='bar' and e.get('symbol')==symbol and e.get('timeframe')==1}
        if any(t not in times for t in range(int(cutoff)+60,int(end)+1,60)):return 'ict_post_review_context_gap'
    if order.get('ict_family')=='IFVG':
        why=ifvg_market_reason(record,packet)
        if why:return why
    events=context.build(packet)['structure_events']['value']
    if events is None:return 'ict_structure_context_unknown'
    return structure_event_reason([e for e in events if cutoff<e['available_at']<=end],order['direction'])


def thesis(order,clock=time.time,*,pending=False):
    import continuation_live,ict_shadow_research
    proof=dict(order_id=order['order_id'],checked_ms=int(clock()*1000),valid=False)
    try:
        why,_=review_check(order,clock=clock)
        if why:return dict(proof,reason=why)
        with continuation_live._connect() as db:
            row=db.execute('SELECT state,source_json FROM live_dispatches WHERE order_id=?',(order['order_id'],)).fetchone()
        states=('SENT','SUBMISSION_UNKNOWN','DISPATCHING') if pending else ('DISPATCHING',)
        if not row or row['state'] not in states:return dict(proof,reason='ict_owned_dispatch_missing')
        saved=json.loads(row['source_json'])
        if any(saved.get(k)!=order.get(k) for k in GEOMETRY):return dict(proof,reason='ict_dispatch_changed')
        # observe_only performs no paid AI request and has no broker write.
        packet=ict_shadow_research.remote_run(0,True)['packet'];record=record_for(order)
        why=market_check(order,record,packet,clock())
        if why:return dict(proof,reason=why)
        if int(clock()*1000)//60000!=proof['checked_ms']//60000 or clock()*1000>=order['expiry_ms']:
            return dict(proof,reason='ict_expired_during_revalidation')
        return dict(proof,valid=True,evidence_id=hashlib.sha256(json.dumps([packet,order['ict_fingerprint']],sort_keys=True).encode()).hexdigest())
    except Exception:return dict(proof,reason='ict_thesis_evidence_unavailable')


def process(record, family="ICT2022"):
    """One claim per review AND per structural opportunity, durable across restarts."""
    import continuation_live as journal
    now=time.time()
    try:
        order=from_record(record,now,family)
        why=permission(order)
        if why:return dict(state='BLOCKED',reason=why)
        if _CALLBACKS is None:return dict(state='BLOCKED',reason='ict_runtime_not_connected')
        journal._init_db()
        # A second model review of the same FVG may not become a scale-in/re-entry.
        plan=(record['decision']['ifvg_review'] if family=='IFVG' else record['decision'])['plan']
        opportunity=hashlib.sha256(json.dumps([order['contract'],order['direction'],plan['entry_fvg_bar_ids']],sort_keys=True).encode()).hexdigest()
        with journal._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ict_opportunities(opportunity TEXT PRIMARY KEY,order_id TEXT UNIQUE,claimed_ms INTEGER)')
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT order_id FROM ict_opportunities WHERE opportunity=?',(opportunity,)).fetchone()
            if old:
                row=db.execute('SELECT state,result_json FROM live_dispatches WHERE order_id=?',(old[0],)).fetchone()
                return json.loads(row['result_json']) if row and row['result_json'] else dict(state='SUBMISSION_UNKNOWN',reason='ict_prior_claim_reconcile_no_retry')
            db.execute('INSERT INTO ict_opportunities VALUES(?,?,?)',(opportunity,order['order_id'],int(now*1000)))
        stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()
        if not journal._claim(order,stamp):return dict(state='SUBMISSION_UNKNOWN',reason='ict_duplicate_dispatch_no_retry')
        try:result=dispatch(order,**_CALLBACKS)
        except Exception:result=dict(state='SUBMISSION_UNKNOWN',reason='ict_dispatch_exception_no_retry')
        journal._finish(order['order_id'],result,stamp)
        return result
    except (ValueError,KeyError,TypeError,AssertionError):return dict(state='BLOCKED',reason='ict_saved_proposal_not_eligible')


def status():
    import execution_validation,ab_v3_live,execution_safety
    blockers=[]
    if os.environ.get('ICT_EXECUTION_ENABLED','false')!='true':blockers.append('ict_execution_disabled')
    if not os.environ.get('ICT_EXECUTION_ARMED_AFTER_MS'):blockers.append('ict_forward_activation_epoch_required')
    why=execution_safety.entry_blocker()
    if why:blockers.append(why)
    try:
        why=ab_v3_live.native_bracket_readiness(execution_validation.policy('ICT_AI'))
        if why:blockers.append(why)
    except Exception:blockers.append('ict_approved_execution_policy_required')
    return dict(implemented=True,submission_connected=_CALLBACKS is not None,
        enabled=os.environ.get('ICT_EXECUTION_ENABLED','false')=='true',blockers=blockers,
        pending_recovery='SHARED_NATIVE_JOURNAL',risk_percent=0.5,target='FIXED_2R_GROSS',
        ifvg=dict(implemented=True,enabled=ifvg_permission() is None,
            blockers=blockers+([why] if (why:=ifvg_permission()) else [])),
        note='Fresh exact AI, market, Guard, account and protection checks apply to every order.')


def dispatch(order, *, budget_builder, feed_age, market_open, news_flags, calendar_age):
    """One shared-account reservation and native submit; no blanket close or retry."""
    import directional_risk_revalidation as sizing
    import ab_v3_live as live, builder_crosstrade as broker, execution_validation, guardrails, execution_trace
    reason=permission(order)
    if reason:return dict(state='BLOCKED',reason=reason)
    why,approved=review_check(order)
    if why:return dict(state='BLOCKED',reason=why)
    try:p=execution_validation.policy_for(order)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(state='BLOCKED',reason='ict_validated_execution_policy_unavailable')
    why=live.native_bracket_readiness(p)
    if why:return dict(state='BLOCKED',reason=why)
    profile=guardrails.account_profile();budget=budget_builder(profile)
    if not profile.get('config_ok') or not math.isfinite(budget) or budget<=0:
        return dict(state='BLOCKED',reason='ict_account_budget_unknown')
    x=signal(order);x.update(_risk_budget_usd=budget,_planned_group_risk_usd=budget)
    capacity=sizing.size_for_entry(x)
    if not capacity['allowed']:return dict(state='BLOCKED',reason=capacity['reason'])
    qty=min(capacity['max_quantity'],int(os.environ['EXEC_MAX_QTY']),p['max_quantity'])
    scope=p.get('broker_workflow_verification',{})
    if scope.get('evidence_kind') in ('ACCOUNT_HISTORY_CONTROLLED_PILOT','ACCOUNT_BROKER_HISTORY'):
        qty=min(qty,scope.get('max_quantity',1))
    if qty<1:return dict(state='BLOCKED',reason='ict_risk_below_one_contract')
    if guardrails.exec_mode()!='auto' or os.environ.get('AUTO_SUBMIT')!='1':
        return dict(state='BLOCKED',reason='automatic_entries_disabled')
    _,news_hard=news_flags(x)
    allowed,why=guardrails.guard_ok(x,feed_age_min=feed_age(),market_open=market_open(),news_hard=news_hard,cal_age_h=calendar_age())
    if not allowed:return dict(state='BLOCKED',reason=why)
    native=dict(order,quantity=qty,contract=live.contract(),route_id=live.route(),
        account_label=os.environ.get('ACCOUNT_LABEL','account'),lifecycle='ICT_NATIVE_FIXED_BRACKET_V1',
        baseline_target_price=order['target_price'],runner_enabled=False,risk_points=abs(order['entry_price']-order['stop_price']))
    with live.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM orders WHERE order_id=?',(order['order_id'],)).fetchone():
            return dict(state='BLOCKED',reason='ict_already_prepared_no_retry')
        if db.execute("SELECT 1 FROM native_orders WHERE state NOT IN ('NO_FILL','CLOSED')").fetchone():
            return dict(state='BLOCKED',reason='ict_account_owned')
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(order['order_id'],'PREPARED',json.dumps(native),None,live.now_ms()))
    gid='ict_ai_'+order['order_id']
    x.update(_sent_qty=qty,_batch_group_id=gid,_setup_group_id=gid,sl_src='struct',tp_src='2R')
    result=None
    try:
        if not guardrails.begin_sibling_batch(gid,budget,['ICT AI']):
            result=dict(sent=False,status=422,reason='ict_batch_reservation_failed')
        else:
            execution_trace.record(order['order_id'],'ict_entry_sizing',entry=native['entry_price'],structural_sl=native['stop_price'],target=native['target_price'],quantity=qty,capacity=capacity)
            payload=dict(ticker=native['contract'],action='buy' if native['direction']=='LONG' else 'sell',orderType='limit',
                limitPrice=native['entry_price'],quantity=qty,stopLoss=dict(stopPrice=native['stop_price']),
                takeProfit=dict(limitPrice=native['target_price']),timeInForce='gtd',
                cancelAfter=max(0,(native['expiry_ms']-live.now_ms())//1000))
            result=broker.submit(payload,order['order_id'])
    except broker.TransportError as exc:
        result=dict(sent=False,status=422,reason=str(exc))
    except Exception:
        result=dict(sent=False,submission_unknown=True,reason='ict_submission_unknown_no_retry')
    unknown=bool(result.get('submission_unknown') or result.get('accepted_any'))
    state='SENT' if result.get('sent') else 'SUBMISSION_UNKNOWN' if unknown else 'BLOCKED'
    local='WAITING_BROKER_FILL' if result.get('sent') else 'SUBMISSION_UNKNOWN' if unknown else 'REJECTED'
    with live.connect() as db:
        db.execute("UPDATE orders SET state=?,updated_ms=? WHERE order_id=? AND state='PREPARED'",(local,live.now_ms(),order['order_id']))
    if result.get('sent') or unknown:guardrails.touch_sibling_batch(gid,'ICT AI',result.get('status'))
    if not result.get('sent'):guardrails.finish_sibling_batch(gid,'native_reconciliation_pending' if unknown else 'failed_before_accept')
    guardrails.note(x,'sent' if result.get('sent') or unknown else 'blocked',
        'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED' if unknown and not result.get('sent') else result.get('reason'))
    return dict(state=state,reason=result.get('reason','ok'),quantity=qty,route_id=native['route_id'],account_label=native['account_label'],broker=result)

