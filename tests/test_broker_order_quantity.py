import copy
import pytest
import ab_v3_live as live
import execution_validation as gate

def proof():
    return dict(evidence_kind='ACCOUNT_HISTORY_CONTROLLED_PILOT',max_quantity=5,
                quantity_scope_verified=True,observed_protected_quantity=5,
                quantity_scope_reference='SYNTHETIC_5_CONTRACT_BRACKET')

@pytest.mark.parametrize('maximum',[5,15,40])
def test_account_capacity_does_not_replace_actual_order_quantity(monkeypatch,maximum):
    monkeypatch.setenv('EXEC_MAX_QTY',str(maximum));p=proof()
    assert live.broker_quantity_scope(p)
    assert live.broker_quantity_scope(p,1)
    assert live.broker_quantity_scope(p,5)
    assert not live.broker_quantity_scope(p,6)
    assert live.native_order_quantity_blocker({'quantity':4},{'broker_workflow_verification':p}) is None

@pytest.mark.parametrize('quantity',[None,True,0,-1,1.5,'1',6,40])
def test_unknown_or_unsupported_quantity_blocks(monkeypatch,quantity):
    monkeypatch.setenv('EXEC_MAX_QTY','40')
    assert live.native_order_quantity_blocker({'quantity':quantity},{'broker_workflow_verification':proof()})=='native_order_quantity_scope_unverified'

@pytest.mark.parametrize('key,value',[
    ('quantity_scope_verified',False),('quantity_scope_reference',''),
    ('observed_protected_quantity',4),('max_quantity',40),('max_quantity',True)])
def test_evidence_cannot_be_extended_by_configuration(monkeypatch,key,value):
    monkeypatch.setenv('EXEC_MAX_QTY','40');p=proof();p[key]=value
    assert not live.broker_quantity_scope(p)

def test_lower_operator_limit_still_applies(monkeypatch):
    monkeypatch.setenv('EXEC_MAX_QTY','3')
    assert live.broker_quantity_scope(proof(),3)
    assert not live.broker_quantity_scope(proof(),4)

def test_final_gate_checks_quantity_before_any_submission_sources(monkeypatch):
    monkeypatch.setenv('EXEC_MAX_QTY','40')
    monkeypatch.setattr(gate,'policy_for',lambda _:dict(broker_workflow_verification=proof()))
    monkeypatch.setattr(gate.execution_trace,'record',lambda *a,**k:None)
    monkeypatch.setattr(gate,'_SOURCES',{'risk':lambda _:pytest.fail('must reject before downstream actions')})
    assert gate.check({'order_id':'synthetic','quantity':6},None)=='native_order_quantity_scope_unverified'
