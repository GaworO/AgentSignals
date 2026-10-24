"""Synthetic evidence-schema tests, NOT live broker verification."""
import hashlib

import pytest

import ab_v3_live as v3
import builder_broker_sync as sync
import execution_validation as execution


@pytest.fixture
def proof(monkeypatch):
    identity=['123','MFFU_TEST_ONLY','demo']
    for key,value in zip(('BUILDER_BROKER_ACCOUNT_ID','BUILDER_BROKER_ACCOUNT_NAME',
                          'BUILDER_BROKER_ENVIRONMENT'),identity):
        monkeypatch.setenv(key,value)
    monkeypatch.setenv('AB_V3_CONTRACT','MNQZ2026')
    monkeypatch.setenv('EXEC_MAX_QTY','1')
    p=dict(verified=True,simulator_only=False,provider='CrossTrade',surface='tradovate',
           contract='MNQZ2026',evidence_kind='ACCOUNT_BROKER_HISTORY',
           account_binding=hashlib.sha256(sync.encode(identity).encode()).hexdigest(),
           evidence_reference='SYNTHETIC_UNIT_FIXTURE_ONLY',broker_order_ids=[1,2,3],max_quantity=1,
           native_oso_oco=True,partial_fill_repair=True,restart_recovery=True,net_accounting=True)
    monkeypatch.setattr(execution,'policy',lambda:dict(broker_workflow_verification=p))
    return p


def test_history_can_be_scoped_without_separate_paper(proof):
    assert 'paper_evidence_reference' not in proof
    assert v3.native_bracket_readiness() is None


@pytest.mark.parametrize('field,value',[
    ('verified',False),('simulator_only',True),('native_oso_oco',False),
    ('partial_fill_repair',False),('restart_recovery',False),('net_accounting',False),
    ('contract','MNQH2027'),('provider','other')])
def test_history_does_not_waive_any_protection_check(proof,field,value):
    proof[field]=value
    assert v3.native_bracket_readiness()=='native_bracket_broker_verification_missing'


@pytest.mark.parametrize('field,value',[
    ('account_binding','different'),('evidence_reference',''),('broker_order_ids',[]),
    ('broker_order_ids',[1,1,2]),('broker_order_ids',[True,2,3]),
    ('max_quantity',2),('max_quantity',True)])
def test_history_must_bind_account_and_one_contract_scope(proof,field,value):
    proof[field]=value
    assert v3.native_bracket_readiness()=='native_bracket_history_scope_unverified'


@pytest.mark.parametrize('cap',['0','2','15','auto',''])
def test_pilot_evidence_cannot_authorize_larger_configured_positions(proof,monkeypatch,cap):
    monkeypatch.setenv('EXEC_MAX_QTY',cap)
    assert v3.native_bracket_readiness()=='native_bracket_history_scope_unverified'


def test_unknown_evidence_kind_is_not_a_paper_fallback(proof):
    proof.update(evidence_kind='simulation',paper_evidence_reference='fake')
    assert v3.native_bracket_readiness()=='native_bracket_evidence_kind_unsupported'
