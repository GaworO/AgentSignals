import json
import sqlite3
from flask import Flask

from services.tanja.ict_agent.strategy_research_web import register


def fixture_app(tmp_path):
    runtime = tmp_path / 'web'; runtime.mkdir()
    data = tmp_path / 'research'; data.mkdir()
    (runtime / 'strategy-research.json').write_text(json.dumps({'directory': str(data)}))
    (data / 'summary.json').write_text(json.dumps({'models': [{'id': 'fvg'}]}))
    db = sqlite3.connect(data / 'research.sqlite')
    db.executescript('''CREATE TABLE events(id,symbol,at,kind,tf,direction,session,month,partition,payload);
    CREATE TABLE candidates(id,symbol,created_at,direction,session,month,partition,state,complete_geometry,engineering_plan,payload);
    CREATE TABLE model_occurrences(model_id,entity_kind,entity_id,screen_status,reason);''')
    for i in range(65):
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)', (str(i), 'NQ', 100+i, 'FVG',1,'LONG','NYAM','2026-05','validation','{}'))
        db.execute('INSERT INTO model_occurrences VALUES(?,?,?,?,?)', ('fvg','event',str(i),'GEOMETRY_ONLY','not qualified'))
    db.commit();db.close()
    app=Flask(__name__);register(app,runtime)
    return app.test_client(), runtime, data


def test_read_only_pagination_and_instrument_filter(tmp_path):
    client, _, data=fixture_app(tmp_path)
    before=(data/'research.sqlite').read_bytes()
    first=client.get('/ict-shadow/shadow-mvp/research/occurrences?model=fvg').get_json()
    second=client.get('/ict-shadow/shadow-mvp/research/occurrences?model=fvg&offset=50').get_json()
    assert len(first['items'])==50 and first['has_more']
    assert len(second['items'])==15 and not second['has_more']
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    assert client.get('/ict-shadow/shadow-mvp/research/occurrences?model=fvg&symbol=ES').get_json()['items']==[]
    assert (data/'research.sqlite').read_bytes()==before
    assert client.post('/ict-shadow/shadow-mvp/research/summary').status_code==405


def test_bad_filters_and_outside_workspace_are_rejected(tmp_path):
    client,runtime,_=fixture_app(tmp_path)
    for query in ('model=fvg&offset=-1','model=fvg&offset=nan','model=fvg&symbol=BAD',"model='OR1=1"):
        assert client.get('/ict-shadow/shadow-mvp/research/occurrences?'+query).status_code==400
    (runtime/'strategy-research.json').write_text(json.dumps({'directory':'/etc'}))
    assert client.get('/ict-shadow/shadow-mvp/research/summary').status_code==503


def test_null_metrics_are_preserved(tmp_path):
    client,_,data=fixture_app(tmp_path)
    payload={'models':[{'id':'fvg','pf_status':'PF_NOT_ESTIMABLE','pf':None,'source_prerequisites_verified_count':None,'simulation_eligible_count':0}]}
    (data/'summary.json').write_text(json.dumps(payload))
    assert client.get('/ict-shadow/shadow-mvp/research/summary').get_json()==payload
