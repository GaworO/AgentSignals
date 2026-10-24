import copy
import json
from unittest.mock import Mock

import pytest

import ab_v3_live
import builder_broker_sync
import continuation_live
import directional_risk_revalidation as risk
import guardrails as guard
import test_builder_crosstrade as fixture

from test_directional_completion import control_proof

REAL_GUARD_OK = guard.guard_ok
REAL_ACCOUNT_BLOCKER = builder_broker_sync.entry_blocker


@pytest.fixture
def native(monkeypatch, tmp_path):
    f = fixture.NativeTests()
    f.setUp()
    monkeypatch.setenv('AUTO_SUBMIT', '1')
    monkeypatch.setenv('EXEC_MAX_QTY', '15')
    monkeypatch.setenv('DAY_LOSS_USD','550');monkeypatch.setenv('DAY_LOSS_N','1')
    f.account['balance'].update(realizedPnL=0,openPnL=0,cashSOD=48961.55,
                                fundsInTransit=0,fundsInTransitAppliedToCash=0)
    evidence=tmp_path/'allowances.json'
    evidence.write_text(json.dumps(dict(provider='MFFU',account_binding=f.sync.binding,
        source_reference='SYNTHETIC_TEST_ONLY',verified=True,observed_at_ms=int(f.now*1000)-1000,
        expires_at_ms=int(f.now*1000)+600000,minimum_balance=48000,
        daily_loss_limit_usd=1000,max_micro_contracts=40,account_controls=control_proof(f.now*1000,48961.55))))
    monkeypatch.setenv('MFFU_RISK_EVIDENCE_FILE',str(evidence))
    monkeypatch.setattr(risk.execution_validation,'policy',lambda:dict(contract='MNQZ2026',
        costs=dict(contract='MNQZ2026',validation_reference='SYNTHETIC_TEST_ONLY',round_trip_usd=1.2,stop_slippage_ticks=0)))
    monkeypatch.setattr(continuation_live, 'DB_PATH', tmp_path/'dispatch.sqlite3')
    continuation_live._init_db()
    f.order['target_price']=30020.
    f.prepare()
    f.source = dict(f.order, candidate_id='fixture', dol_id='FIXED_2R')
    assert continuation_live._claim(f.source, 'fixture')
    monkeypatch.setattr(ab_v3_live, 'connect', f.api.connect)
    # Paper attestation contract has its own Phase 2B tests; this fixture tests risk.
    monkeypatch.setattr(ab_v3_live,'native_bracket_readiness',lambda:None)
    monkeypatch.setattr(builder_broker_sync, 'instance', lambda:f.sync)
    f.signal = dict(dir='LONG', entry=30000., SL=29990., TP=30020., model='A/B Directional',
                    _strict_risk_budget=True,_disable_partial=True,_risk_budget_usd=250.,
                    _v3_directional=True, _strat='A/B Directional LONG', _continuation_order_id='order-one')
    monkeypatch.setattr(guard, 'exec_mode', lambda:'auto')
    monkeypatch.setattr(guard, 'account_profile', lambda:dict(config_ok=True))
    f.guard_check = Mock(return_value=(True, 'ok'))
    monkeypatch.setattr(guard, 'guard_ok', f.guard_check)
    f.validator = risk.RiskValidator(signal_builder=lambda source:dict(f.signal),
        budget_builder=lambda profile:250., feed_age=lambda:0., market_open=lambda:True,
        news_flags=lambda signal:([],False), calendar_age=lambda:.1, clock=lambda:f.now)
    try:
        yield f
    finally:
        f.doCleanups()


def test_final_risk_uses_persisted_dispatch_preparation_and_fresh_broker(native):
    f = native
    poll = Mock(wraps=f.sync.poll)
    f.sync.poll = poll
    proof = f.validator(dict(f.order))
    assert proof['allowed'] is True and proof['account_binding'] == f.sync.binding
    assert proof['checked_ms'] == int(f.sync.snapshot()['read_started_at']*1000)
    poll.assert_called_once_with(wait=True)
    _, kwargs = f.guard_check.call_args
    assert kwargs['reservation_id'] == 'ab_directional_order-one'
    assert kwargs['market_open'] is True and kwargs['news_hard'] is False
    assert f.posts == []


def test_costs_cannot_push_original_trade_over_guard_budget(native):
    f=native
    f.order['quantity']=12
    f.prepare()
    proof=f.validator(dict(f.order))
    assert not proof['allowed'] and proof['reason']=='cost_inclusive_trade_risk_budget'
    f.guard_check.assert_not_called()
    assert f.posts==[]


@pytest.mark.parametrize('change,reason', [
    ('mode', 'automatic_entries_disabled'),
    ('quantity', 'account_quantity_or_risk_budget'),
    ('geometry', 'dispatch_geometry_changed'),
    ('identity', 'dispatch_identity_changed'),
    ('source', 'owned_forward_dispatch_required'),
    ('prepared', 'entry_no_longer_prepared'),
    ('risk', 'day_loss_usd'),
    ('broker', 'fresh_broker_snapshot_required'),
])
def test_risk_revalidation_never_accepts_missing_or_changed_evidence(native, monkeypatch, change, reason):
    f = native
    if change == 'mode':monkeypatch.setattr(guard, 'exec_mode', lambda:'off')
    elif change == 'quantity':
        f.order['quantity'] = 30;f.prepare()
    elif change == 'geometry':f.order['stop_price'] = 29989.
    elif change == 'identity':f.order['expiry_ms'] += 1
    elif change == 'source':
        with continuation_live._connect() as db:db.execute("UPDATE live_dispatches SET state='SENT'")
    elif change == 'prepared':
        with f.api.connect() as db:db.execute("UPDATE orders SET state='GUARD_EXIT_PENDING'")
    elif change == 'risk':f.guard_check.return_value = (False, 'day_loss_usd')
    else:monkeypatch.setattr(f.sync, 'poll', lambda **kw:False)
    proof = f.validator(dict(f.order))
    assert not proof['allowed'] and proof['reason'] == reason
    assert f.posts == []


@pytest.mark.parametrize('change', ['expiry', 'exit_request', 'guard_state', 'kill_switch'])
def test_no_post_when_entry_changes_during_final_evidence_reads(native, monkeypatch, change):
    f = native
    def delayed(order, review):
        if change == 'expiry':f.now += 61
        elif change == 'kill_switch':monkeypatch.setenv('EXEC_KILL_SWITCH', 'true')
        else:
            with f.api.connect() as db:
                if change == 'exit_request':
                    db.execute('INSERT INTO native_exit_requests VALUES(?,?,?,?)',
                               (f.t.binding,'order-one','fixture',int(f.now*1000)))
                else:db.execute("UPDATE orders SET state='GUARD_EXIT_PENDING'")
        return None
    f.final_gate.side_effect = delayed
    result = f.t.submit(f.payload(), 'order-one')
    assert not result['sent'] and not result['submission_unknown']
    assert f.posts == []


def test_own_native_reservation_does_not_hide_other_unresolved_orders(native):
    f = native
    def gate(order, review):
        assert f.t.entry_blocker() == 'broker:native_order_or_cost_reconciliation_pending'
        assert f.t.entry_blocker(pending_order_id='order-one') is None
        assert f.t.entry_blocker(pending_order_id='another') == 'broker:unsubmitted_native_reservation_required'
        with f.api.connect() as db:
            row = dict(db.execute('SELECT * FROM native_orders').fetchone())
            row.update(order_id='other-order',client_id='other-client',state='CLOSED_PENDING_COSTS')
            db.execute('INSERT INTO native_orders VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        assert f.t.entry_blocker(pending_order_id='order-one') == 'broker:native_order_or_cost_reconciliation_pending'
        return 'fixture_block'
    f.final_gate.side_effect = gate
    assert not f.t.submit(f.payload(), 'order-one')['sent']
    assert f.posts == []


@pytest.fixture
def guarded(monkeypatch):
    now = 1792504800000  # 2026-10-20 10:00 New York
    signal = dict(_continuation_order_id='one', _v3_directional=True,
                  _strat='A/B Directional LONG', _planned_group_risk_usd=250.,
                  dir='LONG', entry=30000., SL=29990., TP=30020., sess='NYAM')
    state = dict(equity_ts=now, kill=False, pending_group=dict(group_id='ab_directional_one',
                 strats=['A/B Directional LONG'],accepted=[],planned_risk_usd=250.,expires_ms=now+60000))
    stats = dict(openpos=0,sent=0,losses=0,net=0.)
    monkeypatch.setattr(guard, '_state', lambda:copy.deepcopy(state))
    monkeypatch.setattr(guard, '_now_ms', lambda:now)
    monkeypatch.setattr(guard, 'beat', Mock())
    monkeypatch.setattr(guard, 'is_duplicate', lambda x:False)
    monkeypatch.setattr(guard, 'eval_progress', lambda:dict(passed=False,breached=False))
    monkeypatch.setattr(guard, '_day_stats', lambda:dict(stats))
    monkeypatch.setattr(guard, '_peer_busy', lambda:None)
    monkeypatch.setattr(guard, '_loss_streak_stats', lambda:dict(breached=False))
    monkeypatch.setattr(guard, '_modeled_equity', lambda *a:50000.)
    monkeypatch.setattr(guard, '_dd_floor', lambda:48000.)
    monkeypatch.setattr(guard.builder_broker_sync, 'entry_blocker', lambda **kw:None)
    monkeypatch.setenv('GUARD_LAST_ENTRY_ET', '15:55')
    monkeypatch.setenv('DAY_LOSS_USD', '550')
    monkeypatch.setenv('DAY_LOSS_N', '1')
    monkeypatch.setenv('MAX_TRADES_DAY', '3')
    latch = Mock();monkeypatch.setattr(guard, '_latch', latch)
    def check():return guard.guard_ok(signal,0,True,False,0,reservation_id='ab_directional_one')
    return state,stats,signal,check,latch


@pytest.mark.parametrize('key,value,reason', [('losses',1,'day_loss_n'),('net',-550.,'day_loss_usd'),
                                            ('sent',3,'max_trades_day'),('openpos',1,'position_open')])
def test_owned_reservation_preserves_actual_daily_and_position_guards(guarded,key,value,reason):
    state,stats,signal,check,latch = guarded
    assert check() == (True,'ok')
    stats[key] = value
    assert check() == (False,reason)
    if reason.startswith('day_loss'):latch.assert_called_once_with(reason)


@pytest.mark.parametrize('field,value', [('group_id','other'),('accepted',[{'status':200}]),
                                        ('strats',['A/B']),('planned_risk_usd',500.)])
def test_reserved_group_exception_cannot_borrow_another_batch(guarded,field,value):
    state,stats,signal,check,latch = guarded
    state['pending_group'][field] = value
    assert check() == (False,'owned_unsubmitted_group_required')


@pytest.mark.parametrize('daily_loss', [False, True])
def test_native_submission_rechecks_real_guard_with_its_own_reservation(native, guarded, monkeypatch, daily_loss):
    f = native
    state,stats,signal,check,latch = guarded
    state['pending_group']['group_id'] = 'ab_directional_order-one'
    f.signal.update(signal, _continuation_order_id='order-one', TP=f.order['target_price'])
    if daily_loss:stats['net'] = -550.
    monkeypatch.setattr(guard, 'guard_ok', REAL_GUARD_OK)
    monkeypatch.setattr(builder_broker_sync, 'entry_blocker', REAL_ACCOUNT_BLOCKER)
    proofs = []
    def final_gate(order, review):
        proof = f.validator(order);proofs.append(proof)
        return None if proof['allowed'] else proof['reason']
    f.final_gate.side_effect = final_gate
    result = f.t.submit(f.payload(), 'order-one')
    assert proofs[0]['allowed'] is not daily_loss
    assert result['sent'] is not daily_loss
    assert len(f.posts) == (0 if daily_loss else 1)
    if daily_loss:
        assert result['reason'] == 'day_loss_usd'
        latch.assert_called_once_with('day_loss_usd')


def test_integrated_reconciled_risk_quote_ai_guard_and_one_mnq_protocol(native,guarded,monkeypatch,tmp_path):
    """All external evidence mocked; actual adapter, sizing, Guard and gate logic."""
    import directional_account_reconciliation as ar
    import directional_account_limits as limits
    import execution_validation
    from test_execution_readiness import evidence
    f=native;state,stats,signal,check,latch=guarded
    state['pending_group']['group_id']='ab_directional_order-one'
    f.signal.update(signal,_continuation_order_id='order-one',TP=f.order['target_price'])
    monkeypatch.setattr(guard,'guard_ok',REAL_GUARD_OK)
    monkeypatch.setattr(builder_broker_sync,'entry_blocker',REAL_ACCOUNT_BLOCKER)
    monkeypatch.setenv('EXEC_MAX_QTY','1')
    f.order['quantity']=1;f.prepare();f.sync.config['AB_V3_CONTRACT']='MNQZ2026'
    now=int(f.now*1000);anchor=now-2*86400000
    proof=limits.read_static_evidence();proof.pop('account_controls')
    proof.update(account_controls_source='CROSSTRADE_RECONCILED',
        reconciliation_anchor=dict(verified=True,flat=True,account_binding=f.sync.binding,
            source_reference='MOCKED_FLAT_STATEMENT',at_ms=anchor,cash=48961.55,
            capture_complete_since_ms=anchor,coverage_reference='MOCKED_CAPTURE_COVERAGE',consecutive_losses=0),
        margin_policy=dict(verified=True,source_reference='MOCKED_RULE',mode='CONTRACT_CAP_ONLY',
            contract='MNQZ2026',observed_at_ms=anchor,expires_at_ms=now+60000))
    import os
    from pathlib import Path
    Path(os.environ['MFFU_RISK_EVIDENCE_FILE']).write_text(json.dumps(proof))
    f.values['/fills/history?account=Builder+account&environment=demo&limit=1000']=[]
    f.values['/marginsnapshots']=[dict(environment='demo',data=[dict(id=2214)])]
    assert f.sync.poll()
    assert ar.state(f.sync)['verified'], ar.state(f.sync)
    sized=limits.capacity(f.sync.snapshot(),f.sync.binding,f.order,now,1.2,250.)
    assert sized['allowed'] and sized['max_quantity']==1 and sized['total_risk_usd']==21.2
    args=evidence();args[0]=dict(f.order);args[-1]=now
    args[1].update(bid=29999.75,ask=30000.,as_of_ms=now)
    args[2].update(order_id='order-one',checked_ms=now)
    args[4].update(order_id='order-one',setup=dict(f.order),available_at=f.now-1,expires_at=f.now+60)
    decisions=[]
    def final_gate(order,review):
        actual_risk=f.validator(order)
        decision=execution_validation.evaluate(order,args[1],args[2],actual_risk,args[4],args[5],now)
        decisions.append((actual_risk,decision));return decision
    f.final_gate.side_effect=final_gate
    payload=f.payload();payload['quantity']=1
    result=f.t.submit(payload,'order-one')
    assert result['sent'] and all(r['allowed'] and d is None for r,d in decisions)
    assert len(f.posts)==1 and f.posts[0][1]['qty']==1 and f.local()['position_json'] is None
    f.client=result['client_id'];f.lifecycles()
    for value in f.life.values():value['version']['orderQty']=1
    f.life[11]['order']['ordStatus']='Filled'
    for oid in (12,13):f.life[oid]['order']['ordStatus']='Working'
    fill=f.fill(101,11,1,30000.,'Buy',offset=0)
    f.now+=1;f.t.reconcile_one(f.t.row('order-one'),f.snapshot([fill],1))
    assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
    assert json.loads(f.t.row('order-one')['observation_json'])['protection_verified'] is True
    assert not ar.state(f.sync)['verified']  # Open exposure cannot authorize another entry.
