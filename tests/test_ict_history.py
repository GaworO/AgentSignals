import hashlib,json,sqlite3
from datetime import datetime
import pytest
from ict_history import NY,bounds,build,broker_records,session

def ts(s):return datetime.fromisoformat(s).replace(tzinfo=NY).timestamp()
NOW=ts('2026-11-03T09:00:00')
def review(rid,at,decision='WAIT',family='ICT2022'):
    return dict(id=rid,requested_at=ts(at),state='research_completed',family=family,decision={'decision':decision,'plan':None},validation={'state':'WAIT'})
def cost(rid,at,amount=.2):return dict(id=rid,at=ts(at),amount=amount,reserve=1)
def order(created,closed=None,net=None,state='BLOCKED'):
    return dict(created_at=ts(created),filled_at=ts(created) if closed else None,closed_at=ts(closed) if closed else None,
                filled=bool(closed),closed=bool(closed),costs_reconciled=net is not None,net_usd=net,acknowledged=bool(closed),state=state)

def test_previous_day_both_sessions_and_deduplicated_call():
    rows=[review('a','2026-11-02T09:35:00'),review('a:IFVG','2026-11-02T09:35:00',family='IFVG'),review('b','2026-11-02T15:40:00')]
    rows[1]['parent_review_id']='a'
    h=build({'now':NOW,'reviews':rows},[cost('a','2026-11-02T09:35:00'),cost('b','2026-11-02T15:40:00')],[],[],'day','2026-11-02','ALL')
    assert h['summary']['calls']==2 and h['summary']['assessments']==3 and h['summary']['no_trade']==3
    assert h['summary']['estimated_usd']==pytest.approx(.4)
    pm=build({'now':NOW,'reviews':rows},[],[],[],'day','2026-11-02','NYPM')
    assert len(pm['reviews'])==1 and pm['reviews'][0]['id']=='b'

def test_previous_month_week_dst_and_future_rejected():
    assert bounds('week','2026-11-02',NOW)==('2026-11-02','2026-11-03')
    assert bounds('month','2026-10-30',NOW)==('2026-10-01','2026-10-31')
    assert bounds('week','2026-10-30',NOW)==('2026-10-26','2026-11-01')
    assert session(ts('2026-11-02T09:30:00'))=='NYAM'
    assert session(ts('2026-11-02T16:00:00'))=='OTHER'
    with pytest.raises(ValueError):bounds('day','2026-11-04',NOW)

def test_actual_pnl_uses_close_day_not_proposal_day_or_shadow():
    rows=[review('s','2026-11-02T10:00:00')];rows[0]['outcome']={'state':'SIMULATED_TARGET','gross_usd_per_mnq':9999}
    orders=[order('2026-10-30T15:50:00','2026-11-02T09:40:00',80,'WIN'),order('2026-11-02T10:00:00','2026-11-02T10:10:00',-20,'LOSS')]
    h=build({'now':NOW,'reviews':rows},[],orders,[],'day','2026-11-02','ALL')
    assert h['summary']['net_usd']==60 and h['summary']['profit_factor']==4
    assert h['summary']['broker_fills']==1 and len(h['broker_orders'])==2

def test_unreconciled_costs_keep_total_unknown():
    orders=[order('2026-11-02T09:40:00','2026-11-02T09:50:00',80,'WIN'),order('2026-11-02T10:00:00','2026-11-02T10:10:00',None,'CLOSED_COSTS_PENDING')]
    h=build({'now':NOW,'reviews':[]},[],orders,[],'day','2026-11-02','ALL')
    a=h['summary'];assert a['known_net_usd']==80 and a['net_usd'] is None and a['profit_factor'] is None
    assert a['closed_costs_pending']==1

def test_empty_days_no_fake_review_or_pnl():
    h=build({'now':NOW,'reviews':[]},[],[],[],'week','2026-11-02','ALL')
    assert len(h['daily'])==2 and h['summary']['calls']==0 and h['summary']['net_usd'] is None

def test_orphan_cost_is_unknown_response_not_invented_ai_decision():
    h=build({'now':NOW,'reviews':[]},[cost('burned','2026-11-02T10:00:00',None)],[],[],'day','2026-11-02','ALL')
    assert h['summary']['calls']==1 and h['summary']['failed_reviews']==1
    assert h['reviews'][0]['decision'] is None and h['reviews'][0]['record_basis']=='COST_RECORD_ONLY'

def test_no_loss_pf_is_undefined_even_with_breakeven():
    h=build({'now':NOW,'reviews':[]},[],[order('2026-11-02T10:00:00','2026-11-02T10:05:00',0,'BREAKEVEN')],[],'day','2026-11-02','ALL')
    assert h['summary']['profit_factor'] is None and h['summary']['pf_reason']=='NO_LOSSES'

def test_more_than_100_old_decisions_preserved():
    rows=[review(str(i),'2026-11-02T10:00:00') for i in range(130)]
    h=build({'now':NOW,'reviews':rows},[],[],[],'all','2026-11-03','ALL')
    assert len(h['reviews'])==130 and h['summary']['calls']==130
    assert h['summary']['cost_records_missing']==130

def test_missing_broker_db_is_unknown_and_not_created(tmp_path):
    rows,gaps=broker_records({'DATA_DIR':str(tmp_path)})
    assert not rows and gaps==['ICT_DISPATCH_HISTORY_UNAVAILABLE'] and not list(tmp_path.iterdir())

def test_native_receipt_never_counts_as_fill_and_no_limit_100(tmp_path):
    config={'DATA_DIR':str(tmp_path),'BUILDER_BROKER_ACCOUNT_ID':'1','BUILDER_BROKER_ACCOUNT_NAME':'a','BUILDER_BROKER_ENVIRONMENT':'demo'}
    binding=hashlib.sha256(json.dumps(['1','a','demo'],separators=(',',':')).encode()).hexdigest()
    with sqlite3.connect(tmp_path/'continuation_live.sqlite3') as db:
        db.execute('CREATE TABLE live_dispatches(order_id,state,guard_reason,direction,quantity,activation_ms,source_json,created_at,updated_at,strategy)')
        for i in range(120):db.execute('INSERT INTO live_dispatches VALUES(?,?,?,?,?,?,?,?,?,?)',(str(i),'SENT',None,'LONG',2,0,json.dumps({'ict_review_id':'r'+str(i)}),'2026-11-02T15:00:00+00:00','2026-11-02T15:00:00+00:00','ICT_AI'))
    with sqlite3.connect(tmp_path/'ab_v3.sqlite3') as db:
        db.execute('CREATE TABLE native_orders(binding,order_id,state,receipt_json,observation_json,updated_ms)')
        db.execute('CREATE TABLE orders(order_id,position_json)')
        db.execute('INSERT INTO native_orders VALUES(?,?,?,?,?,?)',(binding,'0','WAITING_BROKER_FILL','{"orderId":123}','{}',0))
        obs={'entry_quantity':2,'exit_quantity':2,'owned_fill_ids':[1,2],'exit_fill_ms':ts('2026-11-02T10:05:00')*1000,'costs_reconciled':True,'net_usd':75,'captured_cost_usd':5,'fees_final':False}
        db.execute('INSERT INTO native_orders VALUES(?,?,?,?,?,?)',(binding,'1','CLOSED','{"orderId":124}',json.dumps(obs),0))
        db.execute('INSERT INTO orders VALUES(?,?)',('1',json.dumps({'fill_ms':ts('2026-11-02T10:00:00')*1000})))
    before={p:p.read_bytes() for p in tmp_path.iterdir()}
    rows,gaps=broker_records(config);assert len(rows)==120 and not gaps
    ack=next(r for r in rows if r['order_id']=='0');assert ack['acknowledged'] and not ack['filled'] and ack['net_usd'] is None
    closed=next(r for r in rows if r['order_id']=='1');assert closed['filled'] and closed['net_usd']==75 and not closed['fees_final']
    assert all(p.read_bytes()==b for p,b in before.items())

def test_history_authenticated_and_get_only(tmp_path,monkeypatch):
    from flask import Flask
    import base64,ict_shadow_cloud,ict_history
    app=Flask(__name__);ict_shadow_cloud.register(app,{'DATA_DIR':str(tmp_path),'TANJA_DASHBOARD_PASSWORD':'test'},start=False)
    monkeypatch.setattr(ict_history,'collect',lambda *a:dict(summary={'calls':0}))
    c=app.test_client();headers={'Authorization':'Basic '+base64.b64encode(b'tanja:test').decode()}
    assert c.get('/ict-shadow/history').status_code==401
    assert c.post('/ict-shadow/history',headers=headers).status_code==405
    r=c.get('/ict-shadow/history',headers=headers);assert r.status_code==200 and r.headers['Cache-Control']=='no-store'
