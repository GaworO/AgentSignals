"""Synthetic protocol/ledger tests. No broker writes and no AI calls."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import directional_account_reconciliation as ar
import directional_account_limits as limits


def ms(s):
    return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)


def fixture():
    now=ms('2026-10-20T15:00:00Z')
    anchor=ms('2026-10-19T00:00:00Z')
    proof=dict(provider='MFFU',verified=True,account_binding='bound',source_reference='MOCKED_RULES',
        observed_at_ms=anchor,expires_at_ms=now+10000,minimum_balance=900.,max_micro_contracts=40,
        daily_loss_policy='NONE',daily_loss_limit_usd=None,daily_loss_source_reference='MOCKED_NO_DLL',
        reconciliation_anchor=dict(verified=True,account_binding='bound',source_reference='MOCKED_FLAT',
            flat=True,at_ms=anchor,cash=1000.,consecutive_losses=0,
            capture_complete_since_ms=anchor,coverage_reference='MOCKED_COVERAGE'),
        margin_policy=dict(verified=True,source_reference='MOCKED_CAP_RULE',contract='MNQZ2026',
            observed_at_ms=anchor,expires_at_ms=now+10000,mode='CONTRACT_CAP_ONLY'))
    rows=[dict(fillId=1,orderId=10,accountId=1,accountName='Eval',environment='demo',
        instrument='MNQ 12-26',action='Sell',qty=1,price=100.,commission=.5,fees=.5,timestamp='2026-10-20T14:00:00Z'),
        dict(fillId=2,orderId=11,accountId=1,accountName='Eval',environment='demo',
        instrument='MNQ 12-26',action='Buy',qty=1,price=110.,commission=.5,fees=.5,timestamp='2026-10-20T14:01:00Z')]
    snap=dict(read_started_at=now/1000-.1,observed_at=now/1000,positions=[],orders=[],
        fills=[dict(id=r['fillId'],**{k:r[k] for k in ('orderId','qty','price','action')}) for r in rows],
        account=dict(balance=dict(totalCashValue=978.,netLiq=978.,realizedPnL=-22.,openPnL=0.,
            totalCashValueSOD=1000.,fundsInTransitUSD=0.,fundsInTransitAppliedToCashUSD=0.)))
    margin=dict(initialMargin=0.,maintenanceMargin=0.,totalUsedMargin=0.,openCollateralReq=0.)
    return [snap,rows,margin,proof,'bound','MNQZ2026',now]


def test_entire_account_loss_includes_non_bot_order_and_costs():
    args=fixture();r=ar.assess(*args)
    assert r['verified'] and r['controls']['losses_today']==1
    assert r['controls']['margin_max_new_micro_contracts']==40
    assert r['ledger']['cash']==978 and r['observed']['captured_costs_usd']==2
    assert r['controls']['remaining_daily_loss_usd'] is None


@pytest.mark.parametrize('change,reason',[
    ('missing_rules','current_account_rules_missing_or_expired'),
    ('missing_anchor','account_history_coverage_and_flat_anchor_missing'),
    ('late_anchor','account_history_coverage_and_flat_anchor_missing'),
    ('missing_margin','account_margin_rule_unknown_or_expired'),
    ('missing_floor','current_drawdown_floor_unknown'),
    ('missing_max','account_maximum_mnq_unknown'),
    ('missing_daily','account_daily_loss_rule_unknown'),
    ('positions','account_has_positions_or_orders'),
    ('orders','account_has_positions_or_orders'),
    ('stale','account_snapshot_stale'),
    ('late_fee','cash_fills_fees_or_transfer_unreconciled'),
    ('unknown_fee','captured_fees_unknown'),
    ('history_lag','live_fill_not_reconciled_with_durable_history'),
    ('foreign_product','account_history_unknown_instrument_multiplier'),
])
def test_missing_or_contradictory_evidence_never_grants_controls(change,reason):
    a=fixture()
    if change=='missing_rules':a[3]={}
    if change=='missing_anchor':a[3].pop('reconciliation_anchor')
    if change=='late_anchor':a[3]['reconciliation_anchor']['at_ms']=a[-1]-1000
    if change=='missing_margin':a[3].pop('margin_policy')
    if change=='missing_floor':a[3].pop('minimum_balance')
    if change=='missing_max':a[3].pop('max_micro_contracts')
    if change=='missing_daily':a[3].pop('daily_loss_source_reference')
    if change=='positions':a[0]['positions']=[{'netPos':1}]
    if change=='orders':a[0]['orders']=[{'ordStatus':'Working'}]
    if change=='stale':a[-1]+=31000
    if change=='late_fee':a[1][1]['fees']=1.5
    if change=='unknown_fee':a[1][1]['fees']=None
    if change=='history_lag':a[1].pop()
    if change=='foreign_product':a[1][0]['instrument']='MES 12-26'
    r=ar.assess(*a)
    assert not r['verified'] and r['controls'] is None and reason in r['missing']


def test_fee_revision_reconciles_only_after_cash_matches():
    a=fixture();a[1][1]['fees']=1.5
    assert not ar.assess(*a)['verified']
    a[0]['account']['balance'].update(totalCashValue=977.,netLiq=977.,realizedPnL=-23.)
    assert ar.assess(*a)['verified']


def test_streak_and_daily_limit_preserved():
    a=fixture();a[3]['reconciliation_anchor'].update(consecutive_losses=2,last_loss_closed_ms=a[3]['observed_at_ms']-1000)
    a[3].update(daily_loss_policy='LIMITED',daily_loss_limit_usd=100.)
    r=ar.assess(*a);assert r['controls']['consecutive_losses']==3
    assert r['controls']['remaining_daily_loss_usd']==78


def test_fixed_margin_uses_explicit_dated_rule_and_provider_values():
    a=fixture();a[3]['margin_policy'].update(mode='FIXED_PER_CONTRACT',usd_per_contract=100.)
    assert ar.assess(*a)['controls']['margin_max_new_micro_contracts']==9
    a[2]['openCollateralReq']=900.
    assert ar.assess(*a)['controls']['margin_max_new_micro_contracts']==0
    a[3]['margin_policy']['usd_per_contract']=None
    assert not ar.assess(*a)['verified']


def test_reversal_or_unmatched_opening_inventory_is_not_ignored():
    a=fixture();a[1][1]['qty']=2
    r=ar.assess(*a);assert 'account_history_reversal_requires_reconciliation' in r['missing']
    a=fixture();a[1]=a[1][:1]
    assert 'account_history_not_flat' in ar.assess(*a)['missing']


def test_history_pagination_bound_to_exact_account_and_conflicts():
    a=fixture();pages=[dict(success=True,data=[a[1][0]],nextCursor='next/one'),dict(success=True,data=[a[1][1]],nextCursor=None)]
    called=[]
    def read(path):called.append(path);return pages.pop(0)
    sync=SimpleNamespace(name='Eval',account='1',environment='demo',reader=read)
    assert len(ar.history(sync))==2 and 'cursor=next%2Fone' in called[1]
    wrong=copy.deepcopy(a[1][0]);wrong['accountId']=2
    sync.reader=lambda p:dict(success=True,data=[wrong])
    with pytest.raises(ValueError,match='binding'):ar.history(sync)
    conflict=dict(a[1][0],fees=9)
    sync.reader=lambda p:dict(success=True,data=[a[1][0],conflict])
    with pytest.raises(ValueError,match='duplicate'):ar.history(sync)
    sync.reader=lambda p:dict(success=True,data=[],nextCursor='same')
    with pytest.raises(ValueError,match='cursor'):ar.history(sync)


def test_guard_risk_limit_consumes_generated_controls(tmp_path,monkeypatch):
    a=fixture();proof=dict(a[3],account_controls=ar.assess(*a)['controls'])
    path=tmp_path/'rules.json';path.write_text(json.dumps(proof))
    monkeypatch.setenv('MFFU_RISK_EVIDENCE_FILE',str(path));monkeypatch.setenv('DAY_LOSS_N','1')
    monkeypatch.setenv('DAY_LOSS_USD','550');monkeypatch.setenv('DD_FLOOR','900');monkeypatch.setenv('DD_BUFFER','0');monkeypatch.setenv('SETUP_GROUP_FLOOR_RESERVE_USD','0')
    order=dict(quantity=1,direction='LONG',entry_price=100.,stop_price=99.,contract='MNQZ2026')
    assert limits.check(a[0],'bound',order,a[-1],2)=='account_wide_one_loss_stop'


def test_runtime_controls_bound_to_exact_static_rule_hash(monkeypatch,tmp_path):
    import hashlib,builder_broker_sync
    a=fixture();proof=dict(a[3],account_controls_source='CROSSTRADE_RECONCILED')
    p=tmp_path/'rules.json';p.write_text(json.dumps(proof));monkeypatch.setenv('MFFU_RISK_EVIDENCE_FILE',str(p))
    r=ar.assess(*a);r['rules_sha256']=hashlib.sha256(builder_broker_sync.encode(proof).encode()).hexdigest()
    monkeypatch.setattr(ar,'state',lambda:r)
    assert limits.read_evidence()['account_controls']['losses_today']==1
    proof['max_micro_contracts']=41;p.write_text(json.dumps(proof))
    with pytest.raises(ValueError,match='unverified'):limits.read_evidence()


def test_history_read_allowlist_rejects_other_account_before_network(tmp_path):
    from builder_broker_sync import AccountSync,SyncError
    sync=AccountSync(dict(DATA_DIR=str(tmp_path),BUILDER_BROKER_ACCOUNT_NAME='Eval',BUILDER_BROKER_ENVIRONMENT='demo'))
    for query in ['account=Other&environment=demo&limit=1000','account=Eval&environment=demo&limit=1000&url=evil','account=Eval&account=Other&environment=demo&limit=1000']:
        with pytest.raises(SyncError,match='ROUTE_NOT_ALLOWED'):sync._read('/fills/history?'+query)


def test_poll_persistence_restart_staleness_and_provider_failure(tmp_path,monkeypatch):
    from builder_broker_sync import AccountSync
    a=fixture();clock=[a[-1]/1000]
    config=dict(DATA_DIR=str(tmp_path),ACCOUNT_PLAN='builder50',BUILDER_BROKER_SYNC_ENABLED='1',
        BUILDER_BROKER_ACCOUNT_VERIFIED='1',BUILDER_BROKER_ACCOUNT_ID='1',BUILDER_BROKER_ACCOUNT_NAME='Eval',
        BUILDER_BROKER_ENVIRONMENT='demo',BUILDER_BROKER_API_TOKEN='SYNTHETIC',AB_V3_CONTRACT='MNQZ2026')
    account=dict(a[0]['account'],accountId=1,name='Eval',environment='demo',active=True,archived=False)
    values={'/accounts':[account],'/accounts/Eval':account,'/accounts/Eval/positions':[],
        '/accounts/Eval/orders':[],'/accounts/Eval/fills':a[0]['fills'],
        '/fills/history?account=Eval&environment=demo&limit=1000':a[1],
        '/marginsnapshots':[dict(environment='demo',data=[dict(a[2],id=1)])]}
    def read(path):return dict(success=True,data=copy.deepcopy(values[path]))
    sync=AccountSync(config,reader=read,clock=lambda:clock[0])
    proof=a[3];proof['account_binding']=sync.binding;proof['reconciliation_anchor']['account_binding']=sync.binding
    monkeypatch.setattr(limits,'read_static_evidence',lambda:proof)
    assert sync.poll() and ar.state(sync)['verified']
    restarted=AccountSync(config,reader=read,clock=lambda:clock[0])
    assert ar.state(restarted)['controls']['losses_today']==1
    clock[0]+=31
    assert not ar.state(restarted)['verified']
    clock[0]=a[-1]/1000+1
    def failed(path):
        if path.startswith('/fills/history'):raise TimeoutError('SYNTHETIC_PRIVATE_DETAIL')
        return read(path)
    restarted.reader=failed
    assert restarted.poll()  # Account balance success does not imply risk success.
    result=ar.state(restarted)
    assert not result['verified'] and result['controls'] is None
    assert 'PRIVATE' not in json.dumps(result)
