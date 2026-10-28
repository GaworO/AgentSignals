"""Health wording must follow the route without granting entry permission."""
import json
from unittest.mock import patch

import pytest
import guardrails as guard
import execution_policy
import execution_validation as validation
from test_builder50_profile_repair import ENV


@pytest.mark.parametrize('mode,cap,sibling,over_cap',[
    ('V3_ONLY',40,False,False),('V3_ONLY',41,False,True),
    ('LEGACY',40,True,False),('AB_ONLY',40,True,False),
    ('AB_ONLY',20,False,False),('SHADOW_ALL',40,False,False)])
def test_account_quantity_warning_matches_executable_family(mode,cap,sibling,over_cap):
    with patch.dict('os.environ',dict(ENV,EXEC_STRATEGY_POLICY=mode,EXEC_MAX_QTY=str(cap)),clear=True):
        profile=guard.account_profile()
        assert any('sibling' in w for w in profile['config_warnings'])==sibling
        assert any('EXEC_MAX_QTY exceeds' in w for w in profile['config_warnings'])==over_cap
        if mode=='V3_ONLY':
            assert 'Directional' in profile['execution_label']
            assert 'A/B shadow' in profile['execution_label']
            assert execution_policy.shadow_family('A/B')


def test_loaded_unapproved_policy_is_explained_but_still_rejected(tmp_path,monkeypatch):
    path=tmp_path/'policy.json'
    path.write_text(json.dumps(dict(approved=False,execution_model='RESTING_LIMIT_CLOSED_M1_V1')))
    monkeypatch.setenv('DIRECTIONAL_EXECUTION_POLICY_FILE',str(path))
    result=validation.status()
    assert 'loaded' in result['policy_detail']
    assert 'technical verification incomplete' in result['policy_detail']
    assert not result['ready'] and not result['policy_validated']
    with pytest.raises(ValueError,match='validated_execution_policy_missing'):validation.policy()


def test_missing_policy_is_not_reported_as_loaded(monkeypatch):
    monkeypatch.delenv('DIRECTIONAL_EXECUTION_POLICY_FILE',raising=False)
    result=validation.status()
    assert result['policy_detail']=='validated_execution_policy_missing'
    assert not result['ready']


def test_health_coalesces_same_policy_dependency_without_going_green(monkeypatch):
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    monkeypatch.setenv('AB_V3_MODE','LIVE');monkeypatch.setenv('AB_DIRECTIONAL_LIVE_LONG','1')
    monkeypatch.setattr(execution_policy,'status',lambda:dict(live_execution_enabled=True,
        execution_kill_switch=False,entry_armed=True,revalidation=dict(ready=False,
        policy_error='validated_execution_policy_missing',policy_detail='policy loaded but unverified',
        access_blockers=['native_bracket_execution_policy_unavailable','account_unknown'],missing_validators=[])))
    result=execution_policy.directional_health()
    assert not result['ready']
    assert result['blockers']==['policy loaded but unverified','account_unknown']


@pytest.mark.parametrize('reason,name', [('costs_pending','broker_accounting'),('protection_unknown','broker_recovery')])
def test_accounting_and_protective_recovery_remain_critical(monkeypatch,reason,name):
    monkeypatch.setattr(guard.builder_crosstrade,'selected',lambda:True)
    monkeypatch.setattr(guard.builder_crosstrade,'status',lambda:dict(
        entry_blocker='broker:native_order_or_cost_reconciliation_pending',
        operator_alerts=[dict(order_id='old-closed-trade',reason=reason)]))
    monkeypatch.setattr(guard.builder_broker_sync,'state',lambda:dict(enabled=True,current=True,open_positions=0,working_orders=0))
    monkeypatch.setattr(guard,'_health_alert',lambda *a,**k:None)
    monkeypatch.setattr(guard,'_set_state',lambda *a,**k:None)
    result=guard.health(market_open=False)
    check=next(c for c in result['checks'] if c['name']==name)
    assert check['level']=='critical' and not check['ok']
    if reason=='costs_pending':assert 'Closed-trade costs/cash' in check['detail']
