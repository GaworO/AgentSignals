import base64
import json
import time
from pathlib import Path
from datetime import datetime
import pytest
from flask import Flask,Response
import account_risk_renewal as core
import ict_risk_renewal_web as web

def ms(s):return int(datetime.fromisoformat(s).replace(tzinfo=core.NY).timestamp()*1000)

@pytest.fixture
def rules():return json.loads(Path('mffu_risk_operator_candidate_20261030.json').read_text())

@pytest.fixture
def ui(tmp_path,monkeypatch):
    app=Flask(__name__)
    auth='Basic '+base64.b64encode(b'tanja:test-only-password').decode()
    from flask import request
    def private(fn):
        def wrapped(*a,**k):
            if request.headers.get('Authorization')!=auth:return Response('Unauthorized',401)
            return fn(*a,**k)
        return wrapped
    service=web.register(app,private,{'DATA_DIR':str(tmp_path),'TANJA_DASHBOARD_PASSWORD':'test-only-password'})
    monkeypatch.setattr(core,'view',lambda:dict(state='EXPIRED',can_prepare=True,rules_hash='a'*64))
    calls=[]
    monkeypatch.setattr(core,'prepare_stage',lambda *args:calls.append('prepare') or dict(equity=48694.75))
    monkeypatch.setattr(core,'install_stage',lambda *args:calls.append('install') or dict(state='APPLIED'))
    class Thread:
        def __init__(self,target,args,**kw):self.target=target;self.args=args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(web.threading,'Thread',Thread)
    client=app.test_client()
    h={'Authorization':auth,'Origin':web.ORIGIN,'Sec-Fetch-Site':'same-origin'}
    r=client.get('/ict-shadow/risk-renewal',headers=h,base_url=web.ORIGIN)
    h['X-Risk-CSRF']=r.json['csrf']
    return client,h,service,calls,r

def post(ui,kind,body):
    client,h,*_=ui
    return client.post('/ict-shadow/risk-renewal/'+kind,headers=h,json=body,base_url=web.ORIGIN)

def prepare(ui):return post(ui,'prepare',dict(confirmed=True,rules_hash='a'*64))

def test_get_is_not_renewal_and_cookie_secure(ui):
    client,h,_,calls,r=ui
    assert calls==[] and r.json['stage']=='NEW'
    cookie=r.headers['Set-Cookie']
    assert all(v in cookie for v in ('Secure','HttpOnly','SameSite=Strict','Path=/'))
    for _ in range(3):client.get('/ict-shadow/risk-renewal',headers=h,base_url=web.ORIGIN)
    assert calls==[]

def test_unauthenticated_post_denied(ui):
    client,h,_,calls,_=ui
    r=client.post('/ict-shadow/risk-renewal/install',json={'confirmed':True},base_url=web.ORIGIN)
    assert r.status_code==401 and calls==[]

@pytest.mark.parametrize('change',[{'Origin':'https://evil.example'},{'Origin':''},{'X-Risk-CSRF':''},{'X-Risk-CSRF':'different'},{'Sec-Fetch-Site':'cross-site'}])
def test_csrf_and_cross_site_denied(ui,change):
    ui[1].update(change)
    assert prepare(ui).status_code==409 and not ui[3]

def test_requires_rule_attestation(ui):
    assert post(ui,'prepare',{'rules_hash':'a'*64}).status_code==409 and not ui[3]

def test_prepare_never_install_and_duplicate_preparation_refused(ui):
    assert prepare(ui).status_code==202 and ui[3]==['prepare']
    assert prepare(ui).status_code==409 and ui[3]==['prepare']

def test_password_and_shared_activation_required(ui):
    prepare(ui)
    assert post(ui,'install',dict(confirmed=True,password='wrong',accept_shared_activation=True)).status_code==409
    assert post(ui,'install',dict(confirmed=True,password='test-only-password')).status_code==409
    assert ui[3]==['prepare']

def test_install_requires_fresh_preparation(ui):
    assert post(ui,'install',dict(confirmed=True,password='test-only-password',accept_shared_activation=True)).status_code==409
    assert not ui[3]

def test_explicit_install_once_and_no_password_persistence(ui):
    prepare(ui)
    data=dict(confirmed=True,password='test-only-password',accept_shared_activation=True)
    assert post(ui,'install',data).status_code==202
    assert post(ui,'install',data).status_code==409
    assert ui[3]==['prepare','install']
    with ui[2].db() as db:rows=[dict(r) for r in db.execute('SELECT * FROM requests')]
    assert 'test-only-password' not in json.dumps(rows)
    assert rows[0]['state']=='APPLIED'

def test_interrupted_install_unknown_blocks_future_work(ui):
    service=ui[2];token=ui[1]['X-Risk-CSRF'];key=service.identity(token)
    with service.db() as db:db.execute("UPDATE requests SET state='INSTALLING',updated=? WHERE id=?",(time.time()-200,key))
    assert service.status(token)['stage']=='UNKNOWN'
    assert prepare(ui).status_code==409 and not ui[3]

def test_expired_confirmation_cannot_install(ui):
    prepare(ui);service=ui[2]
    with service.db() as db:db.execute('UPDATE requests SET expires=?',(time.time()-1,))
    assert post(ui,'install',dict(confirmed=True,password='test-only-password',accept_shared_activation=True)).status_code==409
    assert ui[3]==['prepare']

def test_password_attempt_limit(ui):
    prepare(ui)
    for _ in range(5):post(ui,'install',dict(confirmed=True,password='wrong',accept_shared_activation=True))
    assert post(ui,'install',dict(confirmed=True,password='test-only-password',accept_shared_activation=True)).status_code==409
    assert ui[3]==['prepare']

def test_reset_only_discards_known_inactive_or_finished_attempt(ui):
    service=ui[2];token=ui[1]['X-Risk-CSRF'];key=service.identity(token)
    service.save(key,'ERROR',{'error':'Read failed before install'})
    r=post(ui,'reset',{})
    assert r.status_code==200 and r.json['stage']=='NEW' and r.json['csrf']!=token
    assert not ui[3]

@pytest.mark.parametrize('state',['PREPARING','INSTALLING','UNKNOWN'])
def test_reset_refused_for_uncertain_or_running_attempt(ui,state):
    service=ui[2];service.save(service.identity(ui[1]['X-Risk-CSRF']),state,{})
    assert post(ui,'reset',{}).status_code==409

def test_preparation_global_cooldown_prevents_polling_burst(ui):
    service=ui[2];key=service.identity(ui[1]['X-Risk-CSRF'])
    service.save(key,'ERROR',{'error':'Read failed'})
    assert prepare(ui).status_code==409 and not ui[3]

@pytest.fixture
def stage(tmp_path,monkeypatch,rules):
    at=ms('2026-11-03T09:00:00');monkeypatch.setattr(core,'now_ms',lambda:at)
    risk=tmp_path/'risk.json';risk.write_bytes(core.encoded(rules));policy=tmp_path/'policy.json';policy.write_text('{"approved":true}')
    ctx={'release':'test','policy_hash':core.digest(policy.read_bytes())}
    monkeypatch.setattr(core,'context',lambda:(risk,ctx))
    monkeypatch.setattr(core,'assess',lambda q:{'verified':True,'controls':dict(equity=48694.75,losses_today=0,consecutive_losses=4)})
    monkeypatch.setattr(core,'await_runtime',lambda _:None)
    folder=tmp_path/'draft';folder.mkdir()
    core.prepare_stage(folder,core.digest(risk.read_bytes()))
    return risk,policy,ctx,folder

def test_core_only_metadata_after_explicit_install(stage):
    risk,policy,_,folder=stage;before=json.loads(risk.read_bytes());p=policy.read_bytes()
    assert before['expires_at_ms']<core.now_ms()
    core.install_stage(folder)
    after=core.read(risk)
    for k in ('minimum_balance','max_micro_contracts','reconciliation_anchor','accounting_verification','daily_loss_policy'):
        assert after[k]==before[k]
    assert after['expires_at_ms']==ms('2026-11-03T15:55:00') and policy.read_bytes()==p

def test_core_failed_account_check_preserves_active_files(stage,monkeypatch):
    risk,policy,_,folder=stage;before=(risk.read_bytes(),policy.read_bytes())
    def fail(_):raise ValueError('Unreconciled fees')
    monkeypatch.setattr(core,'assess',fail)
    with pytest.raises(ValueError,match='fees'):core.install_stage(folder)
    assert (risk.read_bytes(),policy.read_bytes())==before

def test_core_changed_context_and_stale_draft_block(stage,monkeypatch):
    risk,_,ctx,folder=stage;before=risk.read_bytes();ctx['policy_hash']='changed'
    with pytest.raises(ValueError,match='Configuration'):core.install_stage(folder)
    assert risk.read_bytes()==before
    monkeypatch.setattr(core,'now_ms',lambda:ms('2026-11-03T09:16:00'))
    with pytest.raises(ValueError,match='expired'):core.install_stage(folder)

def test_core_runtime_failure_restores_risk(stage,monkeypatch):
    risk,policy,_,folder=stage;before=(risk.read_bytes(),policy.read_bytes())
    def fail(_):raise ValueError('Runtime unavailable')
    monkeypatch.setattr(core,'await_runtime',fail)
    with pytest.raises(ValueError,match='not confirmed'):core.install_stage(folder)
    assert (risk.read_bytes(),policy.read_bytes())==before
    with pytest.raises(ValueError,match='already attempted'):core.install_stage(folder)

def test_core_no_candidate_limit_tampering(stage):
    risk,_,_,folder=stage;before=risk.read_bytes();q=core.read(folder/'candidate.json');q['max_micro_contracts']=400
    core.atomic(folder/'candidate.json',core.encoded(q));m=core.read(folder/'prepared.json');m['candidate_sha256']=core.digest(core.encoded(q));core.atomic(folder/'prepared.json',core.encoded(m))
    with pytest.raises(ValueError,match='beyond renewal'):core.install_stage(folder)
    assert risk.read_bytes()==before

@pytest.mark.parametrize('stamp',['2026-11-07T10:00:00','2026-11-03T15:55:00'])
def test_day_window_not_extended(rules,stamp):
    with pytest.raises(ValueError):core.candidate(rules,ms(stamp))
