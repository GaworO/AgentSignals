"""Read-only runtime visibility; capability descriptions are not permissions."""
import json
import sqlite3
import time


def status(transport, now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    result=dict(observed_ms=now_ms,configured=transport is not None,
                active_management_writes=False,broker_paper_verified=False,
                write_blocker='PHASE3B_BROKER_MANAGEMENT_NOT_VERIFIED',
                operations={
                    'reconciliation':'CONNECTED_READ_ONLY',
                    'original_sl_tp_oco':'NATIVE_ENTRY_BRACKET_REQUIRES_PER_POSITION_PROOF',
                    'stop_modify':'COMMAND_REPORT_AND_VERSION_VALIDATION_IMPLEMENTED_PAPER_UNVERIFIED',
                    'target_modify':'COMMAND_REPORT_AND_VERSION_VALIDATION_IMPLEMENTED_PAPER_UNVERIFIED',
                    'partial_close':'BLOCKED_NO_ATOMIC_LINKED_BRACKET_RESIZE_PROOF',
                    'structural_trailing':'RESEARCH_RULES_ONLY',
                    'runner':'ORIGINAL_MODEL0_FULL_EXIT_ONLY_REQUIRES_SEPARATE_PROOF',
                    'early_exit':'ORIGINAL_M3_FULL_EXIT_PAPER_AND_QUOTE_GATED',
                    'guard_exit':'EXISTING_NATIVE_GUARD_PATH_UNCHANGED'},
                positions=[],pending_intents=[])
    if transport is None:
        result['reason']='NATIVE_TRANSPORT_NOT_CONFIGURED';return result
    # Configuration visibility is not permission for a specific position.
    # The writer repeats proof, quote and owned-position checks at dispatch.
    blockers=[]
    try:
        import execution_validation
        import execution_safety
        from directional_management_broker import full_exit_proof
        full_exit_proof(transport,execution_validation.policy(),now_ms)
        result['broker_paper_verified']=True
        if not execution_safety.writes_enabled():blockers.append('LIVE_EXECUTION_DISABLED')
        if transport.api.exit_mode()!='real':blockers.append('exit_mode_not_real')
        blockers.extend(transport.api.exit_blockers())
        if not transport.api.frozen_parity_ok():blockers.append('original_rule_parity_failed')
    except (ValueError,OSError,TypeError,KeyError,AttributeError) as exc:
        blockers.append(str(exc) if isinstance(exc,ValueError) else 'MANAGER_CONFIGURATION_UNKNOWN')
    result['original_full_exit']=dict(implemented=True,configured=not blockers,blockers=blockers,
        rule_source='v3_position_manager / frozen M3 / Model0',
        per_event_validation_required=True,advanced_modifications_enabled=False)
    result['active_management_writes']=not blockers
    result['write_blocker']=blockers[0] if blockers else 'PER_EVENT_VALIDATION_REQUIRED'
    try:
        with transport.api.connect() as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'ict_positions','ict_management_intents'}<=tables:
                result['reason']='NO_MANAGER_OBSERVATIONS';return result
            rows=c.execute('SELECT payload FROM ict_positions WHERE binding=?',(transport.binding,)).fetchall()
            for row in rows:
                s=json.loads(row[0]);at=s.get('observed_ms')
                age=now_ms-at if isinstance(at,(int,float)) else None
                fresh=age is not None and 0<=age<=3000
                # Display stale cached states as stale, never as current protection.
                result['positions'].append(dict(
                    setup_id=s['setup_id'],contract=s['contract'],direction=s['direction'],
                    recorded_state=s['state'],state=s['state'] if fresh else 'RECONCILIATION_REQUIRED',
                    reason=s['reason'] if fresh else 'BROKER_OBSERVATION_STALE',
                    observed_ms=at,age_ms=age,broker_fresh=fresh,
                    remaining_quantity=s.get('remaining_quantity'),stop=s.get('last_stop'),
                    target=s.get('last_target'),fees=s.get('fees'),
                    original_policy_decision=s.get('original_policy_decision'),
                    net_realized_usd=s.get('realized_net_usd')))
            for row in c.execute("SELECT payload FROM ict_management_intents WHERE binding=? AND status IN ('REQUESTED','ACK','UNKNOWN')",(transport.binding,)):
                i=json.loads(row[0]);result['pending_intents'].append({k:i.get(k) for k in
                    ('id','setup_id','operation','status','created_ms','command_id','reason')})
        result['reason']='OBSERVATION_ONLY'
    except (sqlite3.Error,ValueError,TypeError,KeyError):
        result.update(reason='INVALID_MANAGER_JOURNAL',positions=[],pending_intents=[])
    return result
