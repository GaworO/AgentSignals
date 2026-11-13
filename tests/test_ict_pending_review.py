"""Synthetic AI and native responses only; no API calls or evaluation orders."""
import copy
import json
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
import ict_pending_review as pending
import ict_execution
import ict_sessions
import builder_crosstrade as native
from test_resting_limit_lifecycle import f, submit, fill


@pytest.fixture
def ready(f,monkeypatch):
    submit(f)
    r=f.t.row(f.order['order_id']);cmd=json.loads(r['command_json'])
    cmd['order'].update(strategy='ICT_AI',expiry_ms=r['created_ms']+1200000)
    with f.api.connect() as db:
        db.execute('UPDATE native_orders SET command_json=?,expiry_ms=?',(json.dumps(cmd),r['created_ms']+600000))
    monkeypatch.setenv('ICT_PENDING_REVIEW_MODE',pending.VERSION)
    monkeypatch.setattr(ict_execution,'is_order',lambda o:o.get('strategy')=='ICT_AI')
    monkeypatch.setattr(f.t,'directional_permission',lambda o:None)
    monkeypatch.setattr(ict_sessions,'session',lambda now:('NYAM',f.now+3600,f.now-3600))
    request=f.t.request
    def send(method,path,body=None):
        if method=='PUT':
            f.posts.append((path,copy.deepcopy(body)))
            if f.timeout:raise TimeoutError()
            return dict(success=True,destination='tradovate',account=f.sync.name,api='modify_order',response=dict(commandId=888))
        return request(method,path,body)
    f.t.request=send
    f.now+=360
    return f


def propose(f,what='EXTEND',**changes):
    row=f.t.row(f.order['order_id'])
    d=dict(order_id=row['order_id'],binding=pending.binding(f.t,row),decision=what,reason='Synthetic ICT evidence',evidence_ids=['bar'],extension_until_ms=row['created_ms']+1200000 if what=='EXTEND' else None,current_expiry_ms=row['expiry_ms'],review_id='mock-1')
    d.update(changes)
    with f.api.connect() as db:
        db.execute('INSERT INTO ict_pending_reviews VALUES(?,?,?,?,?)',(f.t.binding,d['review_id'],row['order_id'],int(f.now*1000),json.dumps(d)))
    return d


def run(f,snapshot=None):
    row=f.t.row(f.order['order_id']);receipt=json.loads(row['receipt_json']);entry=f.t.entry_identity(row,receipt)
    snapshot=snapshot or f.snapshot();fact=f.t.exposure_fact(row,receipt,entry,snapshot)
    f.t.monitor_parent(row,receipt,entry,snapshot,fact)
    return row


def confirm(f,end):
    f.life[11]['version'].update(id=888,timeInForce='GTD',expireTime=datetime.fromtimestamp(end/1000,timezone.utc).isoformat())
    f.life[11]['commands'].append(dict(id=888,commandType='Modify',commandStatus='Replaced'))
    f.life[11]['reports'].append(dict(id=999,commandId=888,commandStatus='Replaced',ordStatus='Working',rejectReason='Success'))


def test_keep_never_changes_deadline(ready):
    f=ready;before=f.t.row(f.order['order_id'])['expiry_ms'];propose(f,'KEEP');run(f)
    assert len(f.posts)==1 and f.t.row(f.order['order_id'])['expiry_ms']==before
    f.now=before/1000;run(f)
    assert f.posts[-1][0].endswith('/11/cancel')


def test_cancel_only_parent(ready):
    f=ready;propose(f,'CANCEL');run(f)
    assert len(f.posts)==2 and f.posts[-1][0].endswith('/11/cancel')
    assert all(not p.endswith(('/12/cancel','/13/cancel')) for p,b in f.posts)


def test_extension_needs_actual_broker_version_and_is_once(ready):
    f=ready;d=propose(f);old=f.t.row(f.order['order_id'])['expiry_ms'];run(f)
    assert f.t.row(f.order['order_id'])['expiry_ms']==old  # ACK is not confirmation
    assert f.posts[-1][0].endswith('/11/change') and set(f.posts[-1][1])=={'tif','expireTime'}
    run(f);assert len(f.posts)==2  # no duplicate request
    confirm(f,d['extension_until_ms']);run(f)
    assert f.t.row(f.order['order_id'])['expiry_ms']==d['extension_until_ms']
    assert pending.extension_action(f.t,f.t.row(f.order['order_id']))['state']=='CONFIRMED'
    run(f);assert len(f.posts)==2
    f.now=d['extension_until_ms']/1000;run(f)
    assert f.posts[-1][0].endswith('/11/cancel')


def test_unknown_modify_never_retried_original_expiry_cancels(ready):
    f=ready;propose(f);f.timeout=True;run(f);f.timeout=False
    f.t=native.Transport(f.api,f.sync,request=f.t.request,clock=lambda:f.now)
    run(f);assert len(f.posts)==2
    f.now=f.t.row(f.order['order_id'])['expiry_ms']/1000;run(f)
    assert len(f.posts)==3 and f.posts[-1][0].endswith('/11/cancel')


@pytest.mark.parametrize('what',['CANCEL','EXTEND'])
@pytest.mark.parametrize('partial',[False,True])
def test_fill_during_review_never_changed_or_closed(ready,what,partial):
    f=ready;propose(f,what);snap=fill(f,terminal=not partial);run(f,snap)
    assert len(f.posts)==1


@pytest.mark.parametrize('failure',['binding','stale','disabled','expired','mechanical','too_long','setup_expiry','existing_cancel'])
def test_invalid_extension_blocked(ready,monkeypatch,failure):
    f=ready;d=propose(f,binding='wrong') if failure=='binding' else propose(f)
    if failure=='stale':f.now+=121
    if failure=='disabled':monkeypatch.delenv('ICT_PENDING_REVIEW_MODE')
    if failure=='expired':f.now=f.t.row(f.order['order_id'])['expiry_ms']/1000
    if failure=='mechanical':f.pending_gate.return_value='opposing_structure'
    if failure=='too_long':
        with f.api.connect() as db:
            d['extension_until_ms']+=1;db.execute('UPDATE ict_pending_reviews SET payload=?',(json.dumps(d),))
    if failure=='setup_expiry':
        row=f.t.row(f.order['order_id']);cmd=json.loads(row['command_json']);cmd['order']['expiry_ms']=row['expiry_ms']
        with f.api.connect() as db:db.execute('UPDATE native_orders SET command_json=?',(json.dumps(cmd),))
    if failure=='existing_cancel':
        row=f.t.row(f.order['order_id']);f.t.cancel_owned_order(row,11,'cancel','cancel')
    run(f)
    assert not any(p.endswith('/change') for p,b in f.posts)


def packet():
    p=dict(as_of=1000,session_end=5000,evidence={'bar':{}},pending_order=dict(order_id='a',binding='b',submitted_ms=600000,current_expiry_ms=1200000,original_setup_expiry_ms=1800000,extension_until_ms=1800000))
    d=dict(pending_order_review=dict(order_id='a',binding='b',decision='EXTEND',reason='valid',evidence_ids=['bar'],extension_until_ms=1800000))
    return p,d


def test_exact_schema_no_companion_duplicate():
    import ict_shadow_research as research
    p,d=packet();s=research.schema('p',include_ifvg=True,pending_order=p['pending_order'])
    assert 'pending_order_review' in s['required']
    assert 'pending_order_review' not in s['properties']['ifvg_review']['properties']
    assert pending.validate(p,d,1001)['state']=='VALIDATED_PENDING_REVIEW'


@pytest.mark.parametrize('failure',['identity','unknown_evidence','expired','too_long','keep_extension','unavailable','missing_reason'])
def test_response_validation(failure):
    p,d=packet();r=d['pending_order_review'];received=1001
    if failure=='identity':r['binding']='wrong'
    if failure=='unknown_evidence':r['evidence_ids']=['nonexistent']
    if failure=='expired':received=1200
    if failure=='too_long':r['extension_until_ms']+=1
    if failure=='keep_extension':r['decision']='KEEP'
    if failure=='unavailable':p['pending_order']['extension_until_ms']=None
    if failure=='missing_reason':r['reason']=' '
    with pytest.raises(AssertionError):pending.validate(p,d,received)


def test_snapshot_and_persisted_response_binding(ready,monkeypatch):
    import ict_shadow_research as research
    f=ready;row=run(f)
    cmd=json.loads(row['command_json']);cmd['order'].update(ict_review_id='original')
    with f.api.connect() as db:db.execute('UPDATE native_orders SET command_json=?',(json.dumps(cmd),))
    monkeypatch.setattr(ict_execution,'record_for',lambda o:dict(decision=dict(summary='Original idea',plan={},assessments={})))
    p=pending.snapshot(f.now)
    assert p['quantity']==1 and p['extension_until_ms']==row['created_ms']+1200000
    packet=dict(as_of=f.now,session_end=f.now+3600,evidence={'bar':{}},pending_order=p)
    packet['packet_id']=research.digest(packet)
    d=dict(pending_order_review=dict(order_id=p['order_id'],binding=p['binding'],decision='KEEP',reason='still valid',evidence_ids=['bar'],extension_until_ms=None))
    record=dict(id='saved-review',packet=packet,decision=d,state='research_completed',received_at=f.now,
                response=dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])]))
    pending.ingest([record],f.now);pending.ingest([record],f.now)
    with f.api.connect() as db:assert db.execute('SELECT count(*) FROM ict_pending_reviews').fetchone()[0]==1
    assert pending.state()['recent'][0]['decision']=='KEEP'
    # A changed saved packet/decision cannot supply a new approval.
    record['id']='forged';record['decision']['pending_order_review']['decision']='CANCEL'
    pending.ingest([record],f.now)
    with f.api.connect() as db:assert db.execute('SELECT count(*) FROM ict_pending_reviews').fetchone()[0]==1


def test_late_modify_confirmation_cannot_revive_expired_parent(ready):
    f=ready;d=propose(f);run(f);old=f.t.row(f.order['order_id'])['expiry_ms']
    confirm(f,d['extension_until_ms']);f.now=old/1000;run(f)
    assert f.t.row(f.order['order_id'])['expiry_ms']==old
    assert f.posts[-1][0].endswith('/11/cancel')


def test_cancel_during_extension_confirmation_is_not_lost(ready):
    f=ready;d=propose(f);run(f);confirm(f,d['extension_until_ms'])
    f.now+=1;propose(f,'CANCEL',review_id='mock-cancel');run(f);run(f)
    assert f.posts[-1][0].endswith('/11/cancel')


@pytest.mark.parametrize('failure',['missing','wrong_stop','wrong_quantity','unlinked','working','foreign_parent','wrong_action','parent_modify_unresolved'])
def test_extension_requires_intact_native_bracket(ready,failure):
    f=ready;propose(f)
    if failure=='missing':del f.life[13]
    if failure=='wrong_stop':f.life[13]['version']['stopPrice']-=1
    if failure=='wrong_quantity':f.life[13]['version']['orderQty']=2
    if failure=='unlinked':f.life[13]['order']['ocoId']=1000
    if failure=='working':f.life[13]['order']['ordStatus']='Working'
    if failure=='foreign_parent':f.life[12]['order']['parentId']=999
    if failure=='wrong_action':f.life[13]['order']['action']='Unknown'
    if failure=='parent_modify_unresolved':f.life[11]['commands'].append(dict(id=555,commandType='Modify',commandStatus='Pending'))
    run(f)
    assert not any(p.endswith('/change') for p,b in f.posts)


def test_slow_protection_reads_cannot_use_stale_account(ready):
    f=ready;propose(f);original=f.t.lifecycle
    def slow(oid):
        result=original(oid)
        if oid==13:f.now+=4
        return result
    f.t.lifecycle=slow;run(f)
    assert not any(p.endswith('/change') for p,b in f.posts)
