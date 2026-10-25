"""Lifecycle foundation only: synthetic broker proofs, no network/model/order calls."""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import directional_nofill_recovery as recovery

T=1792419480000
@pytest.fixture
def case(tmp_path):
    path=tmp_path/'shared_ai.sqlite3'
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE reviews(id TEXT PRIMARY KEY,day TEXT,started REAL,cutoff INTEGER,status TEXT,request TEXT,review_kind TEXT,finished REAL)')
    db.execute('CREATE TABLE setup_requests(order_id TEXT PRIMARY KEY,state TEXT,expires REAL)')
    recovery.init(db)
    order=dict(strategy='AB_DIRECTIONAL',order_id='original',activation_ms=T,expiry_ms=T+600000)
    recovery.register(db,order,T);db.commit()
    yield db,path,order
    db.close()


def proof(now,**kw):
    return dict(order_id='original',observed_ms=now,verified=True,entry_quantity=0,positions=[],working_orders=[],
        broker_ids=dict(orderId=1,oso1Id=2,oso2Id=3),state='NO_FILL',entry_status='Canceled',no_fill_verified_since_ms=T+600000,**kw)


def claim(db,now=T+616000):
    return recovery.reserve_review(db,'original',{'TEST_ONLY':'not_sent'},0,now,config={'AB_DIRECTIONAL_NO_FILL_RECOVERY':'true'})


def test_default_flag_off():assert not recovery.enabled({})

def test_timeline_no_early_review_no_unconfirmed_cancel(case):
    db,_,_=case
    assert recovery.advance(db,'original',proof(T+599999),T+599999)['action']=='NONE'
    p=proof(T+600000);p.update(state='CANCEL_ACK',entry_status='Working')
    assert recovery.advance(db,'original',p,T+600000)['state']=='CANCEL_UNCERTAIN'
    assert not claim(db,T+600000)['allowed']
    assert recovery.advance(db,'original',proof(T+615000),T+615000)['state']=='REVIEW_ELIGIBLE'
    assert claim(db,T+615000)['allowed']
    assert not claim(db,T+615001)['allowed']
    assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==1


@pytest.mark.parametrize('field,value',[('entry_quantity',1),('positions',[{'netPos':1}]),('working_orders',[{'id':1}]),('verified',False),('entry_status','Rejected'),('observed_ms',T),('broker_ids',{'orderId':1})])
def test_any_unresolved_broker_evidence_prevents_review(case,field,value):
    db,_,_=case;now=T+616000;p=proof(now);p[field]=value
    assert recovery.advance(db,'original',p,now)['state']!='REVIEW_ELIGIBLE'
    assert not claim(db,now)['allowed']


@pytest.mark.parametrize('at',[T+1200000,T+1800000])
def test_hard_twenty_minute_deadline(case,at):
    db,_,_=case
    assert recovery.advance(db,'original',proof(at),at)['state']=='EXPIRED'
    assert not claim(db,at)['allowed']


def test_late_partial_fill_after_deadline_preserves_position(case):
    db,_,_=case;now=T+1201000;p=proof(now);p['entry_quantity']=1;p['positions']=[{'netPos':1}]
    x=recovery.advance(db,'original',p,now)
    assert x['action']=='PRESERVE_PROTECTION_RECONCILE' and x['state']=='EXPOSURE'
    assert not claim(db,now)['allowed']


@pytest.mark.parametrize('decision',['WAIT','CANCEL','REJECTED','REVIEW_UNKNOWN'])
def test_non_allow_is_terminal_no_third_review(case,decision):
    db,_,_=case;now=T+616000;recovery.advance(db,'original',proof(now),now);r=claim(db,now)
    recovery.finish_without_entry(db,'original',r['review_id'],decision,now+1)
    recovery.advance(db,'original',proof(now+2),now+2)
    assert not claim(db,now+2)['allowed']


def test_restart_never_resends_outstanding_review(case):
    db,path,_=case;now=T+616000;recovery.advance(db,'original',proof(now),now);assert claim(db,now)['allowed'];db.commit()
    with sqlite3.connect(path) as restarted:
        restarted.row_factory=sqlite3.Row;recovery.init(restarted);restarted.execute('BEGIN IMMEDIATE')
        assert not claim(restarted,now+1)['allowed']
        assert restarted.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==1


def test_duplicate_original_and_mutated_geometry(case):
    db,_,o=case;assert recovery.register(db,o,T+1) is False
    with pytest.raises(ValueError):recovery.register(db,dict(o,entry_price=200),T+2)
    with pytest.raises(ValueError):recovery.register(db,dict(o,strategy='A/B'),T+2)
    with pytest.raises(ValueError):recovery.register(db,dict(o,recovery_version=2),T+2)


def test_original_review_has_priority(case):
    db,_,_=case;now=T+616000;recovery.advance(db,'original',proof(now),now)
    db.execute('INSERT INTO setup_requests VALUES(?,?,?)',('fresh','queued',now/1000+60))
    assert claim(db,now)['reason']=='ORIGINAL_SETUP_PRIORITY'
    assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==0


@pytest.mark.parametrize('kinds', [['SETUP']*7,['CONTEXT']*3+['SETUP']*7+['POSITION']*2])
def test_shared_budget_and_seven_setup_allocation(case,kinds):
    db,_,_=case;now=T+616000;recovery.advance(db,'original',proof(now),now)
    day=datetime.fromtimestamp(now/1000,ZoneInfo('America/New_York')).date().isoformat()
    for i,k in enumerate(kinds):db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',(str(i),day,0,0,'validated','{}',k,1))
    assert not claim(db,now)['allowed']
    assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==len(kinds)


def test_recovery_consumes_same_setup_bucket(case):
    db,_,_=case;now=T+616000;recovery.advance(db,'original',proof(now),now)
    assert claim(db,now)['allowed']
    assert db.execute('SELECT review_kind FROM reviews').fetchone()[0]=='SETUP'
