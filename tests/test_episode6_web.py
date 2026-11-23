import json
import sqlite3
from flask import Flask

from services.tanja.ict_agent.episode6_web import register


def fixture(tmp_path):
    runtime = tmp_path / 'web'; runtime.mkdir()
    root = tmp_path / 'research'; root.mkdir()
    (runtime / 'episode6-research.json').write_text(json.dumps({'directory': str(root)}))
    db = sqlite3.connect(root / 'qualification.sqlite')
    db.executescript('''CREATE TABLE cases(id,candidate_id,symbol,decision_at,status,chain_id,exposure_group,payload);
    CREATE TABLE conditions(case_id,condition_id,status,payload);''')
    for i in range(31):
        status = 'FAILED' if i % 2 else 'CONTEXT_UNRESOLVED'
        db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?,?,?)', (str(i), str(i), 'NQ', 100+i, status, str(i), str(i), json.dumps({'id':str(i),'status':status,'execution_eligible':False})))
        db.execute('INSERT INTO conditions VALUES(?,?,?,?)', (str(i),'E6R-11',status,'{}'))
    db.commit();db.close()
    app=Flask(__name__);register(app,runtime)
    return app.test_client(), runtime, root


def test_qualification_filters_pagination_preserve_evidence(tmp_path):
    c,_,root=fixture(tmp_path); before=(root/'qualification.sqlite').read_bytes()
    base='/ict-shadow/shadow-mvp/episode6'
    first=c.get(base+'/cases').get_json(); second=c.get(base+'/cases?offset=25').get_json()
    assert len(first['items'])==25 and len(second['items'])==6
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    failed=c.get(base+'/cases?condition=E6R-11&status=FAILED').get_json()
    assert failed['total']==15 and all(r['status']=='FAILED' for r in failed['items'])
    assert c.get(base+'/cases?symbol=ES').get_json()['total']==0
    assert c.get(base+'/cases/0').get_json()['execution_eligible'] is False
    assert c.post(base+'/cases/0').status_code==405
    assert (root/'qualification.sqlite').read_bytes()==before


def test_qualification_rejects_bad_filters_and_unavailable_build(tmp_path):
    c,runtime,_=fixture(tmp_path); base='/ict-shadow/shadow-mvp/episode6'
    for q in ('offset=-1','offset=no','symbol=BAD','status=PASS','condition=madeup'):
        assert c.get(base+'/cases?'+q).status_code==400
    assert c.get(base+'/cases/missing').status_code==404
    assert c.get(base+'/summary').status_code==503
    (runtime/'episode6-research.json').write_text(json.dumps({'directory':'/etc'}))
    assert c.get(base+'/cases').status_code==503


def test_versioned_patch_requires_unchanged_original_and_labels_old_cross(tmp_path):
    from services.tanja.ict_agent.common import digest
    c,runtime,root=fixture(tmp_path);parent=tmp_path/'original';parent.mkdir()
    original={'id':'0','chart':{'annotations':[{'kind':'STRUCTURE_BREAK'}]},'source_qualified':False}
    old=sqlite3.connect(parent/'qualification.sqlite');old.execute('CREATE TABLE cases(id,payload)')
    old.execute('INSERT INTO cases VALUES(?,?)',('0',json.dumps(original)));old.commit();old.close()
    patch={'version':'episode6-preraid-witness-v2','id':'0','original_case_sha256':digest(original),
           'structural_review':{'witnesses':[]},'source_qualified':False}
    db=sqlite3.connect(root/'qualification.sqlite');db.execute('CREATE TABLE metadata(key,payload)')
    db.execute('INSERT INTO metadata VALUES(?,?)',('original_directory',str(parent)))
    db.execute('UPDATE cases SET payload=? WHERE id=?',(json.dumps(patch),'0'));db.commit();db.close()
    url='/ict-shadow/shadow-mvp/episode6/cases/0'
    value=c.get(url).get_json()
    assert value['chart']['annotations'][0]['kind']=='ORIGINAL_CROSS_REFERENCE'
    assert value['source_qualified'] is False
    old=sqlite3.connect(parent/'qualification.sqlite');old.execute('UPDATE cases SET payload=?',(json.dumps({'id':'changed'}),));old.commit();old.close()
    assert c.get(url).status_code==503
