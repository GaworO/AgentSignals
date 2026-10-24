import io
import json
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

import builder_broker_sync as broker


@pytest.mark.parametrize('body,expected',[
    (json.dumps({'error':'tradovate_session_expired','detail':'PRIVATE TOKEN'}),
     'TRADOVATE_SESSION_EXPIRED_RECONNECT_REQUIRED'),
    ('{"error":"other","detail":"PRIVATE TOKEN"}','BROKER_HTTP_403'),
    ('not JSON PRIVATE TOKEN','BROKER_HTTP_403'),
    ('[]','BROKER_HTTP_403'),
])
def test_only_allowlisted_reconnect_reason_is_exposed(monkeypatch,body,expected):
    sync=broker.AccountSync(dict(BUILDER_BROKER_ACCOUNT_NAME='test'))
    error=HTTPError('https://app.crosstrade.io',403,'Forbidden',{},io.BytesIO(body.encode()))
    opener=Mock();opener.open.side_effect=error
    monkeypatch.setattr(broker,'build_opener',lambda *_:opener)
    with pytest.raises(broker.SyncError) as caught:sync._read('/accounts/test')
    assert str(caught.value)==expected
    assert 'PRIVATE' not in str(caught.value)


def test_expired_session_never_reuses_cached_snapshot_as_current(tmp_path):
    config=dict(DATA_DIR=str(tmp_path),ACCOUNT_PLAN='builder50',BUILDER_BROKER_SYNC_ENABLED='1',
                BUILDER_BROKER_ACCOUNT_VERIFIED='1',BUILDER_BROKER_ACCOUNT_ID='1',
                BUILDER_BROKER_ACCOUNT_NAME='test',BUILDER_BROKER_ENVIRONMENT='demo',
                BUILDER_BROKER_API_TOKEN='unit-token')
    account=dict(accountId=1,name='test',environment='demo',active=True,archived=False,
                 balance=dict(netLiq=50000,totalCashValue=50000))
    def reader(path):
        return dict(success=True,data=[account] if path=='/accounts' else account if path=='/accounts/test' else [])
    sync=broker.AccountSync(config,reader=reader,clock=lambda:1000)
    assert sync.poll()
    sync.reader=Mock(side_effect=broker.SyncError('TRADOVATE_SESSION_EXPIRED_RECONNECT_REQUIRED'))
    assert not sync.poll()
    assert not sync.state()['current']
    assert sync.state()['error']=='TRADOVATE_SESSION_EXPIRED_RECONNECT_REQUIRED'
    assert sync.entry_blocker()=='broker:account_snapshot_not_current'
