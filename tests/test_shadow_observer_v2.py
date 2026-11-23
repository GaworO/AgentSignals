import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from copy import deepcopy
import pytest
from services.tanja.ict_agent.scenarios import update_setups
from services.tanja.ict_agent.shadow_sessions import session_at,window_checks,correlation
from services.tanja.ict_agent.shadow_chart import build_chart,followup_due

class K:
    def retrieve(self,*a,**kw):return {'records':[],'audit_notes':[]}

def test_three_instruments_never_reuse_nq_price_for_mnq_or_es():
    ctx={s:{'contract':s+'-test','stale':False,'ranges':{},'latest':{'close':p,'ts':100,'id':s+'-bar'}} for s,p in [('NQ',100),('MNQ',200),('ES',50)]}
    events=[dict(id=s+'-sweep',symbol=s,contract=s+'-test',available_at=100,direction='LONG',price=p+1,extreme=p-1,kind='SWEEP',timeframe=5) for s,p in [('NQ',100),('MNQ',200),('ES',50)]]
    setups={};update_setups(setups,events,ctx,100,K(),('NQ','MNQ','ES'))
    assert {s['symbol'] for s in setups.values()}=={'NQ','MNQ','ES'}
    ctx['NQ']['latest']['close']=98
    update_setups(setups,[],ctx,160,K(),('NQ','MNQ','ES'))
    assert next(s for s in setups.values() if s['symbol']=='NQ')['state']=='INVALIDATED'
    assert all(s['state']=='WAITING_FOR_CONFIRMATION' for s in setups.values() if s['symbol']!='NQ')
    assert correlation({'symbol':'NQ'})==correlation({'symbol':'MNQ'})
    assert not correlation({'symbol':'ES'})['independent_opportunity']

@pytest.mark.parametrize('date,utc_hour',[('2026-03-07',8),('2026-03-09',7),('2026-10-31',7),('2026-11-02',8)])
def test_ny_dst_and_lo_source_window(date,utc_hour):
    at=int(datetime.fromisoformat(date+'T03:15:00').replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
    assert datetime.fromtimestamp(at,ZoneInfo('UTC')).hour==utc_hour
    assert session_at(at)=='LO'
    assert next(w for w in window_checks(at) if w['model']=='SILVER_BULLET_LO')['status']=='IN_WINDOW_TIME_ONLY'

def test_after_window_retest_cannot_become_entry():
    at=int(datetime.fromisoformat('2026-05-20T11:03:00').replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
    checks=window_checks(at,at-3600)
    assert session_at(at)=='NYAM'
    for model in ('CHARTER_AM','SILVER_BULLET_NYAM'):
        w=next(w for w in checks if w['model']==model)
        assert w['status']=='OUTSIDE_ENTRY_WINDOW' and not w['source_qualified']

def test_real_saved_chart_and_unconfirmed_sweep(tmp_path):
    path=os.environ.get('ICT_TEST_REPLAY_DB')
    if not path:pytest.skip('Requires verified prior real replay via ICT_TEST_REPLAY_DB')
    db=sqlite3.connect('file:'+path+'?mode=ro',uri=True)
    row=db.execute("SELECT payload FROM shadow_records WHERE candidate_id='a2830cb600d77a5db4c71b61' AND stage='ASSESSMENT' ORDER BY as_of DESC LIMIT 1").fetchone()
    a=json.loads(row[0]);p=json.loads(db.execute('SELECT payload FROM shadow_evidence WHERE id=?',(a['evidence_packet_id'],)).fetchone()[0]);s=p['snapshot']['setups'][0];ctx=p['snapshot']['context'];at=p['snapshot']['as_of']
    c=build_chart(db,p,s,ctx,at)
    assert {x['kind'] for x in c['annotations']}>={'SWEEP','LIQUIDITY_REFERENCE','DISPLACEMENT','STRUCTURE_BREAK','FVG','RETEST'}
    assert all(x['available_at']<=at for bars in c['frames'].values() for x in bars)
    assert all(x['observation_id'] in p['evidence'] for x in c['annotations'])
    assert c['entry'] is c['stop'] is c['preferred_target'] is None
    assert all(t['source_preference']=='UNRESOLVED' for t in c['targets'])
    broken=deepcopy(p);root=broken['evidence'][s['root_event']]
    candle=next(broken['evidence'][i] for i in root['evidence_ids'] if 'close' in broken['evidence'][i]);candle['close']=root['price']-1
    bad=build_chart(db,broken,s,ctx,at)
    assert next(a for a in bad['annotations'] if a['kind']=='SWEEP')['status']=='SWEEP_UNCONFIRMED'
    future=deepcopy(p);future['evidence'][s['root_event']]['available_at']=at+60
    with pytest.raises(ValueError,match='FUTURE'):build_chart(db,future,s,ctx,at)

def test_lifecycle_continues_after_selector_expiry():
    s={'symbol':'MNQ','state':'EXPIRED'};old={'chart_state':{'zones':[{'id':'own-zone','lower':100,'upper':101,'available_at':100}], 'retested':[],'inverted':[]}}
    ctx={'MNQ':{'latest':{'available_at':10000,'ts':9940,'low':100.5,'high':102}}}
    assert followup_due(s,old,ctx,[],10000)
    old['chart_state']['retested']=['own-zone'];assert not followup_due(s,old,ctx,[],10000)
