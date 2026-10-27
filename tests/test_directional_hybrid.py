"""No network / no paid calls. Explicitly simulated AI, including replay inputs."""
import copy
import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import ab_ai_context as ai
import directional_ai_review as gate
import directional_hybrid_context as hybrid
import directional_hybrid_lifecycle as life
import ab_directional_entry_rules as rules
from test_directional_setup_review import service, order


def mocked_response(f,payload):
    raw=f.transport(payload); p=json.loads(payload['input'][1]['content'])
    d=json.loads(raw['output'][0]['content'][0]['text'])
    d['hybrid_assessment']={k:dict(assessment='UNKNOWN: mocked response, not genuine AI',evidence_ids=[],missing=['fixture']) for k in hybrid.FIELDS}
    if 'setup' in p:d['approved_geometry']={k:p['setup'][k] for k in ('entry_price','stop_price','target_price','expiry_ms')}
    raw['output'][0]['content'][0]['text']=json.dumps(d)
    return raw


@pytest.fixture
def h(service,monkeypatch):
    f=service;f.config['AB_AI_TRIGGER']='HYBRID'
    f.support=ai.ContextSupport(f.config,reader=f.source,transport=lambda p:mocked_response(f,p),clock=lambda:f.now)
    monkeypatch.setenv('AB_AI_TRIGGER','HYBRID');monkeypatch.setattr(ai,'INSTANCE',f.support)
    monkeypatch.setattr(ai,'SETUP_COMPLETE_CALLBACK',None,raising=False)
    return f


def candidate(f,direction='LONG',oid='one'):
    o=order(f,oid)
    if direction=='SHORT':
        o.update(direction=direction,stop_price=110,target_price=80)
        p=json.loads(o['payload_json']);p['entry_rules']=rules.evaluate('PDH','SHORT',dict(open=104,high=104,low=96,close=96),o['activation_ms']-60000,o['activation_ms'])
        o['payload_json']=json.dumps(p)
    return o


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_allow_exact_setup_both_sides(h,side):
    o=candidate(h,side);h.support.queue_setup(o);assert h.support.process_one()
    assert gate.check(side,o['order_id'])[0] is None
    with h.support.connect() as db:
        row=db.execute('SELECT * FROM reviews').fetchone()
        assert row['review_kind']=='SETUP' and row['prompt_version']==hybrid.VERSION
        assert row['request'] and row['response_text'] and row['response_received_at']


@pytest.mark.parametrize('case',['WAIT','timeout','expired','geometry_entry','geometry_stop','geometry_target','event','source_failure'])
def test_invalid_reviews_cannot_authorize_entry(h,case):
    def transport(payload):
        if case=='timeout':raise TimeoutError()
        raw=mocked_response(h,payload);d=json.loads(raw['output'][0]['content'][0]['text'])
        if case=='WAIT':d['long_scenario']['permission']='WAIT'
        if case.startswith('geometry_'):d['approved_geometry'][{'geometry_entry':'entry_price','geometry_stop':'stop_price','geometry_target':'target_price'}[case]]+=.25
        if case=='expired':h.now+=121
        if case=='event':
            h.now+=60
            with h.support.connect() as db:
                db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',('opposing',h.now,json.dumps(dict(symbol='MNQ',kind='MSS',direction='DOWN'))))
        if case=='source_failure':h.support.reader=Mock(side_effect=ValueError('unavailable'))
        raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
    h.support.transport=transport;h.support.queue_setup(candidate(h));assert h.support.process_one()
    assert gate.check('LONG','one')[0] is not None
    submit=Mock()
    reason,_=gate.check('LONG','one')
    if reason is None:submit()
    submit.assert_not_called()


@pytest.mark.parametrize('field',['entry_price','stop_price','target_price','expiry_ms'])
def test_changed_order_supersedes_saved_allow(h,field):
    o=candidate(h);h.support.queue_setup(o);h.support.process_one()
    o[field]+=.25 if field!='expiry_ms' else 60000
    assert h.support.queue_setup(o)['state']=='SETUP_CHANGED'
    assert gate.check('LONG','one')[0]=='v3_ai:superseded'


def test_duplicate_and_restart_do_not_spend_again(h,monkeypatch):
    o=candidate(h);h.support.queue_setup(o);h.support.queue_setup(o);h.support.process_one()
    assert len(h.calls)==1
    h.now+=1
    fresh=ai.ContextSupport(h.config,reader=h.source,transport=Mock(side_effect=AssertionError('no retry')),clock=lambda:h.now)
    monkeypatch.setattr(ai,'INSTANCE',fresh)
    assert gate.check('LONG','one')[0]=='v3_ai:hybrid_context_not_refreshed'
    assert fresh.queue_setup(o)['state']=='validated'
    life.refresh(fresh,fresh.packet(h.source(),h.now))
    assert gate.check('LONG','one')[0] is None


def test_restart_outstanding_review_never_retries(h):
    h.support.queue_setup(candidate(h))
    def crash(payload):raise KeyboardInterrupt()
    h.support.transport=crash
    with pytest.raises(KeyboardInterrupt):h.support.process_one()
    fresh=ai.ContextSupport(h.config,reader=h.source,transport=Mock(side_effect=AssertionError('no retry')),clock=lambda:h.now)
    assert not fresh.process_one()
    assert fresh.status=='PENDING_OR_UNKNOWN_NO_RETRY'
    assert fresh.setup_annotation('one','LONG')['state']=='PENDING'


def test_budget_reserves_context_setup_position_and_counts_failures(h):
    for i in range(8):
        h.support.queue_setup(candidate(h,oid=str(i)));h.support.process_one()
    assert len(h.calls)==7
    assert h.support.status=='SETUP_ALLOCATION_EXHAUSTED'
    assert gate.check('LONG','7')[0]=='v3_ai:budget_blocked'
    assert hybrid.budget_reason([dict(review_kind='CONTEXT')]*3,0,'CONTEXT')=='CONTEXT_ALLOCATION_EXHAUSTED'
    assert hybrid.budget_reason([dict(review_kind='SETUP')]*7+[dict(review_kind='CONTEXT')]*3,0,'SETUP')=='SETUP_ALLOCATION_EXHAUSTED'
    assert hybrid.budget_reason([dict(review_kind='LEGACY')]*12,0,'SETUP')=='DAILY_LIMIT_REACHED'
    assert hybrid.budget_reason([],10,'SETUP')=='RESERVED_CAPACITY_PROTECTED'
    assert hybrid.budget_reason([],1,'CONTEXT') is None
    assert hybrid.budget_reason([],3,'CONTEXT')=='RESERVED_CAPACITY_PROTECTED'
    assert hybrid.budget_reason([],0,'POSITION') is None


def test_scheduled_context_reused_only_while_valid(h):
    assert h.support.process_one();assert len(h.calls)==1
    p=h.support.packet(h.source(),h.now)
    assert life.reusable_context(h.support,p)['advisory_only']
    assert not h.support.process_one();assert len(h.calls)==1
    h.support.queue_setup(candidate(h));assert h.support.process_one();assert len(h.calls)==2
    request=json.loads(h.calls[-1]['input'][1]['content']);assert request['cached_context_review']
    h.now+=901;life.refresh(h.support,h.support.packet(h.source(),h.now))
    assert life.reusable_context(h.support,p) is None


def test_old_allow_invalidated_by_later_market_event_without_new_ai(h):
    h.support.queue_setup(candidate(h));h.support.process_one();assert gate.check('LONG','one')[0] is None
    h.now+=60;life.refresh(h.support,h.support.packet(h.source(),h.now))
    with h.support.connect() as db:db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',('bos',h.now,json.dumps(dict(kind='MSS',symbol='MNQ',direction='DOWN'))))
    assert gate.check('LONG','one')[0]=='v3_ai:hybrid_new_structure_event'


def test_two_actual_dib_rejections_are_intentional():
    for category in ('NYLL+DIB','AL+DIB'):
        result=rules.evaluate(category,'SHORT',None,None,None)
        assert result['rejection_reason']=='V3_DIB_DISABLED' and not result['eligible']


def recorded_source():
    with gzip.open(Path(__file__).parent/'fixtures/hybrid_market_20261021.json.gz','rt') as f:return json.load(f)


def test_actual_oct21_context_causal_provenance_and_size(h):
    source=recorded_source();h.now=source['context']['processed_at']
    p=h.support.packet(source,h.now);derived=hybrid.build(p)
    assert derived['as_of']==1792603740
    assert derived['es_nq_alignment']['value']['aligned']
    assert derived['geometry'] is None
    assert len(derived['htf_structure'])==6
    assert derived['liquidity']['value']
    assert derived['displacement_atr']['value']
    for e in derived['structure_events']['value']:
        assert e['available_at']<=p['as_of']
        assert all(p['evidence'][r]['available_at']<=e['available_at'] for r in e['evidence_ids'])
    assert len(ai.encode(dict(p,hybrid_context=derived)).encode())<505000
    future=copy.deepcopy(p);next(iter(future['evidence'].values()))['available_at']=p['as_of']+60
    with pytest.raises(ValueError,match='future'):hybrid.build(future)


def test_future_added_bars_cannot_change_past_result(h):
    p=h.support.packet(h.source(),h.now);before=hybrid.build(p)
    assert before==hybrid.build(copy.deepcopy(p))
    p['evidence']['future']=dict(kind='bar',symbol='MNQ',timeframe=1,time=h.now,available_at=h.now+60,open=100,high=200,low=50,close=150)
    with pytest.raises(ValueError):hybrid.build(p)


def test_existing_setup_configuration_upgrades_without_execution_changes(h):
    config=dict(h.config,AB_AI_TRIGGER='SETUP');config.pop('AB_AI_HYBRID_ENABLED',None)
    before=copy.deepcopy(config)
    s=ai.ContextSupport(config,reader=h.source,transport=lambda p:mocked_response(h,p),clock=lambda:h.now)
    assert s.trigger=='HYBRID' and config==before


@pytest.mark.parametrize('changed',[False,True])
def test_real_final_execution_gate_rechecks_context_after_slow_sources(h,monkeypatch,changed):
    import execution_validation as v
    from test_execution_readiness import evidence
    o=candidate(h);o['contract']='MNQZ2026'
    h.support.queue_setup(o);h.support.process_one();_,review=gate.check('LONG','one')
    _,q,thesis,risk,_,limits,_=evidence();now=int(h.now*1000)
    q['as_of_ms']=now;thesis['checked_ms']=now;risk['checked_ms']=now
    def market(order):
        if changed:
            h.now+=1
            with h.support.connect() as db:db.execute("UPDATE setup_requests SET state='superseded' WHERE order_id='one'")
        return q
    monkeypatch.setattr(v,'policy',lambda:limits)
    monkeypatch.setattr(v,'_SOURCES',dict(risk=lambda o:risk,thesis=lambda o:thesis,market=market))
    reason=v.check(o,review,clock=lambda:h.now)
    assert reason==('v3_ai:superseded' if changed else None)


@pytest.mark.parametrize('direction,permission',[('LONG','ALLOW'),('SHORT','ALLOW'),('LONG','WAIT')])
def test_actual_dispatch_resume_uses_exact_hybrid_gate(h,monkeypatch,direction,permission):
    import continuation_live as dispatch
    from datetime import datetime,timezone
    class ClockDate(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.fromtimestamp(h.now,tz or timezone.utc)
    monkeypatch.setattr(dispatch.dt,'datetime',ClockDate)
    monkeypatch.setenv('DATA_DIR',h.tmp.name)
    monkeypatch.setattr(dispatch,'DB_PATH',Path(h.tmp.name)/'dispatch.sqlite3')
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    monkeypatch.setenv('AB_DIRECTIONAL_LIVE_'+direction,'1')  # isolated unit-test process only
    monkeypatch.setattr(dispatch.ab_v3_live,'record_dispatch',Mock())
    monkeypatch.setattr(dispatch,'_DISPATCHER',Mock(return_value={'state':'SENT','reason':'MOCK_ONLY'}))
    o=candidate(h,direction);dispatch._init_db();assert dispatch._claim(o,ClockDate.now(timezone.utc).isoformat())
    dispatch._finish(o['order_id'],dict(state='WAITING_AI',reason='setup_review_pending'),ClockDate.now(timezone.utc).isoformat())
    def transport(payload):
        raw=mocked_response(h,payload);d=json.loads(raw['output'][0]['content'][0]['text'])
        d['long_scenario']['permission']=permission;d['short_scenario']['permission']=permission
        raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
    h.support.transport=transport;h.support.queue_setup(o);h.support.process_one();dispatch.resume_reviews()
    assert dispatch._DISPATCHER.call_count==(1 if permission=='ALLOW' else 0)
    if permission=='ALLOW':assert dispatch._DISPATCHER.call_args.args[0]['entry_price']==o['entry_price']


def test_geometry_change_during_inflight_response_cannot_restore_approval(h):
    o=candidate(h);h.support.queue_setup(o)
    def transport(payload):
        raw=mocked_response(h,payload)
        assert h.support.queue_setup(dict(o,target_price=130))['state']=='SETUP_CHANGED'
        return raw
    h.support.transport=transport;h.support.process_one()
    assert gate.check('LONG','one')[0]=='v3_ai:superseded'


def test_confirmed_structure_break_uses_bias_before_event():
    # HH/HL confirmed before bar 13; the later close below the low is MSS.
    pairs=[(105,101),(106,102),(110,106),(108,104),(106,102),(105,98),
           (108,104),(112,108),(110,106),(108,104),(107,100),(109,105),
           (109,105),(105,95),(106,97),(107,99)]
    ev={}
    for i,(hi,lo) in enumerate(pairs):
        ev[str(i)]=dict(kind='bar',symbol='MNQ',timeframe=5,time=i*300,
                       available_at=(i+1)*300,open=(hi+lo)/2,high=hi,low=lo,
                       close=96 if i==13 else (hi+lo)/2)
    p=dict(as_of=4800,evidence=ev,packet_id='mechanical_fixture')
    events=hybrid.build(p)['structure_events']['value']
    assert any(e['kind']=='MSS' and e['direction']=='DOWN' and e['price']==100 and e['available_at']==4200 for e in events)
    earlier=dict(p,as_of=3900,evidence={k:v for k,v in ev.items() if v['available_at']<=3900})
    assert not any(e['kind']=='MSS' for e in hybrid.build(earlier)['structure_events']['value'])


def test_source_revision_invalidates_original_allow(h):
    h.support.queue_setup(candidate(h));h.support.process_one()
    p=h.support.packet(h.source(),h.now);p['source_packet_id']='corrected_same_cutoff'
    life.refresh(h.support,p)
    assert gate.check('LONG','one')[0]=='v3_ai:hybrid_data_revised'


def test_budget_counts_all_attempt_statuses_and_never_resets_on_restart(h):
    def timeout(payload):raise TimeoutError()
    h.support.transport=timeout;h.support.queue_setup(candidate(h));h.support.process_one()
    fresh=ai.ContextSupport(h.config,reader=h.source,transport=Mock(),clock=lambda:h.now)
    with fresh.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM reviews')]
    assert len(rows)==1 and rows[0]['status']=='unknown' and rows[0]['review_kind']=='SETUP'
    assert hybrid.budget_reason(rows*12,0,'SETUP')=='DAILY_LIMIT_REACHED'


def test_mnq_fvg_inversion_is_causal_and_separate_from_nq():
    rows=[(101,102,100,101),(103,108,101,107),(106,109,105,108),(104,106,99,100)]
    ev={str(i):dict(kind='bar',symbol='MNQ',timeframe=1,time=i*60,available_at=(i+1)*60,
                   open=o,high=h,low=l,close=c) for i,(o,h,l,c) in enumerate(rows)}
    p=dict(as_of=240,evidence=ev,packet_id='gap_fixture')
    gs=hybrid.build(p)['fvg_ifvg']['value']
    assert any(g['kind']=='IFVG' and g['lower']==102 and g['upper']==105 and g['available_at']==240 for g in gs)
    early=dict(p,as_of=180,evidence={k:v for k,v in ev.items() if v['available_at']<=180})
    assert not any(g['kind']=='IFVG' for g in hybrid.build(early)['fvg_ifvg']['value'])


def test_ifvg_on_m5_is_a_material_context_event():
    p=dict(as_of=1800,packet_id='ifvg_fixture',evidence={
        'nq-gap':dict(kind='IFVG',symbol='NQ',timeframe=5,available_at=1800,
                      lower=100,upper=102,direction='short')})
    events=hybrid.build(p)['structure_events']['value']
    assert len(events)==1 and events[0]['kind']=='IFVG' and events[0]['direction']=='DOWN'
    assert events[0]['available_at']==1800 and events[0]['evidence_ids']==['nq-gap']


def test_observed_liquidity_taking_uses_closed_m1_before_m5_close():
    ev={}
    pairs=[(104,100),(105,101),(110,106),(106,102),(105,101)]
    for i,(hi,lo) in enumerate(pairs):
        ev['m5-'+str(i)]=dict(kind='bar',symbol='MNQ',timeframe=5,time=i*300,available_at=(i+1)*300,open=lo+1,high=hi,low=lo,close=lo+1)
    ev['m1']=dict(kind='bar',symbol='MNQ',timeframe=1,time=1500,available_at=1560,open=109,high=111,low=108,close=109)
    events=hybrid.build(dict(as_of=1560,evidence=ev,packet_id='sweep_fixture'))['structure_events']['value']
    assert any(e['kind']=='OBSERVED_LIQUIDITY_TAKEN' and e['available_at']==1560 and e['price']==110 for e in events)


def test_invalidated_context_review_is_never_reused(h):
    h.support.process_one()
    with h.support.connect() as db:db.execute("UPDATE reviews SET invalidated='[\"long_scenario\"]'")
    assert life.reusable_context(h.support,h.support.packet(h.source(),h.now)) is None


def test_model_invalidation_anchor_applied_before_dispatch_callback(h):
    start=h.now
    def source():
        s=h.source();p=s['context']['packet']
        for e in p['evidence'].values():
            if e['kind']=='bar' and e['symbol']=='MNQ' and e['timeframe']==1:e.update(high=101,low=99)
            if h.now>start and e['kind']=='bar' and e['symbol']=='NQ' and e['timeframe']==1 and e['available_at']==h.now:e.update(low=88,close=89)
        p['packet_id']=ai.digest({k:v for k,v in p.items() if k!='packet_id'});return s
    h.support.reader=source
    def transport(p):
        raw=mocked_response(h,p);h.now+=60;return raw
    h.support.transport=transport;h.support.queue_setup(candidate(h));h.support.process_one()
    assert gate.check('LONG','one')[0]=='v3_ai:invalidated'


def test_oversized_advisory_cache_does_not_truncate_fresh_setup(h,monkeypatch):
    monkeypatch.setattr(life,'reusable_context',lambda s,p:dict(review_id='cached',as_of=h.now,expires_at=h.now+900,
        advisory_only=True,decision={'reason':'x'*505000}))
    h.support.queue_setup(candidate(h));assert h.support.process_one()
    p=json.loads(h.calls[0]['input'][1]['content'])
    assert p['cached_context_review']['decision_omitted']=='SIZE_LIMIT_USE_FRESH_COMPLETE_SETUP_CONTEXT'
    assert p['setup']['entry_price']==100 and p['evidence'] and p['hybrid_context']
    assert len(ai.encode(p).encode())<=505000
