import io
import json
import urllib.error
from werkzeug.test import Client
from werkzeug.wrappers import Response
from services.tanja.relay import create_app,TARGET

TOKEN='x'*48
class Upstream:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return b'{"ok":true,"status":"stored"}'
class ClientStub:
    def __init__(self,failure=None):self.calls=[];self.failure=failure
    def open(self,request,timeout):
        self.calls.append((request,timeout))
        if self.failure:raise self.failure
        return Upstream()
def client(stub):return Client(create_app({'TANJA_FEED_TOKEN':TOKEN},stub),Response)
def test_relay_forwards_once_and_preserves_receipt():
    stub=ClientStub();c=client(stub)
    for route in ('feed','reference'):
        r=c.post('/'+route+'/'+TOKEN,data=b'{"data":1}',content_type='application/json')
        assert r.status_code==200 and r.json['status']=='stored'
        req,timeout=stub.calls[-1]
        assert req.full_url==TARGET+'/'+route+'/'+TOKEN and req.data==b'{"data":1}' and timeout==8
    assert len(stub.calls)==2

def test_failure_is_not_an_ack_or_retried():
    stub=ClientStub(TimeoutError());r=client(stub).post('/feed/'+TOKEN,data=b'{}')
    assert r.status_code==503 and len(stub.calls)==1

def test_downstream_validation_failure_preserved():
    err=urllib.error.HTTPError(TARGET,409,'conflict',{},io.BytesIO(b'{"error":"conflicting_duplicate"}'))
    stub=ClientStub(err);r=client(stub).post('/feed/'+TOKEN,data=b'{}')
    assert r.status_code==409 and r.json['error']=='conflicting_duplicate'

def test_auth_methods_size_and_no_workers():
    stub=ClientStub();c=client(stub)
    assert c.get('/health').json['mode']=='RELAY_ONLY'
    assert c.post('/feed/bad',data=b'{}').status_code==404
    assert c.get('/feed/'+TOKEN).status_code==405
    assert c.post('/feed/'+TOKEN,data=b'x'*12001).status_code==413
    assert c.post('/orders/'+TOKEN,data=b'{}').status_code==404
    assert not stub.calls
