import json,sqlite3,hashlib
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from tools.observe_railway_shadow import mirror_ingest,ssh_command,immutable
from services.tanja.ict_agent.prospective_shadow import gate

def test_unverified_passive_is_explicit_and_calendar_still_required(tmp_path):
    p=tmp_path/'proof';p.write_text('MOCK ONLY')
    proof=dict(verified=True,valid_from=0,valid_until=200,evidence_file=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    r=dict(classification='FEED_UNVERIFIED',passive_unverified_authorized=True,symbols=['NQ','MNQ','ES'],instrument_binding=dict(proof,tickers={s:s+'1!' for s in ('NQ','MNQ','ES')}),exchange_calendar=dict(proof,open_intervals_utc=[[0,200]]))
    assert gate(r,100)['ready'] and not gate(r,200)['ready']
    r['passive_unverified_authorized']=False;assert not gate(r,100)['ready']
    r['classification']='VERIFIED_REALTIME';assert not gate(r,100)['ready']

def test_exact_receipts_dedup_restart_and_revision_refusal(tmp_path):
    bindings={s:'CME_MINI:'+s+'1!' for s in ('NQ','MNQ','ES')};rows=[]
    for s in bindings:
        p=dict(symbol=s,ticker=bindings[s],bar_open_ms=60000,bar_close_ms=120000,confirmed=True,standard=True,open=10,high=11,low=9,close=10)
        rows.append([s,60,120,121.123456,json.dumps(p)])
    e=dict(read_only=True,captured_at=125,bindings=bindings,rows=rows);f=tmp_path/'mirror.db'
    assert mirror_ingest(f,e,126*10**9,bindings)==3
    assert mirror_ingest(f,e,130*10**9,bindings)==0
    with sqlite3.connect(f) as db:
        assert db.execute('SELECT received FROM bars').fetchone()[0]==121.123456
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM bars')
    bad=deepcopy(e);bad['rows'][0][3]+=1
    with pytest.raises(ValueError,match='REVISION'):mirror_ingest(f,bad,130*10**9,bindings)
    before=hashlib.sha256(f.read_bytes()).hexdigest()
    bad=deepcopy(e);bad['captured_at']=140
    with pytest.raises(ValueError,match='CLOCK'):mirror_ingest(f,bad,130*10**9,bindings)
    assert hashlib.sha256(f.read_bytes()).hexdigest()==before

def test_bounded_reader_no_production_writes_or_api_and_dst():
    cmd=ssh_command(10,100);script=cmd[-1]
    assert 'mode=ro' in script and 'query_only=ON' in script and 'while time.time()<deadline' in script
    assert all(s not in script for s in ('api.openai','requests.','INSERT ','UPDATE ','DELETE ','environ','variables'))
    ny=ZoneInfo('America/New_York')
    assert datetime(2026,7,1,13,tzinfo=ny).utcoffset().total_seconds()==-14400
    assert datetime(2026,11,23,13,tzinfo=ny).utcoffset().total_seconds()==-18000
