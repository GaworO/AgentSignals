"""Offline regression for the 2026-10-30 PREM packet_mismatch. No API calls."""
import copy
import json
import pytest
import ab_ai_context as ai
import directional_ai_review as gate
from test_ab_routing import ContextTests
from test_directional_setup_review import service
from test_directional_hybrid import h, mocked_response, candidate


def test_schema_is_request_local_without_mutating_shared_strings():
    before=copy.deepcopy(ai.SCHEMA)
    a=ai.bound_response_schema(ai.SCHEMA,'first')
    b=ai.bound_response_schema(ai.SCHEMA,'second')
    assert a['properties']['packet_id']=={'type':'string','enum':['first']}
    assert b['properties']['packet_id']=={'type':'string','enum':['second']}
    assert ai.SCHEMA==before
    assert 'packet_id' in a['required'] and a['additionalProperties'] is False
    # A string alias elsewhere in SCHEMA must not inherit packet_id's enum.
    assert a['properties']['target_2r_3r_pl']==before['properties']['target_2r_3r_pl']
    a['properties']['packet_id']['enum'].append('invalid')
    assert b['properties']['packet_id']['enum']==['second']


@pytest.mark.parametrize('value',[None,'',1,True,[]])
def test_missing_request_identity_fails_closed(value):
    with pytest.raises(ValueError,match='invalid_request_packet_id'):
        ai.bound_response_schema(ai.SCHEMA,value)


def assert_bound(payload):
    packet=json.loads(payload['input'][1]['content'])
    fmt=payload['text']['format']
    assert fmt['strict'] is True
    assert fmt['schema']['properties']['packet_id']=={'type':'string','enum':[packet['packet_id']]}
    return packet


def test_legacy_context_wire_payload_is_bound():
    f=ContextTests();f.setUp()
    try:
        assert f.support.process_one()
        assert_bound(f.calls[0])
    finally:f.doCleanups()


def test_hybrid_context_wire_payload_and_persisted_request(h):
    assert h.support.process_one()
    packet=assert_bound(h.calls[0])
    with h.support.connect() as db:
        row=db.execute('SELECT * FROM reviews').fetchone()
    saved=json.loads(row['request'])
    assert assert_bound(saved)['packet_id']==row['id']==packet['packet_id']
    assert row['review_kind']=='CONTEXT'
    assert len(h.calls)==1


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_exact_setup_payload_preserves_geometry_schema(h,side):
    o=candidate(h,side);h.support.queue_setup(o)
    assert h.support.process_one()
    payload=h.calls[0];packet=assert_bound(payload)
    props=payload['text']['format']['schema']['properties']
    assert 'setup_binding' in props and 'approved_geometry' in props and 'ict_assessment' in props
    assert packet['setup']['order_id']==o['order_id']
    assert gate.check(side,o['order_id'])[0] is None


@pytest.mark.parametrize('setup',[False,True])
def test_wrong_packet_is_still_rejected_persisted_and_not_retried(h,setup):
    wrong='6d3b602a6a364d31c2b9dc31f4ea2cbf3ea2cbf0'
    def bad(payload):
        raw=mocked_response(h,payload)
        d=json.loads(raw['output'][0]['content'][0]['text']);d['packet_id']=wrong
        raw['output'][0]['content'][0]['text']=json.dumps(d)
        return raw
    h.support.transport=bad
    if setup:h.support.queue_setup(candidate(h))
    assert h.support.process_one()
    with h.support.connect() as db:row=db.execute('SELECT * FROM reviews').fetchone()
    assert row['status']=='rejected' and row['error_code']=='packet_mismatch'
    assert json.loads(row['response_text'])['packet_id']==wrong
    assert json.loads(row['decision']) is None
    assert len(h.calls)==1
    h.support.process_one()
    # A separately budgeted session-context job may run after a setup job.
    # It must never retry or repair the rejected packet/setup.
    packets=[json.loads(p['input'][1]['content']) for p in h.calls]
    assert sum(p['packet_id']==row['id'] for p in packets)==1
    if setup:
        assert sum('setup' in p for p in packets)==1
        assert gate.check('LONG','one')[0] is not None
    else:assert len(h.calls)==1
