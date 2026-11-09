import base64
import json
import sqlite3
from datetime import datetime
from pathlib import Path
import pytest
from ict_scanner import aggregate, detail, index, NY, record_path

RID='ICT_INDEPENDENT_NYAM_20261102_7'
AT=datetime(2026,11,2,10,35,tzinfo=NY).timestamp()

def fixture(tmp_path, decision='WAIT'):
    e={}
    for n,(o,h,l,c) in enumerate([(100,104,99,102),(101,110,100,109),(108,112,107,110)]):
        t=AT-180+n*60;eid=f'MNQ:M1:{int(t)}'
        e[eid]=dict(kind='bar',symbol='MNQ',timeframe=1,time=t,available_at=t+60,open=o,high=h,low=l,close=c)
    p=None if decision=='WAIT' else dict(direction='LONG',entry_price=105.5,stop_price=99,target_price=125,valid_until=AT+600,entry_fvg_bar_ids=list(e))
    d=dict(decision=decision,summary='<script>alert(1)</script>',plan=p,watch_condition='Wait for MSS',assessments={'fvg_selection':{'reasoning':'Saved analysis','evidence_ids':list(e)}})
    r=dict(id=RID,requested_at=AT+5,received_at=AT+30,packet={'as_of':AT,'evidence':e},decision=d,validation={'state':'WAIT' if not p else 'VALIDATED_SHADOW_PROPOSAL'},flags={'secret':'never return'},original_sse='never return',request={'model':'test','secret':'never return'})
    path=record_path(tmp_path/'research',RID);path.parent.mkdir(parents=True);path.write_text(json.dumps(r))
    return r,path


def test_wait_has_no_invented_trade_and_reads_only(tmp_path):
    r,p=fixture(tmp_path);before=p.read_bytes()
    d=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID,AT+3600)
    assert d['decision']['decision']=='WAIT' and d['decision']['plan'] is None
    assert d['hypothetical_fixed_2r'] is None and d['zones']==[]
    assert len(d['packet_bars']['MNQ']['1'])==3
    assert not d['broker_orders'] and d['broker_gaps']
    assert 'never return' not in json.dumps(d)
    assert p.read_bytes()==before and list(tmp_path.rglob('*.sqlite3'))==[]


def test_families_keep_independent_plan_and_validation(tmp_path):
    r,p=fixture(tmp_path,'PROPOSE_LONG');r['decision']['ifvg_review']={'decision':'WAIT','plan':None,'summary':'No inversion'};r['validation']['ifvg_validation']={'state':'WAIT'};p.write_text(json.dumps(r))
    f=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID,AT+3600)
    i=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID+':IFVG',AT+3600)
    assert f['hypothetical_fixed_2r']==118.5 and f['decision']['plan']['target_price']==125
    assert f['zones'][0]['low']==104 and f['zones'][0]['high']==107
    assert i['decision']['plan'] is None and i['validation']['state']=='WAIT' and not i['zones']
    assert len(index(tmp_path/'research')['reviews'])==2


def test_future_missing_or_malformed_bar_not_claimed_as_ai_candle(tmp_path):
    r,p=fixture(tmp_path);e=r['packet']['evidence'];ids=list(e)
    e[ids[0]]['available_at']=AT+60;e[ids[1]].pop('available_at');e[ids[2]]['high']=0
    p.write_text(json.dumps(r));d=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID,AT+3600)
    assert not d['packet_bars']['MNQ']['1'] and set(d['unsafe_bar_ids'])==set(ids)


def test_aggregation_gaps_unclosed_bars_and_dst():
    t=int(datetime(2026,11,2,9,30,tzinfo=NY).timestamp())
    bars=[[t+i*60,100,104,99,102] for i in range(6)]
    assert len(aggregate(bars,3))==2
    assert len(aggregate(bars[:2]+bars[3:],3))==1
    assert aggregate(bars[:2],3)==[]
    anchor=int(datetime(2026,11,1,18,tzinfo=NY).timestamp())
    assert aggregate([[anchor+i*60,1,2,0,1] for i in range(240)],240)[0][0]==anchor


def test_archive_not_ai_input_and_conflicts_reported(tmp_path):
    r,p=fixture(tmp_path);folder=tmp_path/'market_context';folder.mkdir()
    with sqlite3.connect(folder/'tanja.sqlite3') as db:
        for table in ['bars','collection_bars']:db.execute(f'CREATE TABLE {table}(symbol,start,payload)')
        for n in range(5):
            t=AT-180+n*60
            b=dict(bar_open_ms=t*1000,bar_close_ms=(t+60)*1000,confirmed=n!=4,ticker='MNQ1!',open=100,high=104,low=99,close=102)
            db.execute('INSERT INTO bars VALUES(?,?,?)',('MNQ',t,json.dumps(b)))
    d=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID,AT+3600)
    assert d['archive']['MNQ']['minute_count']==4 and len(d['packet_bars']['MNQ']['1'])==3
    assert len(d['archive_conflicts'])==2 and d['archive']['MNQ']['tickers']==['MNQ1!']


@pytest.mark.parametrize('rid',['../secret','ICT_INDEPENDENT_NYAM_20261102_7.json','ICT_INDEPENDENT_NYAM_20261102_7:OTHER','x'])
def test_id_cannot_escape_directory(tmp_path,rid):
    with pytest.raises(ValueError):record_path(tmp_path,rid)


def test_index_includes_old_wait_errors_and_unreadable(tmp_path):
    r,p=fixture(tmp_path)
    for i in range(105):
        rr=dict(r,id=f'ICT_INDEPENDENT_NYAM_20261102_{i}',requested_at=AT+i)
        if i==104:rr['decision']=None;rr['state']='FAILED'
        p.with_name(rr['id']+'.json').write_text(json.dumps(rr))
    p.with_name('ICT_INDEPENDENT_NYAM_20261102_999.json').write_text('{')
    result=index(tmp_path/'research')
    assert len(result['reviews'])==105 and result['unreadable_records']==1
    assert result['reviews'][0]['decision']=='NO_RESPONSE'


def test_routes_authenticated_get_only_no_work(tmp_path,monkeypatch):
    from flask import Flask
    from ict_shadow_cloud import register,Cloud
    monkeypatch.setattr(Cloud,'state',lambda *_:pytest.fail('Replay must not run cloud state'))
    app=Flask(__name__);register(app,{'DATA_DIR':str(tmp_path),'TANJA_DASHBOARD_PASSWORD':'test'},start=False)
    fixture(tmp_path);c=app.test_client();auth={'Authorization':'Basic '+base64.b64encode(b'tanja:test').decode()}
    for path in ['/ict-shadow/scanner/index','/ict-shadow/scanner/review/'+RID,'/ict-shadow/scanner.js','/ict-shadow/scanner.css']:
        assert c.get(path).status_code==401
        assert c.post(path,headers=auth).status_code==405
        response=c.get(path,headers=auth)
        assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert c.get('/ict-shadow/scanner/review/nope',headers=auth).status_code==400
    assert c.get('/ict-shadow/scanner/review/ICT_INDEPENDENT_NYAM_20261102_999',headers=auth).status_code==404


def test_literal_api_input_wins_over_precompaction_packet(tmp_path):
    r,p=fixture(tmp_path);sent=json.loads(json.dumps(r['packet']));eid=next(iter(sent['evidence']));sent['evidence']={eid:sent['evidence'][eid]}
    r['request']['input']=[{'role':'user','content':json.dumps(sent)}];p.write_text(json.dumps(r))
    d=detail(tmp_path/'research',{'DATA_DIR':str(tmp_path)},RID,AT+3600)
    assert d['packet_source']=='SAVED_API_INPUT' and len(d['packet_bars']['MNQ']['1'])==1
