"""Offline broker protocol tests; these are NOT paper execution evidence."""
import copy
from types import SimpleNamespace
import pytest
from directional_management_broker import modification_reports
from directional_management_status import status
from directional_position_manager import Manager
from test_directional_position_manager import setup,propose,level,event


def lifecycle():
    return dict(order=dict(id=13,ordStatus='Working'),version=dict(id=77,orderId=13),
                commands=[dict(id=77,orderId=13,commandType='Modify',commandStatus='Replaced')],
                reports=[dict(id=100,orderId=13,commandId=77,commandStatus='Replaced',
                              ordStatus='Working',rejectReason='Success')])


def test_tradovate_success_is_not_rejection():
    assert modification_reports(lifecycle())==[dict(command_id=77,order_id=13,report_id=100,
                                                    rejected=False,confirmed=True)]


@pytest.mark.parametrize('mutation',[
    lambda x:x['commands'][0].update(commandStatus='AtExecution'),
    lambda x:x['reports'][0].update(commandStatus='AtExecution'),
    lambda x:x['reports'][0].update(commandStatus='Completed'),
    lambda x:x['reports'][0].update(ordStatus='PendingNew'),
    lambda x:x['order'].update(ordStatus='Canceled'),
    lambda x:x['version'].update(id=78),
    lambda x:x['version'].update(orderId=14),
    lambda x:x['commands'][0].update(orderId=14),
    lambda x:x['reports'][0].update(orderId=14),
    lambda x:x['reports'][0].update(commandId=78),
])
def test_wrong_or_unconfirmed_report_cannot_confirm(mutation):
    life=lifecycle();mutation(life)
    assert not any(r['confirmed'] for r in modification_reports(life))


def test_latest_report_wins_even_if_response_array_out_of_order():
    life=lifecycle();old=copy.deepcopy(life['reports'][0])
    life['reports'].insert(0,dict(old,id=101,rejectReason='RiskRejected'))
    report=modification_reports(life)[0]
    assert report['rejected'] and not report['confirmed'] and report['report_id']==101


def test_rejection_is_durable_only_after_complete_packet_validates(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    i=m.request('one',p,a,120000)
    b['observed_ms']+=100;b['command_reports']=[dict(command_id=77,order_id=13,rejected=True,confirmed=False)]
    broken=copy.deepcopy(b);broken['protective_orders'][0]['quantity']=1
    assert m.reconcile('one',broken,b['observed_ms'])['state']=='RECONCILIATION_REQUIRED'
    with m.connect() as c:
        assert c.execute('SELECT status FROM ict_management_intents WHERE intent_id=?',(i['id'],)).fetchone()[0]=='ACK'
    m.reconcile('one',b,b['observed_ms'])
    resumed=Manager(m.connect,m.binding)
    with m.connect() as c:
        assert c.execute('SELECT status FROM ict_management_intents WHERE intent_id=?',(i['id'],)).fetchone()[0]=='REJECTED'
    assert resumed.get('one')['last_stop']==90
    b['observed_ms']+=100
    assert resumed.reconcile('one',b,b['observed_ms'])['state']=='PROTECTED'


def test_fresh_poll_of_same_facts_does_not_block_request(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    b['observed_ms']+=100
    assert m.request('one',p,a,120100)['status']=='ACK'
    assert len(a.sent)==1


def test_duplicate_stale_snapshot_cannot_retain_protected_state(tmp_path):
    m,b,a=setup(tmp_path)
    assert m.reconcile('one',b,124000)['state']=='RECONCILIATION_REQUIRED'
    b['observed_ms']=124100
    assert m.reconcile('one',b,124100)['state']=='PROTECTED'


def test_report_for_other_order_cannot_confirm(tmp_path):
    m,b,a=setup(tmp_path);p=propose(m,b,{'trail':level(105.)},ev=event(kind='CONFIRMED_SWING'))
    m.request('one',p,a,120000);b['observed_ms']+=100
    b['protective_orders'][1]['price']=105.
    b['command_reports']=[dict(command_id=77,order_id=12,confirmed=True,rejected=False)]
    assert m.reconcile('one',b,b['observed_ms'])['reason']=='COMMAND_REPORT_ORDER_MISMATCH'
    assert m.get('one')['last_stop']==90


def test_status_is_bound_read_only_and_does_not_claim_stale_protection(tmp_path):
    m,b,a=setup(tmp_path);t=SimpleNamespace(api=SimpleNamespace(connect=m.connect),binding=m.binding)
    assert status(t,120000)['positions'][0]['state']=='PROTECTED'
    result=status(t,124000)
    assert result['positions'][0]['state']=='RECONCILIATION_REQUIRED'
    assert not result['active_management_writes'] and not result['broker_paper_verified']
    assert m.get('one')['state']=='PROTECTED'  # Display does not mutate the journal.
    t.binding='OTHER_ACCOUNT';assert status(t,120000)['positions']==[]


def test_status_without_transport_cannot_be_ready():
    assert status(None)['reason']=='NATIVE_TRANSPORT_NOT_CONFIGURED'


def test_health_reports_effective_interlocks_despite_healthy_transport(monkeypatch):
    import execution_policy as policy
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    monkeypatch.setenv('AB_V3_MODE','SHADOW')
    for side in ('LONG','SHORT'):monkeypatch.setenv('AB_DIRECTIONAL_LIVE_'+side,'0')
    monkeypatch.setattr(policy,'status',lambda:dict(live_execution_enabled=True,
        execution_kill_switch=True,entry_armed=False,revalidation=dict(ready=False,
        policy_error='validated_execution_policy_missing',
        access_blockers=['quote_source_suitability_unverified'],missing_validators=[])))
    result=policy.directional_health()
    assert not result['ready']
    assert set(result['blockers'])=={'execution_kill_switch','v3_entry_not_armed',
        'directional_mode_not_live','direction_not_enabled','validated_execution_policy_missing',
        'quote_source_suitability_unverified'}


def test_health_unknown_validation_cannot_be_green(monkeypatch):
    import execution_policy as policy
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    monkeypatch.setenv('AB_V3_MODE','LIVE')
    monkeypatch.setenv('AB_DIRECTIONAL_LIVE_LONG','1')
    monkeypatch.setattr(policy,'status',lambda:dict(live_execution_enabled=True,
        execution_kill_switch=False,entry_armed=True,revalidation={}))
    assert policy.directional_health()['blockers']==['execution_revalidation_not_ready']
