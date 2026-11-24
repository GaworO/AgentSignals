"""Synthetic receipt-order controls for the 2026-11-24 bridge incident."""
import json
import sqlite3
from copy import deepcopy
import pytest
from services.tanja.ict_agent.bridge import observe_once,read_tanja
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.scenarios import synchronization_missing,decide
from services.tanja.ict_agent.tests.test_observer import FakeKnowledge,bar

NOW=1795546800


def source(path,symbol,start,received=None):
    b=bar(start,symbol)
    p=dict(symbol=symbol,ticker=b['contract'],confirmed=True,standard=True,
           bar_open_ms=start*1000,bar_close_ms=(start+60)*1000,
           **{k:b[k] for k in ('open','high','low','close')})
    with sqlite3.connect(path) as db:
        for table in ('bars','collection_bars'):
            db.execute('CREATE TABLE IF NOT EXISTS '+table+'(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
        table='collection_bars' if symbol=='NQ' else 'bars'
        db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?)',(symbol,start,start+60,received or start+61,json.dumps(p)))


def observer(tmp_path):
    store=Store(tmp_path/'observe.db',{'test':'INDEPENDENT_SYNTHETIC_RECEIPTS'})
    return store,Observer(store,FakeKnowledge())


def memory(store):
    with store.readonly() as db:return json.loads(db.execute('SELECT payload FROM memory WHERE id=1').fetchone()[0])


def test_partial_minute_is_ingested_and_peer_later_in_same_minute_is_not_lost(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    for sym in ('NQ','ES','MNQ'):source(path,sym,NOW-120)
    assert observe_once(ob,path,NOW-58)['processed']==1
    source(path,'NQ',NOW-60,received=NOW+1)
    assert observe_once(ob,path,NOW+1)['processed']==1
    assert st.snapshot()['context']['NQ']['latest']['ts']==NOW-60
    assert 'SYNCHRONIZED_ES' in st.snapshot()['decision']['data_missing']
    source(path,'ES',NOW-60,received=NOW+2)
    assert observe_once(Observer(st,FakeKnowledge()),path,NOW+2)['processed']==1
    snap=st.snapshot();ctx=snap['context']
    assert 'SYNCHRONIZED_ES' not in snap['decision']['data_missing']
    assert synchronization_missing(ctx,('NQ','ES','MNQ'))==['SYNCHRONIZED_MNQ']
    # Synthetic declaration of a model requiring all three: no claim that ICT
    # requires MNQ for every setup. NQ+ES-only models remain independent of it.
    required=decide({},[],ctx,NOW+2,required_markets=('NQ','ES','MNQ'))
    assert required['action']=='NO_TRADE' and 'SYNCHRONIZED_MNQ' in required['data_missing']
    before=st.stats()
    assert observe_once(ob,path,NOW+3)['processed']==0 and st.stats()==before
    with st.readonly() as db:
        assert dict(db.execute('SELECT symbol,count(*) FROM bars GROUP BY symbol'))=={'NQ':2,'ES':2,'MNQ':1}


def test_late_older_bar_preserved_once_without_retroactive_frames_or_alerts(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    source(path,'NQ',NOW-180);observe_once(ob,path,NOW-118)
    source(path,'NQ',NOW-60);observe_once(ob,path,NOW+2)
    before=st.stats();mem=memory(st);snapshot=st.snapshot()
    source(path,'NQ',NOW-120,received=NOW+10)
    result=observe_once(ob,path,NOW+10)
    assert result['processed']==1 and result['late'][0]['reason']=='LATE_BAR_HISTORY_ONLY_NO_RETROSPECTIVE_ALERT'
    assert st.stats()['bars']==before['bars']+1
    assert all(st.stats()[k]==before[k] for k in ('frames','events','snapshots','transitions','setups'))
    assert memory(st)==mem and st.snapshot()==snapshot
    assert observe_once(Observer(st,FakeKnowledge()),path,NOW+11)['processed']==0
    assert st.stats()['bars']==before['bars']+1


def test_missing_peer_does_not_block_any_market_or_exceed_five_batches(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    for ts in range(NOW-86400,NOW,60):source(path,'NQ',ts)
    original=path.read_bytes()
    result=observe_once(ob,path,NOW+2)
    assert result['processed']==5 and len(result['missing_pairs'])==5
    assert result['history_not_replayed_before']==NOW-300
    assert st.stats()['bars']==5 and path.read_bytes()==original
    assert st.snapshot()['context']['NQ']['latest']['ts']==NOW-60
    assert st.timeline()[0]['id']==st.snapshot()['id']
    assert len(st.timeline(limit=2))==5  # same-second group remains pageable
    assert observe_once(ob,path,NOW+2)['processed']==0


def test_receipt_future_is_not_available_and_late_peer_can_share_observation_second(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    source(path,'NQ',NOW-60,received=NOW+1)
    source(path,'ES',NOW-60,received=NOW+10)
    assert observe_once(ob,path,NOW+2)['processed']==1
    assert 'ES' not in memory(st)['markets']
    assert observe_once(ob,path,NOW+9)['processed']==0
    assert observe_once(ob,path,NOW+10)['processed']==1
    # Another receipt observed in the same rounded second is distinct evidence.
    source(path,'MNQ',NOW-60,received=NOW+10)
    assert observe_once(ob,path,NOW+10)['processed']==1
    assert st.snapshot()['observed_symbols']==['MNQ']
    assert synchronization_missing(st.snapshot()['context'],('NQ','ES','MNQ'))==[]


def test_changed_source_payload_cannot_silently_overwrite_evidence(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    source(path,'NQ',NOW-60);observe_once(ob,path,NOW+2)
    with sqlite3.connect(path) as db:
        row=json.loads(db.execute('SELECT payload FROM collection_bars').fetchone()[0]);row['close']+=.25
        db.execute('UPDATE collection_bars SET payload=?',(json.dumps(row),))
    with pytest.raises(ValueError,match='BAR_IMMUTABILITY_CONFLICT'):observe_once(ob,path,NOW+3)


def test_seen_rows_do_not_starve_later_receipts_under_a_smaller_batch_limit(tmp_path):
    path=tmp_path/'source.db';st,ob=observer(tmp_path)
    for ts in range(NOW-300,NOW,60):source(path,'NQ',ts)
    assert [observe_once(ob,path,NOW+2,max_batches=2)['processed'] for _ in range(4)]==[2,2,1,0]
    assert st.stats()['bars']==5 and st.snapshot()['context']['NQ']['latest']['ts']==NOW-60
