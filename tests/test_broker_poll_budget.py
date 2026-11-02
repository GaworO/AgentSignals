"""Synthetic reads only: call reduction must never reuse stale entry evidence."""
import copy
import io
import json
import fcntl
from urllib.error import HTTPError
from unittest.mock import Mock
import pytest
import builder_broker_sync as b
import broker_read_control as control
import directional_account_reconciliation as ar
import directional_account_limits as limits
from test_account_risk_adapter import fixture

@pytest.fixture
def setup(tmp_path,monkeypatch):
    snap,rows,margin,proof,_,contract,now=fixture()
    clock=[now/1000]
    cfg=dict(DATA_DIR=str(tmp_path),ACCOUNT_PLAN='builder50',BUILDER_BROKER_SYNC_ENABLED='1',
        BUILDER_BROKER_ACCOUNT_VERIFIED='1',BUILDER_BROKER_ACCOUNT_ID='1',
        BUILDER_BROKER_ACCOUNT_NAME='Eval',BUILDER_BROKER_ENVIRONMENT='demo',
        BUILDER_BROKER_API_TOKEN='not-a-real-token',AB_V3_CONTRACT=contract)
    acct=dict(snap['account'],accountId=1,name='Eval',environment='demo',active=True,archived=False)
    values={'/accounts':[acct],'/accounts/Eval':acct,'/accounts/Eval/positions':[],
            '/accounts/Eval/orders':[],'/accounts/Eval/fills':snap['fills'],
            '/fills/history?account=Eval&environment=demo&limit=1000':rows,
            '/marginsnapshots':[dict(environment='demo',data=[dict(margin,id=1)])]}
    calls=[]
    def read(p):calls.append(p);return dict(success=True,data=copy.deepcopy(values[p]))
    sync=b.AccountSync(cfg,reader=read,clock=lambda:clock[0])
    proof['account_binding']=sync.binding;proof['reconciliation_anchor']['account_binding']=sync.binding
    proof['expires_at_ms']=now+172800000;proof['margin_policy']['expires_at_ms']=now+172800000
    monkeypatch.setattr(limits,'read_static_evidence',lambda:copy.deepcopy(proof))
    return sync,clock,calls,values,proof


def test_idle_reads_reduce_seven_to_four_but_entry_forces_six(setup):
    s,t,c,v,p=setup
    assert s.poll() and ar.state(s)['verified'];assert len(c)==7
    c.clear();t[0]+=15
    assert s.poll() and ar.state(s)['verified'];assert len(c)==4
    assert ar.state(s)['history_cache_reused']
    c.clear();t[0]+=1
    assert s.poll(wait=True) and ar.state(s)['verified'];assert len(c)==6
    assert not ar.state(s)['history_cache_reused']

@pytest.mark.parametrize('what',['balance','fill','order','position','proof','ttl','day'])
def test_changes_invalidate_history_cache(setup,what):
    s,t,c,v,p=setup;assert s.poll();c.clear();t[0]+=15
    if what=='balance':v['/accounts/Eval']['balance']['netLiq']-=1
    if what=='fill':v['/accounts/Eval/fills'].append(dict(id=3,orderId=12,accountId=1))
    if what=='order':v['/accounts/Eval/orders']=[dict(id=12,accountId=1)]
    if what=='position':v['/accounts/Eval/positions']=[dict(accountId=1,netPos=1)]
    if what=='proof':p['source_reference']='changed'
    if what=='ttl':t[0]+=300
    if what=='day':t[0]+=86400
    s.poll()
    assert any(x.startswith('/fills/history?') for x in c)
    if what in ('fill','order','position'):assert not ar.state(s)['verified']
    if what=='day':assert '/accounts' in c


def test_late_fee_revision_blocks_entry_even_with_unchanged_cash(setup):
    s,t,c,v,p=setup;assert s.poll();c.clear();t[0]+=15
    v['/fills/history?account=Eval&environment=demo&limit=1000'][0]['fees']+=1
    assert s.poll(wait=True)
    assert not ar.state(s)['verified']
    assert 'cash_fills_fees_or_transfer_unreconciled' in ar.state(s)['missing']


def test_margin_based_accounts_always_fetch_margin(setup):
    s,t,c,v,p=setup;p['margin_policy'].update(mode='FIXED_PER_CONTRACT',usd_per_contract=100)
    assert s.poll();c.clear();t[0]+=15
    assert s.poll();assert len(c)==5 and '/marginsnapshots' in c


def test_restart_does_not_trust_memory_cache(setup):
    s,t,c,v,p=setup;assert s.poll();c.clear()
    other=b.AccountSync(s.config,reader=s.reader,clock=s.clock)
    assert other.poll();assert len(c)==7


def test_process_lock_prevents_duplicate_cycle_and_recovers(setup):
    s,t,c,v,p=setup
    with open(str(s.path)+'.'+s.binding+'.poll.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert not s.poll();assert not c
    assert s.poll();assert len(c)==7

@pytest.mark.parametrize('name,reason,delay',[('snapshot_refresh_pending','BALANCE_REFRESH_PENDING',1),
    ('rate_limited','CROSSTRADE_RATE_LIMITED',2),('broker_rate_limited','TRADOVATE_RATE_LIMITED',60),
    ('private unexpected message','HTTP_429_UNKNOWN',60)])
def test_429_reason_redaction_count_and_restart_cooldown(setup,monkeypatch,name,reason,delay):
    s,t,c,v,p=setup
    exc=HTTPError('https://example.invalid',429,'Too many',{},io.BytesIO(json.dumps({'error':name}).encode()))
    opener=Mock();opener.open.side_effect=exc;monkeypatch.setattr(b,'build_opener',lambda *a:opener)
    with pytest.raises(b.SyncError,match=reason):s._read('/accounts')
    st=control.status(s);assert st['remaining_seconds']==delay
    assert st['routes']==[dict(route='/accounts',attempts=1,failures=1,throttled=1)]
    assert 'private unexpected' not in json.dumps(st)
    other=b.AccountSync(s.config,reader=s.reader,clock=s.clock)
    assert not other.poll();assert c==[]
    assert not other.state()['current']
    t[0]+=delay+1;assert other.poll();assert len(c)==7


def test_provider_retry_after_respected_and_lesser_error_does_not_shorten(setup):
    s,t,c,v,p=setup
    def err(seconds):return HTTPError('url',429,'x',{'Retry-After':str(seconds)},io.BytesIO(b'{"error":"broker_rate_limited"}'))
    control.http_error(s,'/accounts',err(3600))
    control.http_error(s,'/accounts',err(1))
    assert control.status(s)['remaining_seconds']==3600
    assert not s.poll();assert not c


def test_auth_failure_requires_fresh_identity_next_time(setup,monkeypatch):
    s,t,c,v,p=setup;assert s.poll();c.clear()
    old=s.reader;s.reader=Mock(side_effect=b.SyncError('TRADOVATE_SESSION_EXPIRED_RECONNECT_REQUIRED'))
    assert not s.poll();assert s.identity_day is None
    s.reader=old;assert s.poll();assert len(c)==7


def test_expired_rules_cannot_be_extended_by_cache(setup):
    s,t,c,v,p=setup;assert s.poll()
    t[0]=p['expires_at_ms']/1000+1
    assert s.poll()
    assert not ar.state(s)['verified'] and s.reconciliation_cache is None


def test_simultaneous_entry_checks_share_one_complete_fresh_cycle(setup):
    import threading
    s,t,c,v,p=setup
    entered=threading.Event();release=threading.Event();queued=threading.Event()
    read=s.reader
    def slow(path):
        if path=='/accounts':entered.set();assert release.wait(3)
        return read(path)
    s.reader=slow;out=[]
    first=threading.Thread(target=lambda:out.append(s.poll(wait=True)))
    first.start();assert entered.wait(3)
    # Instrument lock acquisition so release occurs only after the second
    # caller has observed contention, without relying on scheduler sleeps.
    lock=s.poll_lock
    class ObservedLock:
        def locked(self):queued.set();return lock.locked()
        def acquire(self,**kw):return lock.acquire(**kw)
        def release(self):return lock.release()
    s.poll_lock=ObservedLock()
    second=threading.Thread(target=lambda:out.append(s.poll(wait=True)))
    second.start();assert queued.wait(3);release.set()
    first.join(3);second.join(3)
    assert out==[True,True] and len(c)==7


def test_native_get_obeys_shared_backoff_but_protective_post_is_not_blocked(setup,monkeypatch):
    import builder_crosstrade as native
    from types import SimpleNamespace
    s,t,c,v,p=setup
    control.http_error(s,'/accounts',HTTPError('url',429,'x',{'Retry-After':'60'},io.BytesIO(b'{"error":"broker_rate_limited"}')))
    transport=object.__new__(native.Transport);transport.sync=s;transport.prefix='/accounts/Eval'
    monkeypatch.setattr(native.execution_safety,'writes_enabled',lambda:True)
    monkeypatch.setattr(native,'selected',lambda:True)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return b'{"success":true}'
    opener=Mock();opener.open.return_value=Response()
    monkeypatch.setattr(native,'build_opener',lambda *a:opener)
    with pytest.raises(native.TransportError,match='BACKOFF'):transport._request('GET','/orders')
    opener.open.assert_not_called()
    assert transport._request('POST','/accounts/Eval/orders/123/cancel',{})['success']
    assert opener.open.call_count==1


def test_read_pacing_shared_across_instances_leaves_write_capacity(setup,monkeypatch):
    s,t,c,v,p=setup;sleeps=[]
    def sleep(seconds):sleeps.append(seconds);t[0]+=seconds
    monkeypatch.setattr(control.time,'sleep',sleep)
    other=b.AccountSync(s.config,reader=s.reader,clock=s.clock)
    control.before(s,'/accounts')
    control.before(other,'/marginsnapshots')
    control.before(s,'/accounts/Eval/positions')
    assert sleeps==[.5,.5]


def test_rate_limit_does_not_refetch_known_identity(setup):
    s,t,c,v,p=setup;assert s.poll();day=s.identity_day;c.clear()
    old=s.reader;s.reader=Mock(side_effect=b.SyncError('BROKER_HTTP_429_TRADOVATE_RATE_LIMITED'))
    assert not s.poll();assert s.identity_day==day
    s.reader=old;assert s.poll();assert '/accounts' not in c


def test_budget_calculation_uses_current_snapshot_without_polling(setup,monkeypatch):
    import ast,os
    from pathlib import Path
    s,t,c,v,p=setup;assert s.poll()
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_MODE','EQUITY_PERCENT')
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT','0.5')
    monkeypatch.setattr(b,'instance',lambda:s)
    node=next(n for n in ast.parse(Path('agent.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='_directional_live_budget')
    ns={'os':os,'builder_broker_sync':b};exec(compile(ast.Module(body=[node],type_ignores=[]),'agent.py','exec'),ns)
    s.poll=Mock(side_effect=AssertionError('budget must not poll'))
    assert ns['_directional_live_budget']({})==4.89
    s.poll.assert_not_called()
    t[0]+=31;assert ns['_directional_live_budget']({})==0
