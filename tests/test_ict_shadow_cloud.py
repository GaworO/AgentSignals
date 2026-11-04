import json,time,sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import pytest
from flask import Flask
from ict_shadow_cloud import Cloud, cost, compact, session, register

def ts(s='2026-11-02T09:35:00'):return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
def payload():return dict(model='gpt-6.1-sol',service_tier='default',max_output_tokens=5000,input='hello')
@pytest.fixture
def c(tmp_path,monkeypatch):
    import ict_news_context
    import ict_shadow_research
    # tick updates these globals; keep test order from changing legacy dates.
    monkeypatch.setattr(ict_shadow_research,'DAY',ict_shadow_research.DAY)
    monkeypatch.setattr(ict_shadow_research,'PREFIX',ict_shadow_research.PREFIX)
    # Scheduler/budget fixtures use a clear synthetic calendar. Calendar I/O,
    # freshness and blackouts have separate end-to-end tests.
    monkeypatch.setattr(ict_news_context,'build',lambda config,now:dict(
        version=ict_news_context.VERSION,status='CLEAR',observed_at=now,
        next_blackout_at=None,post_release_not_before=None,events=[]))
    return Cloud({'DATA_DIR':str(tmp_path),'TANJA_DASHBOARD_PASSWORD':'test-password-long'})
def test_cache_write_pricing():
    assert cost({'input_tokens':100000,'input_tokens_details':{'cache_write_tokens':100000},'output_tokens':1000})==.26
    assert cost({'input_tokens':100000,'input_tokens_details':{'cached_tokens':100000},'output_tokens':1000})==.02
    assert cost({}) is None
    assert cost({'input_tokens':1,'input_tokens_details':{'cached_tokens':2},'output_tokens':0}) is None

def test_cost_cap_restart_unknown_duplicate(c):
    p=payload();p['input']='x'*250000
    n=0
    while True:
        try:c.reserve('r_'+str(n),p,ts());n+=1
        except ValueError as e:assert str(e)=='DAILY_DOLLAR_CAP';break
    assert n>1
    d=Cloud(c.config)
    with pytest.raises(ValueError):d.reserve('r_0',payload(),ts())
    with d.db() as db:assert db.execute('SELECT SUM(reserve) FROM costs').fetchone()[0]<=8

def test_atomic_dedup(c):
    def f(_):
        try:c.reserve('same_1',payload(),ts());return 1
        except ValueError:return 0
    with ThreadPoolExecutor(max_workers=4) as ex:assert sum(ex.map(f,range(4)))==1

def test_unpriced_or_huge_blocks(c):
    p=payload();p['model']='different'
    with pytest.raises(ValueError,match='UNPRICED'):c.reserve('a',p,ts())
    p=payload();p['input']='x'*280000
    with pytest.raises(ValueError,match='BOUND'):c.reserve('a',p,ts())

def test_unknown_keeps_reserve(c):
    c.reserve('a',payload(),ts());c.finish('a',None,'timeout')
    with c.db() as db:
        r=db.execute('SELECT * FROM costs').fetchone();assert r['amount'] is None and r['state']=='UNKNOWN_RESERVED'

def test_session_dst():
    assert session(ts())[0]=='NYAM'
    assert datetime.fromtimestamp(ts(),ZoneInfo('UTC')).hour==14
    assert datetime.fromtimestamp(ts('2026-10-30T09:35:00'),ZoneInfo('UTC')).hour==13
    assert session(ts('2026-11-02T11:01:00')) is None
    assert session(ts('2026-10-31T09:35:00')) is None
    assert session(ts('2026-11-02T16:00:00')) is None

def test_event_selector_and_budget(c):
    at=ts();p={'as_of':at,'trigger_events':[]}
    assert c.select([],p,at)[1]=='CLOUD_NYAM_OPENING'
    rows=[dict(id='x_0',status='research_completed',requested_at=at-120)]
    assert c.select(rows,p,at)[0] is None
    p['trigger_events']=[{'available_at':at-60,'kind':'MSS'}]
    assert c.select(rows,p,at)[1]=='CLOUD_STRUCTURE_EVENT'
    rows[0]['status']='research_unknown'
    assert c.select(rows,p,at)[0] is None
    rows=[dict(id='x_'+str(i),status='research_completed',requested_at=at-120) for i in range(16)]
    assert c.select(rows,p,at)[1]=='DAILY_CALL_CAP'

def test_burned_reservation_not_retried(c):
    at=ts();c.reserve('ICT_0',payload(),at)
    assert c.select([],{'as_of':at,'trigger_events':[]},at)[0]==1

def test_dashboard_private_no_mutations(c):
    app=Flask(__name__);register(app,c.config,start=False);client=app.test_client()
    assert client.get('/ict-shadow').status_code==401
    import base64
    h={'Authorization':'Basic '+base64.b64encode(b'tanja:test-password-long').decode()}
    assert client.get('/ict-shadow',headers=h).status_code==200
    r=client.get('/ict-shadow/state',headers=h);assert r.status_code==200 and r.json['execution_authorized'] is False
    assert client.post('/ict-shadow/state',headers=h).status_code==405
    assert not any('order' in x.rule for x in app.url_map.iter_rules())

def test_compaction_preserves_mnq_and_evidence():
    p={'evidence':{},'missing_context':[]}
    for i in range(70):p['evidence'][str(i)]={'kind':'bar','symbol':'MNQ','timeframe':1,'available_at':i}
    p['evidence']['anchor']={'kind':'FVG','symbol':'MNQ','timeframe':1,'available_at':71,'evidence_ids':['0','1','2']}
    q=compact(p)
    assert all(k in q['evidence'] for k in ('0','1','2','69','anchor'))
    assert q['coverage']['omitted_items']>0

def test_import_old_attempt_cost_no_dup(c):
    folder=c.root/'ict_2022_nyam_20261030';folder.mkdir()
    r={'id':'ICT_INDEPENDENT_NYAM_20261030_0','requested_at':1,'response':{'usage':{'input_tokens':100,'output_tokens':100}}}
    (folder/(r['id']+'.json')).write_text(json.dumps(r));c.import_costs();c.import_costs()
    with c.db() as db:assert db.execute('SELECT count(*) FROM costs').fetchone()[0]==1

def test_default_worker_disabled(c,monkeypatch):
    import ict_shadow_research
    monkeypatch.setattr(ict_shadow_research,'remote_run',lambda *a,**k:pytest.fail('network forbidden'))
    c.tick()
    with c.db() as db:assert 'SCANNER_DISABLED' in db.execute("SELECT value FROM state WHERE key='last_check'").fetchone()[0]

@pytest.mark.parametrize('future',[False,True,'extension'])
@pytest.mark.parametrize('production_state',[None,'finished_unknown','running','unfinished_unknown'])
def test_full_review_pipeline_mocked_only(c,monkeypatch,future,production_state):
    import ict_shadow_research as r, ab_ai_context as ai, directional_ai_transport as transport
    from pathlib import Path
    p=json.loads((Path(__file__).parent/'fixtures/ict_shadow_market.json').read_text())['packet']
    at=ts('2026-11-02T09:35:00') if future is True else ts('2026-10-30T15:15:00') if future=='extension' else ts('2026-10-30T13:35:00')
    delta=at-60-p['as_of'];p['as_of']+=delta
    for e in p['evidence'].values():
        e['available_at']+=delta
        if 'time' in e:e['time']+=delta
    p.pop('manual_authorization',None)
    monkeypatch.setattr(r,'DAY','2026-11-02' if future is True else '2026-10-30')
    monkeypatch.setattr(r,'PREFIX','ICT_INDEPENDENT_NYAM_'+r.DAY.replace('-','')+'_')
    monkeypatch.setattr(r.time,'time',lambda:at)
    monkeypatch.setattr(ai.ContextSupport,'read_source',lambda _: {'calls_today':0})
    monkeypatch.setattr(ai.ContextSupport,'packet',lambda *_:p.copy())
    monkeypatch.setenv('DATA_DIR',str(c.root.parent));monkeypatch.setenv('OPENAI_MODEL','gpt-6.1-sol');monkeypatch.setenv('OPENAI_API_KEY','mock-not-a-key')
    with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:
        db.executescript('CREATE TABLE reviews(id TEXT,day TEXT,status TEXT,requested_at REAL,review_kind TEXT,finished REAL,error_code TEXT); CREATE TABLE setup_requests(state TEXT, expires REAL);')
        if production_state:
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',('production-original',r.DAY,'running' if production_state=='running' else 'unknown',at-3600,'SETUP',at-3500 if production_state=='finished_unknown' else None,'TimeoutError'))
        production_before=db.execute('SELECT * FROM reviews').fetchall()
    sent=[]
    def request(payload,key,seconds):
        sent.append(payload);pk=json.loads(payload['input'][1]['content'])
        return {'status':'completed','model':'gpt-6.1-sol','usage':{'input_tokens':1000,'output_tokens':100},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'packet_id':pk['packet_id'],'execution_authorized':False,'decision':'WAIT','plan':None,'assessments':{},'summary':'Mock wait'})}]}]}
    monkeypatch.setattr(transport,'request',request)
    auth=None
    if future=='extension':
        monkeypatch.setattr(c,'select',lambda rows,p,now:(8,'CLOUD_STRUCTURE_EVENT',None) if not rows else (None,'90_SECOND_COOLDOWN',None))
        monkeypatch.setattr(r,'reserve_borrowed_call',lambda *a,**k:pytest.fail('Production debit forbidden'))
    result=r.remote_run(None,pm_authorization=auth,cloud=c)
    if production_state in ('running','unfinished_unknown'):
        assert result['state'] in ('DEFER_PRODUCTION_AI_BUSY','DEFER_PRODUCTION_AI_UNRESOLVED')
        assert not sent
        with c.db() as db:assert db.execute('SELECT count(*) FROM costs').fetchone()[0]==0
        with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:assert db.execute('SELECT * FROM reviews').fetchall()==production_before
        return
    assert result['state']=='research_completed' and result['validation']['state']=='WAIT'
    assert len(sent)==1
    assert json.loads(sent[0]['input'][1]['content'])['scheduled_news']['status']=='CLEAR'
    assert 'news_and_session_risk' in sent[0]['text']['format']['schema']['properties']['assessments']['required']
    assert 'Assess scheduled_news explicitly' in sent[0]['input'][0]['content']
    assert 'ifvg_review' in sent[0]['text']['format']['schema']['required']
    assert json.loads(sent[0]['input'][1]['content'])['review_families']==['ICT2022','IFVG']
    if future=='extension':
        assert result['id'].endswith('_8')
        assert result['budget_before']['borrowed_attempts']==0
        assert result['budget_before']['research_limit']==12
    assert r.remote_run(None,pm_authorization=auth,cloud=c)['state'] in ('TEN_MINUTE_COOLDOWN','90_SECOND_COOLDOWN')
    with sqlite3.connect(c.root.parent/'ab_ai_context.sqlite3') as db:assert db.execute('SELECT * FROM reviews').fetchall()==production_before
    with c.db() as db:assert db.execute('SELECT count(*) FROM costs').fetchone()[0]==1

@pytest.mark.parametrize('kind',['gap','ambiguous','target_before_fill','nofill'])
def test_candle_outcomes(c,kind):
    at=ts();r={'validation':{'state':'VALIDATED_SHADOW_PROPOSAL','activation_at':at,'expires_at':at+120,'entry':100,'stop':95,'target':110,'gross_rr':2,'risk_usd_per_mnq':10},'decision':{'plan':{'direction':'LONG'}}}
    bars=[dict(kind='bar',symbol='MNQ',timeframe=1,time=at,high=104,low=101)]
    if kind=='gap':bars[0]['time']+=60
    if kind=='ambiguous':bars[0].update(low=94,high=111)
    if kind=='target_before_fill':bars[0]['high']=111
    if kind=='nofill':bars.extend([dict(bars[0],time=at+60),dict(bars[0],time=at+120)])
    c.observe({'evidence':{str(i):b for i,b in enumerate(bars)},'as_of':at+180})
    result=c.outcome(r)['state']
    assert result=={'gap':'UNKNOWN_CANDLE_GAP','ambiguous':'AMBIGUOUS_ENTRY_BAR','target_before_fill':'SIMULATED_INVALIDATED_BEFORE_FILL','nofill':'SIMULATED_NO_FILL'}[kind]

def test_entry_gap_triggers_review_without_creating_trade(c):
    from test_ict_shadow_research import sample
    for short in (False,True):
        p,_=sample(short);events=c.entry_events(p)
        assert events and events[-1]['direction']==('SHORT' if short else 'LONG')
        assert events[-1]['kind']=='OBSERVED_FVG_FORMATION'
        assert 'entry_price' not in events[-1]


def test_deployed_authorization_never_borrows_production(monkeypatch):
    from pathlib import Path
    import ict_shadow_research as research
    auth=json.loads(Path(research.__file__).with_name('ict_shadow_pm_authorization.json').read_text())
    monkeypatch.setattr(research,'DAY',auth['day_ny'])
    assert auth['scope']=='NYPM_TWO_REMAINING_RESEARCH_CALLS'
    assert auth['max_borrowed_calls']==0
    rows=[dict(id=f'ICT_INDEPENDENT_NYAM_20261030_{i}',status='research_completed',requested_at=ts('2026-10-30T13:35:00')) for i in range(8)]
    assert research.pm_slot(rows,{},auth,ts('2026-10-30T14:50:00'))[:2]==(None,'EIGHT_CALL_CAP')


def test_twelve_call_extension_preserves_history_and_burned_slots(c):
    from ict_shadow_cloud import daily_call_limit
    at=ts('2026-10-30T15:15:00')
    rows=[dict(id='ICT_'+str(i),status='research_completed',requested_at=ts('2026-10-30T10:00:00') if i<6 else ts('2026-10-30T13:35:00')) for i in range(8)]
    p={'as_of':at,'trigger_events':[{'available_at':at-60,'kind':'MSS'}]}
    assert c.select(rows,p,at)[0]==8
    c.reserve('ICT_8',payload(),at)
    assert c.select(rows,p,at)[0]==9
    c.reserve('ICT_11',payload(),at)
    assert c.select(rows,p,at)[1]=='DAILY_CALL_CAP_INCLUDING_BURNED'
    rows += [dict(id='ICT_'+str(i),status='research_completed',requested_at=at-3600) for i in range(8,12)]
    assert c.select(rows,p,at)[1]=='DAILY_CALL_CAP'
    assert daily_call_limit('2026-10-30')==12
    assert daily_call_limit('2026-11-02')==16
    assert c.select([],p,ts('2026-10-30T16:00:00'))[1]=='OUTSIDE_SESSION'


@pytest.mark.parametrize('patch',[
    {'finished':None},{'finished':float('nan')},{'finished':float('inf')},
    {'finished':1001},{'finished':899},{'finished':True},
    {'requested_at':None},{'requested_at':0},{'requested_at':'900'},
    {'error_code':None},{'error_code':''},{'error_code':' '},
])
def test_incomplete_production_failure_evidence_still_blocks(patch):
    import ict_shadow_research as r
    row=dict(status='unknown',requested_at=900,finished=950,error_code='TimeoutError')
    row.update(patch)
    assert r.production_defer_reason([row],1000,independent_cloud=True)=='DEFER_PRODUCTION_AI_UNRESOLVED'


def test_finished_failure_is_not_running_or_refunded_and_legacy_stays_strict():
    import ict_shadow_research as r
    row=dict(status='unknown',requested_at=900,finished=950,error_code='TimeoutError')
    before=dict(row)
    assert r.production_defer_reason([row],1000,independent_cloud=True) is None
    assert r.production_defer_reason([row],1000)=='DEFER_PRODUCTION_AI_UNRESOLVED'
    assert r.production_defer_reason([row,dict(row,status='running')],1000,independent_cloud=True)=='DEFER_PRODUCTION_AI_BUSY'
    assert row==before
