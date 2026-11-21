"""Read-only library UI contracts; realistic SQLite + native auth, no providers."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import pytest
from flask import Flask
SCHEMA = "\nCREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);\nCREATE TABLE sources(video_id TEXT PRIMARY KEY,payload TEXT NOT NULL);\nCREATE TABLE cues(video_id TEXT,idx INTEGER,start REAL,end REAL,text TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(video_id,idx));\nCREATE TABLE chunks(id TEXT PRIMARY KEY,video_id TEXT,cue_start INTEGER,cue_end_exclusive INTEGER,start REAL,payload TEXT NOT NULL);\nCREATE VIRTUAL TABLE text_search USING fts5(id UNINDEXED,text,tokenize='unicode61');\nCREATE TABLE rules(id TEXT PRIMARY KEY,kind TEXT,payload TEXT NOT NULL);\nCREATE TABLE conditions(id TEXT PRIMARY KEY,rule_id TEXT,stage TEXT,source_class TEXT,payload TEXT NOT NULL);\nCREATE TABLE families(id TEXT PRIMARY KEY,payload TEXT NOT NULL);\nCREATE TABLE variants(id TEXT PRIMARY KEY,family TEXT,payload TEXT NOT NULL);\nCREATE TABLE programs(id TEXT PRIMARY KEY,family TEXT,payload TEXT NOT NULL);\nCREATE TABLE audit_notes(id TEXT PRIMARY KEY,payload TEXT NOT NULL);\nCREATE TABLE examples(id TEXT PRIMARY KEY,payload TEXT NOT NULL);\nCREATE TABLE gap_annotations(id TEXT PRIMARY KEY,video_id TEXT,cue_index INTEGER,payload TEXT NOT NULL);\n"  # Isolated DB fixture; compiler is not shipped to HTTP service.
from services.tanja.ict_agent.research.library.repository import Library
from services.tanja.ict_agent.research.library.knowledge_view import KnowledgeView, public, youtube
from services.tanja.ict_agent.integrated_web import blueprint

VID='abcdefghijk'
RULE='full-assistant-'+VID+'-001'

@pytest.fixture
def library(tmp_path):
    p=tmp_path/'library.sqlite'
    with sqlite3.connect(p) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO metadata VALUES (?,?)',('summary',json.dumps(dict(schema='fixture',release_id='test-release',sources=2))))
        for i,v in enumerate([VID,'lmnopqrstuv']):
            s=dict(video_id=v,catalogue_index=i+1,title=f'Source {i} <script>bad()</script>',series=['2022' if i==0 else 'OTE'],cues=62,words=100,source_rule_ids=[RULE] if i==0 else [],prior_reviewed_cues=62,visual_verified=False,audio_verified=False,source_completeness='NOT_VERIFIED',provenance={'raw_path':'/Users/private/key.txt','identity_status':'MATCH','url':'javascript:alert(1)'},quality={},prior_audit={})
            db.execute('INSERT INTO sources VALUES (?,?)',(v,json.dumps(s)))
            for j in range(62):
                cue=dict(start=j*3,end=j*3+3,text=f'cue {j} <img src=x onerror=bad()>')
                db.execute('INSERT INTO cues VALUES(?,?,?,?,?,?)',(v,j,j*3,j*3+3,cue['text'],json.dumps(cue)))
            for j in range(3):
                ident=f'{v}-chunk{j}';row=dict(id=ident,video_id=v,cue_start=j*20,cue_end_exclusive=(j+1)*20,start=j*60,text='liquidity stop gap zebra',url='javascript:bad()')
                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',(ident,v,j*20,(j+1)*20,j*60,json.dumps(row)))
                db.execute('INSERT INTO text_search VALUES(?,?)',(ident,row['text']))
        ref=dict(video_id=VID,cue_start=12,cue_end=60,start=36,end=180,url='javascript:bad()',local_source_file='sources/private.json',passage_sha256='proof')
        rule=dict(id=RULE,model='Rule',description='Keep the source text <script>bad</script>',references=[ref],fields={'stop_loss':'source defined stop'},exceptions=['exception'],ambiguities=['ambiguous chart'],contradictions=[],visual_required=True)
        db.execute('INSERT INTO rules VALUES(?,?,?)',(RULE,'MODEL_FRAGMENT',json.dumps(rule)))
        for i in range(25):
            f=dict(id=f'family{i:02}',name=f'Model {i}',kind='MODEL' if i<20 else 'CONTEXT',supporting_video_ids=[VID],supporting_rule_ids=[RULE],variant_fragment_ids=['v'],sides={},readiness_counts={'SOURCE_CONFLICT':1})
            db.execute('INSERT INTO families VALUES(?,?)',(f['id'],json.dumps(f)))
        for i in range(23):
            v=dict(id=f'v{i:02}',family='family00',readiness='SOURCE_CONFLICT' if i<3 else 'PARTIAL_RESEARCH',missing_stages=['target'],conditions=[],references=[ref],contradictions=['source disagreement'])
            db.execute('INSERT INTO variants VALUES(?,?,?)',(v['id'],v['family'],json.dumps(v)))
        db.execute('INSERT INTO programs VALUES(?,?,?)',('p','family00',json.dumps(dict(id='p',family='family00',readiness='PARTIAL_RESEARCH',unresolved=['not a failed market condition'],missing_stages=['target']))))
    return Library(p)

@pytest.fixture
def view(library):return KnowledgeView(library)

@pytest.fixture
def client(library,tmp_path):
    report=tmp_path/'reports';report.mkdir();(report/'knowledge-index.json').write_text(json.dumps(dict(library_release='test-release',items=[dict(id='case',family_ids=['family00'],rule_ids=[RULE])])) )
    app=Flask(__name__);app.register_blueprint(blueprint(dict(ICT_AI_LIBRARY_DB=str(library.path),ICT_AI_CONTEXT_REPORT_DIR=str(report)),lambda:True),url_prefix='/ict-shadow/ai-trader')
    return app.test_client()


def test_fulltext_source_pages_are_lossless_not_preloaded(view):
    a=view.source(VID,limit=40);b=view.source(VID,limit=40,offset=40)
    assert [x['index'] for x in a['items']+b['items']]==list(range(62))
    assert a['next_offset']==40 and b['next_offset'] is None
    assert a['source']['provenance']['identity_status']=='MATCH'
    assert 'raw_path' not in a['source']['provenance']


def test_exact_rule_excerpt_page_and_end_boundaries(view):
    a=view.rule(RULE,limit=40);b=view.rule(RULE,limit=40,offset=40)
    assert [x['index'] for x in a['items']+b['items']]==list(range(12,61))
    assert a['rule']['references'][0]['passage_sha256']=='proof'
    assert 'local_source_file' not in a['rule']['references'][0]
    assert a['items'][0]['url'].endswith('&t=36s')


def test_server_family_search_filter_and_pagination(view):
    a=view.families(kind='MODEL',limit=7);b=view.families(kind='MODEL',limit=7,offset=7)
    assert a['total']==20 and len(a['items'])==7
    assert not {f['id'] for f in a['items']} & {f['id'] for f in b['items']}
    assert view.families(q='Model 24')['items'][0]['kind']=='CONTEXT'
    assert view.family('family00',limit=10,offset=20)['total']==23
    assert len(view.family('family00',limit=10,offset=20)['items'])==3
    assert view.family('family00','rules')['items'][0]['id']==RULE


def test_source_search_and_playlist_filters(view):
    assert view.sources(series='OTE')['total']==1
    assert view.sources(q='Source 0')['items'][0]['video_id']==VID
    assert view.source('missing') is None


def test_search_entire_fulltext_filtered_ranked_and_paged(view):
    a=view.search('zebra',limit=2);b=view.search('zebra',limit=2,offset=2)
    assert a['total']==6 and len(a['items'])==2
    assert not {x['id'] for x in a['items']} & {x['id'] for x in b['items']}
    assert view.search('zebra',video=VID)['total']==3
    assert view.search('zebra',series='OTE')['total']==3
    assert view.search('zebra',family='family00')['total']==3
    assert RULE in view.search('zebra',video=VID)['items'][0]['related_rule_ids']
    assert not a['answer_generated'] and a['method']=='FTS5_LEXICAL'


def test_natural_question_polish_expansion_and_safe_fts(view):
    result=view.search('Gdzie jest płynność i wejście?')
    assert 'liquidity' in result['terms'] and 'entry' in result['terms']
    assert result['total']==6
    assert view.search('" OR *; DROP TABLE sources; --')['total']==0
    assert view.search(' ") (')['total']==0
    assert view.sources()['total']==2


def test_conflicts_missing_and_data_requirements_not_market_failure(view):
    assert view.completeness('conflicts')['total']==3
    p=view.completeness('incomplete')['items'][0]
    assert p['unresolved']==['not a failed market condition'] and p['missing_stages']==['target']


@pytest.mark.parametrize('options',[{'limit':0},{'limit':51},{'offset':-1},{'offset':100001}])
def test_request_work_is_bounded(view,options):
    for call in [view.families,view.sources,view.completeness]:
        with pytest.raises(ValueError):call(**options)
    with pytest.raises(ValueError):view.search('q',**options)


@pytest.mark.parametrize('path',['/overview','/families','/sources','/sources/'+VID,'/rules/'+RULE,'/families/family00','/search?q=zebra','/completeness','/history'])
def test_authenticated_readonly_http(client,path):
    r=client.get('/ict-shadow/ai-trader/api/knowledge'+path)
    assert r.status_code==200 and r.headers['Cache-Control']=='no-store'
    assert 'nosniff'==r.headers['X-Content-Type-Options']
    assert '/Users/' not in r.text and 'local_source_file' not in r.text and 'javascript:' not in r.text
    assert client.post('/ict-shadow/ai-trader/api/knowledge'+path).status_code==405


def test_auth_blocks_all_new_routes_without_reading_database():
    app=Flask(__name__);app.register_blueprint(blueprint({},lambda:False),url_prefix='/ai')
    with patch.object(Library,'__init__',side_effect=AssertionError('auth must precede DB')):
        for path in ['/api/knowledge/overview','/api/knowledge/search?q=gap','/assets/knowledge.js','/assets/knowledge.css']:
            assert app.test_client().get('/ai'+path).status_code==401


def test_registration_does_not_initialize_library_or_indexes():
    with patch.object(Library,'__init__',side_effect=AssertionError('preloaded library')):
        app=Flask(__name__);app.register_blueprint(blueprint({},lambda:True),url_prefix='/ai')
        assert app.test_client().get('/ai/api/health').json['live_enabled'] is False


@pytest.mark.parametrize('suffix',['/families?offset=-1','/sources?limit=100000','/rules/'+RULE+'?reference=-1','/search?q='+('x'*501),'/completeness?category=wrong','/families/family00?section=sqlite_master'])
def test_http_validation(client,suffix):
    assert client.get('/ict-shadow/ai-trader/api/knowledge'+suffix).status_code==400


def test_history_is_exact_rule_and_family_linkage(client):
    base='/ict-shadow/ai-trader/api/knowledge/history'
    assert client.get(base+'?family=family00&rule='+RULE).json['total']==1
    assert client.get(base+'?family=other').json['total']==0
    assert client.get(base+'?rule=other').json['total']==0


def test_readonly_connection_closes_and_repeated_concurrent_reads_do_not_mutate(library):
    before=library.path.read_bytes()
    def read(_):return KnowledgeView(library).search('zebra',limit=2)['total']
    with ThreadPoolExecutor(max_workers=8) as pool:assert list(pool.map(read,range(32)))==[6]*32
    assert library.path.read_bytes()==before
    with library.read() as db:
        with pytest.raises(sqlite3.OperationalError):db.execute('DELETE FROM sources')
    with pytest.raises(sqlite3.ProgrammingError):db.execute('SELECT 1')


def test_private_metadata_redaction_and_unsafe_url_construction():
    r=public(dict(local_source_file='sources/a.json',raw_path='/Users/a/secret',api_key='secret',notes='see /private/tmp/private.txt',references=[dict(video_id=VID,start=12,url='https://attacker.test/?key=secret',raw_path='/etc/secret')]))
    assert r['references'][0]['url']=='https://www.youtube.com/watch?v=abcdefghijk&t=12s'
    assert r['notes']=='see [private location]'
    assert not set(r)&{'local_source_file','raw_path','api_key'}
    assert youtube('javascript:alert(1)') is None
    assert youtube(VID,float('nan')).endswith('t=0s')


def test_native_navigation_lazy_ui_and_exact_scenario_deeplinks():
    import ict_ai_trader
    root=Path(ict_ai_trader.__file__).parent
    html=ict_ai_trader.render_shadow((root/'ict_shadow.html').read_text())
    assert 'id="viewICTKnowledge"' in html and 'id="ictKnowledgeView" hidden' in html
    assert '<!--ICT_KNOWLEDGE_VIEW-->' not in html and 'knowledge.js' in html
    app=(root/'services/tanja/ict_agent/static/app.js').read_text()
    onopen=app.split('window.openICTTrader=')[1].split('function remember')[0]
    assert 'renderLibrary()' not in onopen and 'renderLibraryReplay()' not in onopen
    assert 'knowledgeLink(a.rule_id)' in app and 'knowledgeLink(id,id)' in app
    assert "get('ai_case')" in app


def test_private_volume_release_paths_and_no_research_compiler(library,tmp_path):
    import shutil,sys
    release='a'*24
    base=tmp_path/'volume/ict_ai/library136';folder=base/release;folder.mkdir(parents=True)
    shutil.copyfile(library.path,folder/'library.sqlite')
    (base/'current.json').write_text(json.dumps({'release_id':release}))
    reports=folder/'context-reports';reports.mkdir();(reports/'index.json').write_text(json.dumps({'status':'FROZEN_TEST','cases':[]}))
    app=Flask(__name__);app.register_blueprint(blueprint({'DATA_DIR':str(tmp_path/'volume')},lambda:True),url_prefix='/ai')
    with app.test_client() as c:
        assert c.get('/ai/api/knowledge/overview').status_code==200
        assert c.get('/ai/api/context-intelligence').json['status']=='FROZEN_TEST'
        assert c.post('/ai/api/knowledge/overview').status_code==405
    assert 'services.tanja.ict_agent.research.library.build' not in sys.modules
