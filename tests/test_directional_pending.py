"""Pending eligibility tests; mocked feed/AI, real account sizing/limits."""
import json
import pytest
from unittest.mock import Mock
from test_directional_risk_revalidation import native
import directional_pending as pending

@pytest.fixture
def validator(native,monkeypatch):
    f=native
    review=dict(review_id='MOCK',expires_at=f.now+120)
    f.order['ai_review_at_native_dispatch']=review;f.prepare()
    monkeypatch.setattr(pending.directional_ai_review,'check',lambda *a:(None,dict(review)))
    monkeypatch.setattr(pending.ab_v3_live,'m1_fresh',lambda *a:True)
    monkeypatch.setattr(pending,'ThesisValidator',lambda **kw:lambda *a,**kw:dict(valid=True))
    v=pending.PendingValidator(signal_builder=lambda x:f.signal,budget_builder=lambda profile:250.,feed_age=lambda:0,market_open=lambda:True,
        news_flags=lambda x:([],False),calendar_age=lambda:.1,clock=lambda:f.now)
    return f,v

def test_owned_pending_orders_do_not_self_block_risk_or_mutate_snapshot(validator):
    f,v=validator;s=f.snapshot(orders=[dict(id=11),dict(id=12),dict(id=13)])
    assert v(f.order,s,{11,12,13}) is None
    assert len(s['orders'])==3
    assert f.guard_check.call_args.kwargs['_pending_owned'] is True

@pytest.mark.parametrize('change,expected',[
 ('foreign','pending_foreign_order'),('position','pending_account_exposure'),
 ('stale','pending_account_stale'),('risk','mffu_projected_drawdown_breach'),
 ('guard','news_window'),('geometry','pending_geometry_changed'),('expiry','pending_setup_expired')])
def test_pending_changes_cancel(validator,change,expected):
    f,v=validator;s=f.snapshot()
    if change=='foreign':s['orders']=[dict(id=999)]
    if change=='position':s['positions']=[dict(contractId=100,netPos=1)]
    if change=='stale':s['read_started_at']-=4
    if change=='risk':s['account']['balance'].update(totalCashValue=48050.,netLiq=48050.,cashSOD=48050.)
    if change=='guard':f.guard_check.return_value=(False,'news_window')
    if change=='geometry':f.order=dict(f.order,entry_price=30001.)
    if change=='expiry':f.now+=700
    assert v(f.order,s,set())==expected

def test_candle_staleness_real_watchdog(validator,monkeypatch):
    f,v=validator;monkeypatch.setattr(pending.ab_v3_live,'m1_fresh',lambda *a:False)
    assert v(f.order,f.snapshot(),set())=='pending_m1_not_fresh'
