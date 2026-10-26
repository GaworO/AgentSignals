"""Phase 2 integration verification. Every AI response here is MOCKED."""
import copy
import json
from unittest.mock import Mock

import pytest

import ab_ai_context as ai
import directional_ai_review as gate
import directional_ict_context as ict
import directional_review_observation as observation
import directional_setup_review as setup_review
from test_directional_setup_review import service, order


def test_outside_session_root_cause_is_saved_and_never_spends(service):
    f=service
    f.now=1792529911.0  # Actual historical decision time: Oct 20, 16:58:31 NY.
    # Reproduce the old NY-only policy explicitly; current configured PM_AH
    # is now eligible for request-driven review.
    f.support.config['SKIP_SESSIONS']='ASIA,LO,PREM,NYL,PM_AH'
    row=order(f)
    result=f.support.queue_setup(row)
    assert result==dict(state='OUTSIDE_AB_SESSIONS')
    assert not f.support.process_one()
    assert not f.calls
    with f.support.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM setup_requests').fetchone()[0]==0
        event=db.execute('SELECT * FROM setup_review_events').fetchone()
    assert json.loads(event['detail'])['state']=='OUTSIDE_AB_SESSIONS'


def test_frozen_dol_and_sources_reach_all_ten_ict_topics(service):
    f=service;row=order(f);payload=json.loads(row['payload_json']);at=row['activation_ms']
    payload['v3_snapshot'].update(selected_at_ms=at,
        source_event=dict(trigger_ms=at-60000,level_formed_ms=at-120000,jade_thesis='LONG'),
        frozen_dol=dict(price=115,state='OPEN'),
        boundary_context=dict(fvg_lo=95,fvg_hi=99,setup_ms=at-60000),
        dol_tag=dict(evaluated_at_ms=at,current_dol=dict(pool_price=115,status='OPEN')))
    row['payload_json']=json.dumps(payload)
    f.support.queue_setup(row);assert f.support.process_one()
    packet=json.loads(f.calls[0]['input'][1]['content'])
    assert set(packet['ict_context']['topics'])==set(ict.TOPICS)
    assert packet['setup']['frozen_context']['current_dol']['value']['pool_price']==115
    for topic in packet['ict_context']['topics'].values():
        assert topic['source'] and topic['as_of']<=packet['as_of']
        for ref in topic['references']:
            assert ref['evidence_id'] in packet['evidence']
            evidence=packet['evidence'][ref['evidence_id']]
            assert evidence['available_at']<=packet['as_of']
            assert evidence.get('source') or packet['ict_context']['sources']['market_packet']
    assert packet['ict_context']['topics']['atr_expansion']['status']=='UNKNOWN'
    assert 'UNKNOWN' in packet['ict_context']['topics']['dealing_range']['missing'][0]
    assert 'future_outcome' not in json.dumps(packet)


def test_future_frozen_labels_and_untimestamped_atr_are_not_evidence(service):
    f=service;row=order(f);p=json.loads(row['payload_json']);at=row['activation_ms']
    p['v3_snapshot'].update(selected_at_ms=at+60000,frozen_dol=dict(price=999))
    p['v3_snapshot']['source_event']=dict(trigger_ms=at-60000,level_formed_ms=at+60000)
    row['payload_json']=json.dumps(p)
    desc=setup_review.describe(row,'MNQZ2026')
    assert desc['frozen_context']['frozen_dol']['status']=='UNKNOWN'
    assert desc['frozen_context']['source_event']['status']=='UNKNOWN'
    packet=f.support.packet(f.source(),f.now)
    packet['daily_atr']=dict(atr=123)  # Missing known-at time.
    packet['atr_review_context']=dict(available_at=f.now+60,regime='future')
    enriched=ict.attach(packet,desc,ai.digest(desc))
    assert enriched['daily_atr'] is None and enriched['atr_review_context'] is None


def test_raw_request_response_wait_lifecycle_and_export(service,monkeypatch):
    from flask import Flask
    f=service
    original=f.transport
    def wait(payload):
        r=original(payload);d=json.loads(r['output'][0]['content'][0]['text'])
        for side in ('long_scenario','short_scenario'):
            d[side]['permission']='WAIT';d[side]['reason_pl']='MOCK: missing confirmed dealing range'
        text='  '+json.dumps(d,ensure_ascii=False)+'\n'
        r['output'][0]['content'][0]['text']=text;r['model']='mock-model-version'
        result=ai.RecordedResponse(r);result.response_text=json.dumps(r,ensure_ascii=False,indent=1)
        f.now+=2.5
        return result
    f.support.transport=wait
    f.support.queue_setup(order(f));assert f.support.process_one()
    reason,review=gate.check('LONG','one:native')
    assert reason=='v3_ai:wait' and review['setup']['entry_price']==100
    with f.support.connect() as db:
        r=dict(db.execute('SELECT * FROM reviews').fetchone())
        obs=json.loads(db.execute('SELECT payload FROM setup_review_observations').fetchone()[0])
    assert json.loads(r['request'])==f.calls[0]
    assert r['response_text'].startswith('  ') and r['response_text'].endswith('\n')
    assert r['response_body_format']=='original_http_utf8'
    assert r['response_model']=='mock-model-version'
    assert r['prompt_version']=='directional-ict-setup-v2'
    assert r['finished']-r['started']==2.5
    assert r['response_received_at']-r['requested_at']==2.5
    assert obs['decision']=='BLOCK' and obs['execution_authorized'] is False
    assert 'v3_ai:wait' in obs['blocking_reasons']
    app=Flask(__name__);ai.register(app)
    monkeypatch.setenv('GUARD_TOKEN','test-operator')
    assert app.test_client().get('/ab/ai-context/setup/one').status_code==401
    saved=app.test_client().get('/ab/ai-context/setup/one',headers={'X-Guard-Token':'test-operator'}).get_json()
    assert saved['review']['response_text']==r['response_text']
    assert saved['observation']['decision']=='BLOCK'
    assert saved['events'][-1]['event']=='review_completed'


@pytest.mark.parametrize('change',['order_id','direction','setup_fingerprint','unknown_evidence'])
def test_wrong_ai_binding_or_ict_evidence_is_rejected_and_raw_saved(service,change):
    f=service;original=f.transport
    def broken(payload):
        r=original(payload);d=json.loads(r['output'][0]['content'][0]['text'])
        if change=='unknown_evidence':d['ict_assessment']['htf_narrative']['evidence_ids']=['fake']
        else:d['setup_binding'][change]='SHORT' if change=='direction' else 'other'
        r['output'][0]['content'][0]['text']=json.dumps(d)
        return r
    f.support.transport=broken;f.support.queue_setup(order(f));f.support.process_one()
    assert gate.check('LONG','one')[0]=='v3_ai:unavailable'
    with f.support.connect() as db:r=dict(db.execute('SELECT * FROM reviews').fetchone())
    assert r['status']=='rejected' and r['decision']=='null'
    assert r['response_text'] and r['response_body'] and r['error_code']


def test_source_failure_remains_visible_and_does_not_spend(service):
    f=service;f.support.queue_setup(order(f))
    f.support.reader=Mock(side_effect=ValueError('stale_or_future_context'))
    assert not f.support.process_one()
    with f.support.connect() as db:r=dict(db.execute('SELECT * FROM setup_review_events ORDER BY id DESC').fetchone())
    assert r['event']=='source_blocked' and 'stale_or_future_context' in r['detail']
    assert f.calls==[]


def test_observational_collective_never_authorizes_even_near_limit(service):
    f=service;setup=setup_review.describe(order(f),'MNQZ2026')
    quote=dict(raw=dict(bid=99.75,ask=100,source='collective'),observed_ms=f.now*1000,
               quote=dict(as_of_ms=f.now*1000,execution_eligible=False))
    r=observation.assess(setup,dict(state='ALLOW'),quote,now=f.now,
        config=dict(LIVE_EXECUTION_ENABLED='true',EXEC_KILL_SWITCH='false',V3_ENTRY_ARMED='1',
                    AUTO_SUBMIT='1',MFFU_RISK_EVIDENCE_FILE='fixture',SETUP_GROUP_RT_COST_USD='1.90'))
    assert r['confirmed_executable_price'] is None
    assert r['inside_entry_region'] is None  # no invented policy
    assert r['estimated_net_r']<2
    assert r['decision']=='BLOCK' and not r['execution_authorized']
    assert 'quote_source_suitability_unverified' in r['blocking_reasons']


def test_large_evidence_index_preserves_all_observations_without_metadata_duplication(service):
    f=service;setup=setup_review.describe(order(f),'MNQZ2026')
    packet=f.support.packet(f.source(),f.now)
    for n in range(1500):
        packet['evidence']['NQ:H1:recorded-bar-'+str(n)]=dict(kind='bar',symbol='NQ',timeframe=60,
            available_at=packet['as_of'],open=100,high=105,low=95,close=102)
    result=ict.attach(packet,setup,ai.digest(setup))
    for key,value in packet['evidence'].items():assert result['evidence'][key]==value
    assert len(ai.encode(result).encode())<505000
    assert result['ict_context']['sources']['market_packet']['packet_id']==packet['source_packet_id']


def test_context_build_failure_is_durable_and_does_not_call_ai(service,monkeypatch):
    f=service;f.support.queue_setup(order(f))
    monkeypatch.setattr(ict,'attach',Mock(side_effect=ValueError('input_too_large')))
    assert not f.support.process_one()
    with f.support.connect() as db:
        event=dict(db.execute('SELECT * FROM setup_review_events ORDER BY id DESC').fetchone())
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==0
    assert event['event']=='context_build_blocked'
    assert json.loads(event['detail'])['reason']=='input_too_large'
    assert not f.calls
