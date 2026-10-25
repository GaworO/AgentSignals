"""Offline SSE fixtures only; no paid requests or broker actions."""
import io
import json
from unittest.mock import Mock
import pytest
import ab_ai_context as ai
import directional_ai_transport as transport
from test_directional_hybrid import h, candidate, mocked_response
from test_directional_setup_review import service


def event(kind, response=None):
    return ('data: '+json.dumps(dict(type=kind,response=response or {}))+'\n\n').encode()


def fake(stream, clock, delay=0):
    class Socket:
        def settimeout(self,value):assert value>0
    class Reply(io.BytesIO):
        status=200
        def getheader(self,*args):return 'text/event-stream'
        def readline(self,*args):
            clock[0]+=delay
            return super().readline(*args)
    class Connection:
        def __init__(self,*args,**kw):self.sock=Socket();self.closed=False
        def request(self,*args):pass
        def getresponse(self):return Reply(stream)
        def close(self):self.closed=True
    return Connection


def test_stream_can_complete_after_old_sixty_second_timeout():
    clock=[0.]
    stream=event('response.created',{'id':'resp_one'})+event('response.completed',{'id':'resp_one','status':'completed','output':[]})
    r=transport.request({'stream':True,'store':False},'FAKE',90,
        connection=fake(stream,clock,17),clock=lambda:clock[0])
    assert clock[0]==68 and r['status']=='completed'
    assert r.response_body_format=='original_sse_events_utf8'
    assert r.response_text.encode()==stream and 'FAKE' not in r.response_text


@pytest.mark.parametrize('case',['deadline','disconnect','different_id','delta_only'])
def test_partial_or_late_stream_never_becomes_approval(case):
    clock=[0.];stream=event('response.created',{'id':'resp_one'})
    if case=='different_id':stream+=event('response.completed',{'id':'resp_other','status':'completed'})
    if case=='delta_only':stream+=b'data: {"type":"response.output_text.delta","delta":"ALLOW"}\n\n'
    if case=='deadline':stream+=event('response.completed',{'id':'resp_one','status':'completed'})
    with pytest.raises((ValueError,TimeoutError)):
        transport.request({'stream':True,'store':False},'FAKE',60,
            connection=fake(stream,clock,17 if case=='deadline' else 0),clock=lambda:clock[0])


def test_call_deadline_is_bounded_by_review_expiry(h,monkeypatch):
    fn=Mock(return_value={'status':'completed'})
    monkeypatch.setattr(transport,'request',fn)
    payload={'stream':True,'input':[{},dict(content=json.dumps({'review_expires_at':h.now+7}))]}
    h.support.call_api(payload);assert fn.call_args.args[2]==7
    payload['input'][1]['content']=json.dumps({'review_expires_at':h.now})
    with pytest.raises(ValueError,match='expired'):h.support.call_api(payload)
    assert fn.call_count==1


def test_unknown_hybrid_attempt_consumes_capacity_but_does_not_starve_other_setup(h):
    h.support.transport=Mock(side_effect=TimeoutError)
    h.support.queue_setup(candidate(h,oid='timeout'));h.support.process_one()
    assert h.support.setup_annotation('timeout','LONG')['state']!='ALLOW'
    h.support.transport=lambda p:mocked_response(h,p)
    h.support.queue_setup(candidate(h,oid='different'));assert h.support.process_one()
    assert h.support.setup_annotation('different','LONG')['state']=='ALLOW'
    assert h.support.queue_setup(candidate(h,oid='timeout'))['state']=='unknown'
    with h.support.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM reviews')]
    assert len(rows)==2 and {r['status'] for r in rows}=={'unknown','validated'}
    assert all(r['review_kind']=='SETUP' for r in rows)
    assert json.loads(rows[-1]['request'])['stream'] is True


def test_reject_has_reason_and_cannot_authorize(h):
    def deny(payload):
        raw=mocked_response(h,payload);d=json.loads(raw['output'][0]['content'][0]['text'])
        d['long_scenario']['permission']='REJECT'
        raw['output'][0]['content'][0]['text']=json.dumps(d);return raw
    h.support.transport=deny;h.support.queue_setup(candidate(h));h.support.process_one()
    result=h.support.setup_annotation('one','LONG')
    assert result['state']=='REJECT' and result['scenario']['reason_pl']


def test_seven_unknown_setup_attempts_exhaust_reservation_without_eighth_call(h):
    failed=Mock(side_effect=TimeoutError)
    h.support.transport=failed
    for n in range(8):
        h.support.queue_setup(candidate(h,oid='failure-'+str(n)))
        h.support.process_one()
    assert failed.call_count==7
    assert h.support.status=='SETUP_ALLOCATION_EXHAUSTED'
    with h.support.connect() as db:
        assert db.execute("SELECT count(*) FROM reviews WHERE status='unknown'").fetchone()[0]==7
    assert h.support.setup_annotation('failure-7','LONG')['state']=='BUDGET_BLOCKED'
