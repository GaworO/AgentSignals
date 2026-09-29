import os
import unittest
import time
from unittest.mock import patch

import execution_policy as policy


def signal(direction='LONG'):
    return dict(model='A/B Directional', _strat='A/B Directional '+direction,
                _v3_directional=True, _continuation_order_id='TEST-ONLY', dir=direction,
                _v3_expiry_ms=int(time.time()*1000)+600000,
                entry=100., SL=90. if direction=='LONG' else 110.,
                TP=120. if direction=='LONG' else 80.,
                _strict_risk_budget=True,_disable_partial=True,_risk_budget_usd=250.)


def env():
    return dict(EXEC_STRATEGY_POLICY='V3_ONLY', V3_ENTRY_ARMED='1',EXEC_TICKER='MNQZ2026',
                GUARD_TOKEN='unit-test-only-not-a-real-secret-12345',
                AB_V3_CONTRACT='MNQZ2026',AB_V3_DETECTOR_CONTRACT_VERIFIED='1',
                AB_V3_EXCLUSIVE_ROUTE='1',EXEC_TICK='0.25',PRICE_OFFSET='0',POINT_VALUE='2',
                ACCOUNT_PLAN='builder50',EXEC_FX='0')


class PermissionTests(unittest.TestCase):
    def test_both_directions_same_permission(self):
        with patch.dict(os.environ,env()):
            for direction in ('LONG','SHORT'):
                self.assertIsNone(policy.entry_blocker(signal(direction)))

    def test_old_strategies_are_shadow_even_if_their_flags_are_live(self):
        with patch.dict(os.environ,env()):
            for strategy in ('A/B','A/B-shallow','DOL_DELIVERY_REVERSAL','Continuation LONG','Continuation SHORT'):
                self.assertEqual(policy.entry_blocker(dict(signal(),_strat=strategy)), 'shadow_strategy:V3_ONLY')

    def test_default_arm_is_off(self):
        with patch.dict(os.environ,dict(env(),V3_ENTRY_ARMED='0')):
            self.assertEqual(policy.entry_blocker(signal()),'v3_entry_not_armed')

    def test_unknown_policy_fails_closed(self):
        with patch.dict(os.environ,dict(env(),EXEC_STRATEGY_POLICY='TYPO')):
            self.assertEqual(policy.entry_blocker(signal()),'execution_policy_invalid')

    def test_provenance_boolean_alone_cannot_promote_old_ab(self):
        with patch.dict(os.environ,env()):
            for change in ({'_continuation_order_id':None},{'model':'reversal'},{'_v3_directional':1}):
                self.assertEqual(policy.entry_blocker(dict(signal(),**change)),'shadow_strategy:V3_ONLY')

    def test_data_mapping_and_route_are_required(self):
        variants=[('GUARD_TOKEN','','v3_guard_admin_token_required'),
                  ('EXEC_TICKER','MNQ1!','v3_explicit_mnq_contract_required'),
                  ('AB_V3_CONTRACT','MNQU2026','v3_detector_broker_contract_mismatch'),
                  ('AB_V3_DETECTOR_CONTRACT_VERIFIED','0','v3_detector_contract_unverified'),
                  ('AB_V3_EXCLUSIVE_ROUTE','0','v3_entry_route_unconfirmed'),
                  ('PRICE_OFFSET','1','v3_execution_geometry_config_mismatch'),
                  ('POINT_VALUE','20','v3_execution_geometry_config_mismatch')]
        for key,value,reason in variants:
            with self.subTest(key=key),patch.dict(os.environ,dict(env(),**{key:value})):
                self.assertEqual(policy.entry_blocker(signal()),reason)

    def test_geometry_partials_and_risk_cannot_change(self):
        with patch.dict(os.environ,env()):
            for change in ({'TP':125.},{'entry':100.1},{'SL':100.},{'_disable_partial':False},
                           {'_strict_risk_budget':False},{'_risk_budget_usd':251.},{'TP':float('nan')}):
                with self.subTest(change=change):
                    self.assertIsNotNone(policy.entry_blocker(dict(signal(),**change)))

    def test_shadow_all_overrides_v3_armed(self):
        with patch.dict(os.environ,dict(env(),EXEC_STRATEGY_POLICY='SHADOW_ALL')):
            self.assertEqual(policy.entry_blocker(signal()),'shadow_strategy:SHADOW_ALL')

    def test_legacy_mode_is_backwards_compatible(self):
        with patch.dict(os.environ,dict(env(),EXEC_STRATEGY_POLICY='LEGACY')):
            self.assertIsNone(policy.entry_blocker({'_strat':'A/B'}))

    def test_permission_never_claims_manager_or_fill_is_live(self):
        with patch.dict(os.environ,env()):
            self.assertFalse(policy.status()['active_manager_live'])
            self.assertFalse(policy.status()['broker_fill_proven_by_http'])


if __name__=='__main__': unittest.main()
