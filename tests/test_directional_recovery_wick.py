"""Recorded candles, confirmed first-candle rule, mocked AI/account. No API/orders."""
import copy
import json
import sqlite3
from pathlib import Path
import pytest
import directional_recovery_plans as plans
import directional_recovery_review as review
import directional_nofill_recovery as journal
from test_directional_account_sizing import case

T=1792419480000
CONFIG={'AB_DIRECTIONAL_NO_FILL_RECOVERY':'true','OPENAI_MODEL':'mock-model',
        'AB_AI_ENABLED':'true','AB_AI_OWNER':'AB_DIRECTIONAL'}


def ref(t):return f'MNQZ2026:M1:{t//1000}'


@pytest.fixture
def market():
    raw=json.loads((Path(__file__).parent/'fixtures/directional_recovery_oct19.json').read_text())
    evidence={ref(b['time']*1000):dict(b,kind='bar',symbol='MNQ',contract='MNQZ2026',
              timeframe=1,available_at=b['time']+60) for b in raw['bars']}
    packet=dict(as_of=(T+600000)//1000,evidence=evidence,source_packet_id='recorded_test_fixture')
    original=dict(strategy='AB_DIRECTIONAL',order_id='TEST_ORIGINAL',direction='LONG',contract='MNQZ2026',
        entry_price=29816.75,stop_price=29785.75,target_price=29878.75,activation_ms=T,expiry_ms=T+600000,
        recovery_anchors=dict(order_id='TEST_ORIGINAL',origin=dict(candle_id=ref(T-120000),price=29802.25),
            extreme=dict(candle_id=ref(T-60000),price=29837.75)))
    selection=dict(kind='FIBO_50_LIMIT',fvg_candle_ids=[ref(T-i*60000) for i in (5,4,3)],
        source_candle_id=ref(T-5*60000),source_association_reference=plans.SOURCE_POLICY)
    return original,packet,selection


def build(market,**kw):
    return plans.build(*market,T+616000,stop_buffer_points=kw.get('buffer',.25))


def answer(payload,decision='ALLOW'):
    ctx=review.unpack(payload);p=ctx['plans'][0];ids=p['fvg']['candle_ids']
    return dict(request_id=ctx['request_id'],opportunity_id=ctx['opportunity_id'],plan_version=2,
        decision=decision,selected_plan=review.binding(p) if decision=='ALLOW' else None,
        reason_pl='MOCK: test geometry, not actual ICT advice.',confidence=.5,
        ict_assessment={k:dict(assessment='SUPPORTS',evidence_ids=ids,reason_pl='MOCK evidence review.',
            missing_evidence=[]) for k in review.directional_ict_context.TOPICS})


def payload(market):return review.request(market[0],market[1],[build(market)],'mock-model')


def proof(now):
    return dict(order_id='TEST_ORIGINAL',observed_ms=now,verified=True,entry_quantity=0,positions=[],working_orders=[],
        broker_ids=dict(orderId=1,oso1Id=2,oso2Id=3),state='NO_FILL',entry_status='Canceled',no_fill_verified_since_ms=T+600000)


def test_fibo_stop_uses_confirmed_first_candle_not_old_stop_or_gap_edge(market):
    before=copy.deepcopy(market);r=build(market);assert r['status']=='ELIGIBLE_FOR_AI'
    p=r['plan'];assert p['entry_price']==29820
    assert p['stop_price']==29735.75 and p['stop_price']!=market[0]['stop_price']
    assert p['target_price']==29988.5 and p['stop_distance_points']==84.25
    assert p['stop_anchor']['wick_price']==29736 and p['expiry_ms']==T+1200000
    assert p['stop_anchor']['source_policy']==plans.SOURCE_POLICY
    assert market==before


@pytest.mark.parametrize('offset',[4,3])
def test_middle_or_confirmation_candle_cannot_replace_first(market,offset):
    market[2]['source_candle_id']=ref(T-offset*60000)
    assert build(market)['reason']=='FVG_SOURCE_POLICY_MISMATCH'


def test_short_is_exact_mirror_using_high_wick(market):
    original,packet,selection=market;center=60000
    for e in packet['evidence'].values():
        o,h,l,c=[e[k] for k in ('open','high','low','close')]
        e.update(open=center-o,high=center-l,low=center-h,close=center-c)
    original['direction']='SHORT'
    for k in ('entry_price','stop_price','target_price'):original[k]=center-original[k]
    for a in original['recovery_anchors'].values():
        if isinstance(a,dict):a['price']=center-a['price']
    r=build(market);assert r['status']=='ELIGIBLE_FOR_AI';p=r['plan']
    assert p['entry_price']==30180 and p['stop_price']==30264.25 and p['target_price']==30011.5
    assert p['stop_anchor']['wick_field']=='high'


@pytest.mark.parametrize('field',['source_candle_id','source_association_reference'])
def test_mapping_is_derived_from_confirmed_rule_not_required_per_setup(market,field):
    expected=build(market)['plan'];market[2].pop(field)
    assert build(market)['plan']==expected


def test_gap_proof_alone_resolves_first_candle_without_operator_mapping(market):
    market[2].pop('source_candle_id');market[2].pop('source_association_reference')
    p=build(market)['plan'];plans.verify_plan(p)
    assert p['stop_anchor']['candle_id']==ref(T-5*60000)
    assert p['selection']['source_policy']==plans.SOURCE_POLICY
    assert 'FIRST chronological candle' in payload(market)['input'][0]['content']


def test_first_candle_rule_does_not_guess_missing_or_reordered_evidence(market):
    market[2].pop('source_candle_id')
    market[2]['fvg_candle_ids'].reverse()
    assert build(market)['reason']=='FVG_CANDLES_NOT_CONTIGUOUS'
    market[2]['fvg_candle_ids'].reverse()
    market[1]['evidence'].pop(ref(T-5*60000))
    assert build(market)['reason']=='SOURCE_CANDLE_UNKNOWN'


def test_stale_source_policy_and_legacy_saved_plan_are_rejected(market):
    p=build(market)['plan'];p['version']='directional-recovery-wick-v1'
    p['plan_id']=plans.digest({k:v for k,v in p.items() if k not in ('plan_id','order_id')})
    with pytest.raises(ValueError,match='PLAN_CHANGED'):plans.verify_plan(p)
    market[2]['source_policy']='EXPLICIT_ANY_CANDLE'
    assert build(market)['reason']=='FVG_SOURCE_POLICY_MISMATCH'


def test_even_rehashed_plan_cannot_select_another_source(market):
    p=build(market)['plan'];p['stop_anchor']['candle_id']=ref(T-4*60000)
    p['plan_id']=plans.digest({k:v for k,v in p.items() if k not in ('plan_id','order_id')})
    p['order_id']=p['opportunity_id']+':recovery:2:'+p['plan_id'][:16]
    with pytest.raises(ValueError,match='SOURCE_POLICY_MISMATCH'):plans.verify_plan(p)


def test_confirmation_candle_wrong_side_is_blocked_not_fixed(market):
    market[2].update(kind='DEEP_FVG_LIMIT',source_candle_id=ref(T-3*60000))
    assert build(market)['reason']=='FVG_SOURCE_POLICY_MISMATCH'


def test_exact_fvg_half_tick_is_blocked_without_silent_rounding(market):
    market[2]['kind']='DEEP_FVG_LIMIT'
    assert build(market)['reason']=='EXACT_MIDPOINT_OFF_TICK'


def test_deep_gap_uses_its_own_source_not_original_or_other_gap(market):
    original,packet,s=market;s['kind']='DEEP_FVG_LIMIT'
    # Synthetic half-point boundary adjustment makes the midpoint on tick.
    packet['evidence'][s['fvg_candle_ids'][0]]['high']=29780.
    r=build(market);assert r['status']=='ELIGIBLE_FOR_AI';p=r['plan']
    assert p['entry_price']==29792.75 and p['stop_price']==29735.75 and p['target_price']==29906.75


@pytest.mark.parametrize('buffer',[0,-.25,.1,float('nan'),None])
def test_invalid_wick_buffer_never_uses_fixed_stop(market,buffer):
    assert build(market,buffer=buffer)['status']=='UNKNOWN'


@pytest.mark.parametrize('change,reason',[
    ('future','CANDLE_NOT_CLOSED_AT_REVIEW'),('contract','SOURCE_CANDLE_CONTRACT_UNVERIFIED'),
    ('anchors','ORIGINAL_FIBO_ANCHORS_UNKNOWN'),('revision','ORIGINAL_FIBO_ANCHOR_CHANGED'),
    ('gap','FVG_CANDLES_NOT_CONTIGUOUS'),('history','POST_SETUP_HISTORY_INCOMPLETE')])
def test_future_unknown_and_revised_evidence_blocks(market,change,reason):
    original,packet,s=market
    if change=='future':packet['evidence'][s['source_candle_id']]['available_at']=packet['as_of']+1
    elif change=='contract':packet['evidence'][s['source_candle_id']]['contract']='MNQH2027'
    elif change=='anchors':original.pop('recovery_anchors')
    elif change=='revision':original['recovery_anchors']['origin']['price']+=.25
    elif change=='gap':packet['evidence'][s['source_candle_id']]['time']-=60;packet['evidence'][s['source_candle_id']]['available_at']-=60
    else:packet['evidence'].pop(ref(T+60000))
    assert build(market)['reason']==reason


def test_new_bos_requires_fresh_original_scanner_swing_and_displacement(market):
    o,p,s=market;s.update(kind='NEW_BOS_FVG_RETEST',fvg_candle_ids=[ref(T+i*60000) for i in (-2,-1,0)],
                        source_candle_id=ref(T-120000),structure_event_id='TEST_BOS')
    assert build(market)['reason']=='NEW_BOS_DISPLACEMENT_SWING_PROOF_UNKNOWN'
    p['evidence']['TEST_BOS']=dict(kind='BOS',symbol='MNQ',contract='MNQZ2026',direction='LONG',
        source='ORIGINAL_DIRECTIONAL_SCANNER',available_at=T//1000+60,fvg_candle_ids=s['fvg_candle_ids'],
        structural_source_candle_id=s['source_candle_id'],displacement_confirmed=True)
    assert build(market)['status']=='ELIGIBLE_FOR_AI'
    p['evidence']['TEST_BOS']['displacement_confirmed']=False
    assert build(market)['reason']=='NEW_BOS_DISPLACEMENT_SWING_PROOF_UNKNOWN'


@pytest.mark.parametrize('decision',['ALLOW','WAIT','REJECT'])
def test_exact_ai_decision_and_non_allow(market,decision):
    req=payload(market);d=answer(req,decision);selected=review.validate(d,req)
    assert bool(selected)==(decision=='ALLOW')
    if selected:assert review.revalidate(selected,req,market[1],T+620000)==selected


@pytest.mark.parametrize('field,value',[('entry_price',29821),('stop_price',29785.75),('target_price',30000),
    ('direction','SHORT'),('contract','MNQH2027'),('expiry_ms',T+1800000),('plan_id','other'),('order_id','other')])
def test_ai_cannot_change_source_geometry_direction_or_validity(market,field,value):
    req=payload(market);d=answer(req);d['selected_plan'][field]=value
    with pytest.raises(ValueError,match='AI_CHANGED_RECOVERY_GEOMETRY'):review.validate(d,req)


def test_ai_unknown_decisive_invalidation_cannot_allow(market):
    req=payload(market);d=answer(req);d['ict_assessment']['invalidation_geometry'].update(
        assessment='UNKNOWN',missing_evidence=['Source wick not structurally justified'])
    with pytest.raises(ValueError,match='DECISIVE'):review.validate(d,req)


def test_expired_or_revised_source_after_ai_cannot_revive(market):
    req=payload(market);p=review.validate(answer(req),req)
    with pytest.raises(ValueError,match='EXPIRED'):review.revalidate(p,req,market[1],T+1200000)
    market[1]['evidence'][p['stop_anchor']['candle_id']]['low']-=.25
    with pytest.raises(ValueError,match='REVISED'):review.revalidate(p,req,market[1],T+620000)


def test_model_input_excludes_order_ledger_and_credentials(market):
    market[0].update(api_key='DO_NOT_SEND',estimated_fill=True,future_result='WIN')
    req=payload(market);assert 'DO_NOT_SEND' not in json.dumps(req) and 'future_result' not in json.dumps(req)


def test_future_htf_context_is_rejected_even_when_price_geometry_is_causal(market):
    p=market[1];e=copy.deepcopy(next(iter(p['evidence'].values())))
    e.update(symbol='ES',contract='ESZ2026',time=p['as_of'],available_at=p['as_of']+60)
    p['evidence']['FUTURE_ES']=e
    with pytest.raises(ValueError,match='future'):payload(market)


@pytest.mark.parametrize('direction,blocked',[('DOWN',True),('UP',False)])
def test_new_structure_after_ai_checks_direction(market,monkeypatch,direction,blocked):
    import directional_hybrid_context
    req=payload(market);p=review.validate(answer(req),req)
    monkeypatch.setattr(directional_hybrid_context,'build',lambda packet:dict(structure_events=dict(
        value=[dict(available_at=packet['as_of']+1,symbol='MNQ',kind='MSS',direction=direction)])) )
    if blocked:
        with pytest.raises(ValueError,match='NEW_STRUCTURE'):review.revalidate(p,req,market[1],T+620000)
    else:
        assert review.revalidate(p,req,market[1],T+620000)==p


def test_saved_plan_mutation_is_detected_before_risk_sizing(market):
    p=build(market)['plan'];p['stop_price']+=1
    with pytest.raises(ValueError,match='PLAN_CHANGED'):plans.size(p,{},'test',T+620000,3.24,250)


def test_recovery_does_not_silently_use_legacy_fixed_risk(market,monkeypatch):
    monkeypatch.delenv('AB_DIRECTIONAL_RISK_MODE',raising=False)
    assert plans.size(build(market)['plan'],{},'test',T+620000,3.24,250)['reason']=='RECOVERY_05_PERCENT_NOT_CONFIGURED'


@pytest.mark.parametrize('wick_low,quantity',[(29736,1),(29763,2)])
def test_real_risk_engine_recalculates_05_percent_from_new_wick(case,market,monkeypatch,wick_low,quantity):
    args,proof_data,save=case
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_MODE','EQUITY_PERCENT');monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT','0.5')
    monkeypatch.setenv('EXEC_MAX_QTY','40');proof_data['max_micro_contracts']=40
    # Second case is synthetic OHLC, not a different interpretation of the real FVG.
    candle=market[1]['evidence'][market[2]['fvg_candle_ids'][0]]
    candle['low']=wick_low
    candle['open']=max(candle['open'],wick_low);candle['close']=max(candle['close'],wick_low)
    p=build(market)['plan']
    # Shift only the fixture clock so both independent risk and review windows are current.
    now=T+620000;delta=now-args[3]
    args[0]['read_started_at']+=delta/1000
    proof_data['observed_at_ms']+=delta;proof_data['expires_at_ms']+=delta
    proof_data['account_controls']['observed_at_ms']+=delta
    from datetime import datetime
    from zoneinfo import ZoneInfo
    proof_data['account_controls']['ny_day']=datetime.fromtimestamp(now/1000,ZoneInfo('America/New_York')).date().isoformat()
    save();r=plans.size(p,args[0],args[1],now,3.24,250)
    assert r['allowed'],r
    assert r['max_quantity']==quantity and r['strategy_budget_usd']==243.60
    assert r['risk_per_contract_usd']==p['stop_risk_usd_per_contract']+3.24
    proof_data['account_controls']['losses_today']=1;save()
    assert plans.size(p,args[0],args[1],now,3.24,250)['reason']=='account_wide_one_loss_stop'


@pytest.mark.parametrize('outcome',['ALLOW','STREAM_ALLOW','WAIT','REJECT','TIMEOUT','LATE_FILL','EXPIRED'])
def test_durable_journal_actual_request_response_and_one_review(tmp_path,market,outcome):
    path=tmp_path/'ai.sqlite3';now=T+616000
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE reviews(id TEXT PRIMARY KEY,day TEXT,started REAL,cutoff INTEGER,status TEXT,request TEXT,review_kind TEXT,finished REAL)')
        db.execute('CREATE TABLE setup_requests(order_id TEXT PRIMARY KEY,state TEXT,expires REAL)')
        journal.init(db);journal.register(db,market[0],T);journal.advance(db,'TEST_ORIGINAL',proof(now),now)
        r=journal.prepare_review(db,'TEST_ORIGINAL',market[1],[market[2]],0,now,
            config=CONFIG)
        assert r['allowed'];db.commit();db.execute('BEGIN IMMEDIATE')
        d=answer(r['request'],outcome if outcome in ('ALLOW','WAIT','REJECT') else 'ALLOW')
        raw=dict(status='completed',model='mock-model',usage={'total_tokens':1},output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])
        end=T+1200000 if outcome=='EXPIRED' else now+1000;bp=proof(end)
        if outcome=='LATE_FILL':bp.update(entry_quantity=1,positions=[{'netPos':1}])
        body=None if outcome=='TIMEOUT' else json.dumps(raw)
        if outcome=='STREAM_ALLOW':
            from directional_ai_transport import Response
            body=Response(raw);body.response_text='event: response.completed\ndata: '+json.dumps(raw)+'\n\n'
            body.response_body_format='original_sse_events_utf8'
        result=journal.complete_review(db,'TEST_ORIGINAL',r['review_id'],body,market[1],bp,end)
        expected={'ALLOW':'SELECTED','STREAM_ALLOW':'SELECTED','WAIT':'WAIT','REJECT':'REJECT','TIMEOUT':'REVIEW_UNKNOWN','LATE_FILL':'EXPOSURE','EXPIRED':'EXPIRED'}[outcome]
        assert result['state']==expected,result
        assert result['execution_authorized'] is False
        saved=db.execute('SELECT * FROM nofill_review_results').fetchone()
        assert saved['response_body']==(body.response_text if outcome=='STREAM_ALLOW' else body or 'null')
        assert saved['latency_ms']==end-now
        if outcome=='STREAM_ALLOW':assert saved['body_format']=='original_sse_events_utf8'
        db.commit()
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        assert not journal.prepare_review(db,'TEST_ORIGINAL',market[1],[market[2]],0,end,
            config=CONFIG)['allowed']
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==1
        with pytest.raises(ValueError):journal.complete_review(db,'TEST_ORIGINAL',r['review_id'],body,market[1],bp,end)
