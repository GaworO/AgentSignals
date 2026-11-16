import json
import threading
from pathlib import Path
from unittest.mock import patch
import pytest
import builder_crosstrade as native
import test_builder_crosstrade as protocol


@pytest.fixture
def f():
    f=protocol.NativeTests();f.setUp()
    try:yield f
    finally:f.doCleanups()


def veto(f):
    def final(order,*a,**kw):
        return 'BRACKET_TOUCHED_BEFORE_SUBMISSION' if f.t.row('order-one') else None
    with patch.object(native.execution_validation,'check',side_effect=final):
        result=f.t.submit(f.payload(),'order-one')
    assert result['sent'] is False and f.posts==[]
    with f.api.connect() as c:c.execute("UPDATE orders SET state='REJECTED'")


def corrupt_stale_reconciler(f):
    with f.api.connect() as c:
        c.execute("UPDATE native_orders SET state='RECONCILIATION_REQUIRED',error='UNKNOWN_SUBMISSION_REQUIRES_EXACT_CLIENT_ID'")
        c.execute('INSERT INTO native_exit_requests VALUES(?,?,?,?)',(f.t.binding,'order-one','stale_error',0))
        c.execute('INSERT INTO native_operator_alerts VALUES(?,?,?,?,0)',(f.t.binding,'order-one','stale_error',0))


def test_concurrent_account_poll_cannot_reconcile_pre_post_reservation(f):
    reserved=threading.Event();release=threading.Event();results=[]
    def final(order,*a,**kw):
        if f.t.row('order-one'):
            reserved.set();assert release.wait(5)
            return 'BRACKET_TOUCHED_BEFORE_SUBMISSION'
        return None
    with patch.object(native.execution_validation,'check',side_effect=final):
        thread=threading.Thread(target=lambda:results.append(f.t.submit(f.payload(),'order-one')))
        thread.start()
        try:
            assert reserved.wait(5)
            with patch.object(f.t,'reconcile_one',side_effect=AssertionError('pre-POST reservation must not be reconciled')):
                f.t.reconcile(f.sync.snapshot())
        finally:release.set();thread.join(5)
    assert not thread.is_alive() and results[0]['sent'] is False
    assert f.t.row('order-one')['state']=='NO_FILL' and f.posts==[]
    with f.api.connect() as c:assert c.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==0


def test_restart_repairs_durable_veto_without_broker_io(f):
    veto(f);corrupt_stale_reconciler(f)
    def no_io(*a,**kw):raise AssertionError('No broker request permitted')
    t=native.Transport(f.api,f.sync,request=no_io,clock=lambda:f.now)
    t.reconcile_one(t.row('order-one'),f.sync.snapshot())
    assert t.row('order-one')['state']=='NO_FILL'
    assert json.loads(t.row('order-one')['observation_json'])['known_not_sent'] is True
    assert t.entry_blocker() is None and f.posts==[]
    with f.api.connect() as c:
        assert c.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==0
        assert c.execute('SELECT resolved FROM native_operator_alerts').fetchone()[0]==1
        assert c.execute('SELECT state FROM orders').fetchone()[0]=='REJECTED'
    with patch('directional_account_reconciliation.state',return_value=dict(verified=True,missing=[])):
        t.post_trade.check();assert t.post_trade.state()['state']=='SETTLED'


@pytest.mark.parametrize('case',['no_veto','submitted_phase','unknown_response','receipt','action','position','working_order','stale','local_open','different_reserved_body','different_client','duplicate_veto'])
def test_ambiguous_submission_is_never_reclassified_as_unsent(f,case):
    veto(f);corrupt_stale_reconciler(f);snapshot=f.sync.snapshot()
    with f.api.connect() as c:
        if case=='no_veto':c.execute("DELETE FROM native_audit WHERE kind='ENTRY_REVALIDATION_BLOCKED'")
        if case=='submitted_phase':f.t.audit(c,'order-one','LIFECYCLE_PHASE',dict(phase='SUBMITTED'))
        if case=='unknown_response':f.t.audit(c,'order-one','UNKNOWN',{})
        if case=='receipt':c.execute('UPDATE native_orders SET receipt_json=?',(json.dumps({'orderId':11}),))
        if case=='action':c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',(f.t.binding,'action','order-one','cancel','UNKNOWN','{}','{}',0))
        if case=='local_open':c.execute("UPDATE orders SET state='OPEN'")
        if case=='different_reserved_body':c.execute("UPDATE native_audit SET payload_json='{}' WHERE kind='ENTRY_RESERVED'")
        if case=='different_client':c.execute("UPDATE native_orders SET client_id='other'")
        if case=='duplicate_veto':f.t.audit(c,'order-one','ENTRY_REVALIDATION_BLOCKED',dict(reason='another'))
    if case=='position':snapshot['positions']=[{'netPos':1}]
    if case=='working_order':snapshot['orders']=[{'id':11}]
    if case=='stale':f.now+=4
    assert f.t.reconcile_not_sent(f.t.row('order-one'),snapshot) is False
    assert f.t.row('order-one')['state']=='RECONCILIATION_REQUIRED'


def test_stale_reconciliation_error_cannot_overwrite_a_terminal_veto(f):
    veto(f)
    with f.api.connect() as c:c.execute("UPDATE native_orders SET state='SENDING'")
    def stale_reader(row,snapshot):
        with f.api.connect() as c:c.execute("UPDATE native_orders SET state='NO_FILL'")
        raise native.TransportError('UNKNOWN_SUBMISSION_REQUIRES_EXACT_CLIENT_ID')
    with patch.object(f.t,'reconcile_one',side_effect=stale_reader):f.t.reconcile(f.sync.snapshot())
    assert f.t.row('order-one')['state']=='NO_FILL'
    with f.api.connect() as c:
        assert c.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM native_operator_alerts WHERE resolved=0').fetchone()[0]==0


def test_stale_row_reloads_terminal_state_without_provider_request(f):
    veto(f);old=f.t.row('order-one');old['state']='SENDING'
    with patch.object(f.t,'request',side_effect=AssertionError('No broker lookup')):
        f.t.reconcile_one(old,f.sync.snapshot())
    assert f.t.row('order-one')['state']=='NO_FILL'


def test_recorded_november_16_pre_post_veto_restores_only_not_sent(f):
    record=json.loads((Path(__file__).parent/'fixtures/pre_submit_veto_race.json').read_text())
    row=record['native_orders'][0];oid=row['order_id'];f.t.binding=row['binding']
    f.now=row['updated_ms']/1000+1;f.snapshot()
    with f.api.connect() as c:
        for table in ('native_orders','native_audit','native_order_phase','orders','native_exit_requests'):
            for item in record[table]:
                columns=list(item)
                c.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(item.values()))
    with patch.object(f.t,'request',side_effect=AssertionError('No broker I/O')):
        f.t.reconcile_one(row,f.sync.snapshot())
    assert f.t.row(oid)['state']=='NO_FILL'
    assert f.t.row(oid)['error']=='directional_thesis_not_revalidated:BRACKET_TOUCHED_BEFORE_SUBMISSION'
    assert json.loads(f.t.row(oid)['observation_json'])['known_not_sent'] is True
    with f.api.connect() as c:
        assert c.execute('SELECT count(*) FROM native_exit_requests WHERE order_id=?',(oid,)).fetchone()[0]==0
        assert c.execute('SELECT state FROM orders WHERE order_id=?',(oid,)).fetchone()[0]=='REJECTED'
