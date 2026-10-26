"""Existing worker, journal, shared budget and dispatcher integration; no network."""
import copy
import json
import sqlite3
from types import SimpleNamespace
import pandas as pd
import pytest
import continuation_live
import directional_recovery_runtime as runtime
import directional_nofill_recovery as journal
import directional_recovery_plans as plans
import directional_recovery_review as review
from test_directional_recovery_wick import market, T, ref, proof, answer, CONFIG


@pytest.fixture
def rig(tmp_path,monkeypatch,market):
    original,packet,selection=market
    path=tmp_path/'shared.sqlite3'
    def connect():
        db=sqlite3.connect(path);db.row_factory=sqlite3.Row;return db
    now=[T+616000];calls=[];posts=[];events=[]
    import ab_v3_live
    monkeypatch.setattr(ab_v3_live,'record_dispatch',lambda order,result:events.append(('native_dispatch_recorded',result)))
    service=SimpleNamespace(connect=connect,config=dict(CONFIG),clock=lambda:now[0]/1000,
        reader=lambda:copy.deepcopy(packet),packet=lambda source,now:source,
        event=lambda *args,**kw:events.append((args,kw)))
    with connect() as db:
        db.executescript('CREATE TABLE reviews(id TEXT PRIMARY KEY,day TEXT,started REAL,cutoff INTEGER,status TEXT,request TEXT,review_kind TEXT,finished REAL); CREATE TABLE setup_requests(order_id TEXT PRIMARY KEY,state TEXT,expires REAL);')
        journal.init(db);journal.register(db,original,T)
    monkeypatch.setattr(continuation_live,'DB_PATH',tmp_path/'dispatch.sqlite3');continuation_live._init_db()
    source=dict(original,candidate_id='TEST_CANDIDATE',dol_id='FIXED_2R',risk_points=31,
                payload_json='{}',candidate_payload_json='{"brk":2}')
    assert continuation_live._claim(source,'TEST')
    def transport(payload):
        calls.append(payload)
        return dict(status='completed',model='MOCK',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(answer(payload)))])])
    service.transport=transport
    def dispatch(row):
        posts.append(row)
        a=runtime.annotation(service,row['order_id'],row['direction'])
        assert a['state']=='ALLOW' and a['setup']['stop_price']==row['stop_price']
        return dict(state='SENT',quantity=1,reason='MOCK_DISPATCH_NOT_BROKER')
    def run(**overrides):
        kw=dict(proof_reader=lambda oid:dict(proof(now[0]),order_id=oid),packet_builder=lambda s,p,o:p,
                selector=lambda o,p,**kw:[selection],dispatcher=dispatch)
        kw.update(overrides);return runtime.process(service,copy.deepcopy(packet),0,**kw)
    return SimpleNamespace(service=service,connect=connect,now=now,calls=calls,posts=posts,events=events,
                           run=run,original=original,packet=packet,selection=selection)


def test_real_coordinator_selects_once_and_uses_existing_dispatch_ledger(rig):
    f=rig;assert f.run();assert len(f.calls)==len(f.posts)==1
    row=f.posts[0]
    assert row['parent_opportunity_id']==f.original['order_id'] and row['recovery_version']==2
    assert row['expiry_ms']==T+1200000 and row['target_price']==29988.5
    assert f.calls[0]['stream'] is True and f.calls[0]['store'] is False
    f.run();assert len(f.calls)==len(f.posts)==1
    with f.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM nofill_review_results').fetchone()[0]==1
    with continuation_live._connect() as db:
        assert db.execute('SELECT state FROM live_dispatches WHERE order_id=?',(row['order_id'],)).fetchone()[0]=='SENT'


@pytest.mark.parametrize('decision',['WAIT','REJECT','CANCEL'])
def test_nonallow_never_reaches_dispatch(rig,decision):
    f=rig
    def transport(payload):
        f.calls.append(payload)
        return dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(answer(payload,decision)))])])
    f.service.transport=transport
    assert f.run();f.run();assert len(f.calls)==1 and not f.posts


def test_timeout_consumes_one_call_and_never_retries(rig):
    def fail(payload):rig.calls.append(payload);raise TimeoutError()
    rig.service.transport=fail
    assert rig.run();rig.run();assert len(rig.calls)==1 and not rig.posts


@pytest.mark.parametrize('race',['late_fill','expired','unverified'])
def test_response_race_prevents_replacement(rig,race):
    f=rig;original_transport=f.service.transport
    def transport(payload):
        value=original_transport(payload)
        if race=='expired':f.now[0]=T+1200000
        return value
    f.service.transport=transport
    def broker(oid):
        p=proof(f.now[0])
        if f.calls and race=='late_fill':p.update(entry_quantity=1,positions=[dict(netPos=1)])
        if f.calls and race=='unverified':p['verified']=False
        return p
    assert f.run(proof_reader=broker);assert not f.posts


def test_twenty_minute_deadline_reconciles_child_without_third_review(rig):
    f=rig;assert f.run();f.now[0]=T+1200000
    f.run()
    with f.connect() as db:assert db.execute('SELECT state FROM nofill_opportunities').fetchone()[0]=='EXPIRED'
    assert len(f.calls)==len(f.posts)==1


def test_restart_during_review_frees_worker_but_does_not_resend(rig):
    f=rig
    with f.connect() as db:
        db.execute('BEGIN IMMEDIATE');journal.advance(db,f.original['order_id'],proof(f.now[0]),f.now[0])
        result=journal.prepare_review(db,f.original['order_id'],f.packet,[f.selection],0,f.now[0],config=f.service.config)
        assert result['allowed']
    with f.connect() as db:journal.recover_on_start(db,f.now[0]+1)
    assert not f.run();assert not f.calls and not f.posts


def test_disabled_recovery_never_calls_ai_or_broker(rig):
    rig.service.config['AB_DIRECTIONAL_NO_FILL_RECOVERY']='false'
    assert not rig.run(proof_reader=lambda oid:pytest.fail('broker called'))


def test_original_setup_priority_prevents_recovery_call(rig):
    with rig.connect() as db:db.execute('INSERT INTO setup_requests VALUES(?,?,?)',('NEW','queued',(T+700000)/1000))
    assert not rig.run();assert not rig.calls and not rig.posts


def test_scanner_capture_uses_actual_extreme_not_confirmation_bar(market):
    o,p,s=market
    data=sorted(p['evidence'].values(),key=lambda x:x['time'])
    df=pd.DataFrame([dict(ts_event=pd.Timestamp(b['time'],unit='s',tz='UTC'),instrument_id=5,
                         **{k:b[k] for k in ('open','high','low','close')}) for b in data])
    times=[b['time'] for b in data]
    row=dict(origin_bar=times.index((T-120000)//1000),bos_bar=times.index((T-60000)//1000),
        fvg_bar=times.index((T-180000)//1000),hh_bar=999999,kind='FIBO',dir='LONG',instrument_id=5,
        bos_ms=T-60000,entry_ms=T)
    captured=runtime.capture(df,row)
    assert captured['status']=='RECORDED' and captured['origin']['low']==29802.25
    assert captured['extreme']['high']==29837.75
    frozen=runtime.original_record(o,captured)
    assert frozen['recovery_anchors']==o['recovery_anchors']
    row['entry_ms']=T-120000
    assert runtime.capture(df,row)['status']=='UNKNOWN'


def test_quantity_scope_is_evidence_based_not_an_invented_one_contract_cap(monkeypatch):
    import ab_v3_live
    monkeypatch.setenv('EXEC_MAX_QTY','4')
    assert not ab_v3_live.broker_quantity_scope({})
    assert not ab_v3_live.broker_quantity_scope(dict(max_quantity=4))
    assert ab_v3_live.broker_quantity_scope(dict(max_quantity=4,quantity_scope_verified=True,
        quantity_scope_reference='MOCK_BROKER_RECORD',observed_protected_quantity=4))


def test_review_after_90_seconds_is_rejected_before_hard_deadline(rig):
    f=rig;transport=f.service.transport
    def delayed(payload):
        result=transport(payload);f.now[0]+=90001;return result
    f.service.transport=delayed
    assert f.run() and not f.posts
    with f.connect() as db:
        assert db.execute('SELECT error FROM nofill_review_results').fetchone()[0]=='RECOVERY_AI_RESPONSE_TOO_LATE'


def test_final_thesis_rechecks_exact_geometry_and_parent_after_allow(rig,monkeypatch):
    f=rig;assert f.run();row=f.posts[0]
    monkeypatch.setattr(runtime,'market_packet',lambda s,p,o:p)
    monkeypatch.setattr(runtime,'broker_proof',lambda oid:proof(f.now[0]))
    assert runtime.validate_entry(f.service,row)['valid']
    with pytest.raises(ValueError,match='GEOMETRY_CHANGED'):
        runtime.validate_entry(f.service,dict(row,stop_price=row['stop_price']+.25))
    monkeypatch.setattr(runtime,'broker_proof',lambda oid:dict(proof(f.now[0]),entry_quantity=1))
    with pytest.raises(ValueError,match='EXPOSURE_EXISTS'):runtime.validate_entry(f.service,row)
    # An owned pending replacement is reconciled by existing pending/broker logic.
    assert runtime.validate_entry(f.service,row,pending=True)['valid']


def test_new_bos_uses_recorded_confirmed_swing_not_arbitrary_stop(market):
    o,p,s=market
    s.update(kind='NEW_BOS_FVG_RETEST',fvg_candle_ids=[ref(T+i*60000) for i in (-2,-1,0)],
             structure_event_id='NEW',source_policy=plans.SWING_POLICY,source_candle_id=ref(T-300000))
    p['evidence']['NEW']=dict(kind='BOS',symbol='MNQ',contract='MNQZ2026',direction='LONG',
        source='ORIGINAL_DIRECTIONAL_SCANNER',available_at=T//1000+60,
        fvg_candle_ids=s['fvg_candle_ids'],confirmed_swing_candle_id=ref(T-300000),
        structural_source_candle_id=ref(T-300000),displacement_confirmed=True)
    result=plans.build(o,p,s,T+616000,stop_buffer_points=.25)
    assert result['status']=='ELIGIBLE_FOR_AI',result
    plan=result['plan'];plans.verify_plan(plan)
    assert plan['stop_price']==29735.75 and plan['stop_anchor']['candle_id']!=plan['fvg']['candle_ids'][0]
    p['evidence']['NEW']['source']='UNVERIFIED'
    assert plans.build(o,p,s,T+616000,stop_buffer_points=.25)['status']=='UNKNOWN'


def test_existing_setup_worker_invokes_recovery_without_spurious_context_calls(service,monkeypatch):
    f=service;f.support.config['AB_DIRECTIONAL_NO_FILL_RECOVERY']='true';seen=[]
    monkeypatch.setattr(runtime,'process',lambda *args:seen.append(True) or False)
    assert not f.support.process_one();assert seen==[True] and not f.calls


from test_directional_setup_review import service


def test_runtime_packet_uses_actual_saved_bars_and_scanner_events(market,tmp_path,monkeypatch):
    import ab_v3_live,continuation_shadow
    o,p,s=market
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setattr(continuation_shadow,'DB_PATH',tmp_path/'shadow.sqlite3')
    continuation_shadow._init_db()
    ctx=dict(status='RECORDED',instrument_id=5,source='ORIGINAL_DIRECTIONAL_SCANNER',direction='LONG',
             origin=p['evidence'][ref(T-120000)],extreme=p['evidence'][ref(T-60000)],
             fvg=[p['evidence'][x] for x in s['fvg_candle_ids']],bos_ms=T-60000)
    o['recovery_context']=copy.deepcopy(ctx)
    with ab_v3_live.connect() as db:
        for b in p['evidence'].values():
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1',o['contract'],b['time']*1000,json.dumps(b),b['available_at']*1000))
    later=copy.deepcopy(ctx);later.update(bos_ms=T,fvg=[p['evidence'][ref(T+i*60000)] for i in (-2,-1,0)])
    candidate=dict(recovery_context=later,entry_rules=dict(eligible=True))
    with continuation_shadow._connect() as db:
        db.execute("INSERT INTO continuation_candidates(candidate_id,strategy,direction,event_kind,decision_ms,stage,status,eligible,payload_json,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ('C','AB_DIRECTIONAL','LONG','CANONICAL_OUTPUT',T+60000,'ORDER','ORDER',1,json.dumps(candidate),'test','test'))
    service=SimpleNamespace(clock=lambda:(T+616000)/1000)
    result=runtime.market_packet(service,dict(as_of=p['as_of'],evidence={}),o)
    event=result['evidence']['RECOVERY_SCANNER:C']
    assert event['source']=='ORIGINAL_DIRECTIONAL_SCANNER' and event['confirmed_swing_candle_id']==ref(T-120000)
    choices=runtime.selections(o,result)
    assert len(choices)==3 and choices[2]['source_policy']==plans.SWING_POLICY
    assert plans.build(o,result,choices[2],T+616000,stop_buffer_points=.25)['status']=='ELIGIBLE_FOR_AI'
    # Revised source candle cannot silently replace the original frozen evidence.
    o['recovery_context']['origin']['low']-=.25
    with pytest.raises(ValueError,match='SCANNER_REFERENCE_MISSING_OR_CHANGED'):
        runtime.market_packet(service,dict(as_of=p['as_of'],evidence={}),o)


def test_deployed_draft_files_load_but_cannot_authorize_orders(monkeypatch):
    from pathlib import Path
    import execution_validation as gate
    import directional_account_limits as risk
    root=Path(__file__).resolve().parents[1]
    monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(root/'directional_live_policy.json'))
    monkeypatch.setenv('MFFU_RISK_EVIDENCE_FILE',str(root/'mffu_account_evidence.json'))
    monkeypatch.setenv('LIVE_EXECUTION_ENABLED','true')
    monkeypatch.setenv('EXEC_KILL_SWITCH','false')
    monkeypatch.setenv('V3_ENTRY_ARMED','1')
    files=gate.configuration_files()
    assert all(v['loaded'] and len(v['sha256'])==64 for v in files.values())
    assert files['DIRECTIONAL_EXECUTION_POLICY_FILE']['max_quantity']==40
    with pytest.raises(ValueError,match='validated_execution_policy_missing'):gate.policy()
    assert risk.read_static_evidence()['verified'] is False
    proof=risk.read_static_evidence()
    assert proof['max_micro_contracts']==40
    assert proof['max_micro_contracts_source'].startswith('USER_CONFIRMED')
    assert proof['margin_policy']['verified'] is False
    assert proof['reconciliation_anchor']['verified'] is False


def test_parent_proof_owns_reconciliation_lock_before_account_refresh(monkeypatch):
    import threading,builder_crosstrade
    lock=threading.Lock();called=[]
    def poll(**kw):
        assert lock.locked();called.append('poll');return True
    t=SimpleNamespace(poll_lock=lock,sync=SimpleNamespace(poll=poll,snapshot=lambda:dict(positions=[],orders=[])),
        row=lambda oid:dict(state='NO_FILL',error=None,observation_json=json.dumps(dict(observed_at=1,
           entry_quantity=0,entry_status='Canceled',native_active=[],no_fill_verified_since_ms=0)),
           receipt_json=json.dumps(dict(orderId=1,oso1Id=2,oso2Id=3))),
        reconcile_one=lambda row,snapshot:called.append('reconcile'))
    monkeypatch.setattr(builder_crosstrade,'_TRANSPORT',t)
    assert runtime.broker_proof('P')['verified']
    assert called==['poll','reconcile'] and not lock.locked()


def test_guard_rejection_is_terminal_and_recorded_without_retry(rig):
    assert rig.run(dispatcher=lambda row:dict(state='BLOCKED',reason='MOCK_GUARD'))
    with rig.connect() as db:assert db.execute('SELECT state FROM nofill_opportunities').fetchone()[0]=='REJECTED'
    rig.run();assert len(rig.calls)==1 and not rig.posts
    assert any(e[0]=='native_dispatch_recorded' for e in rig.events)
