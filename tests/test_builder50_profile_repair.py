import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import guardrails as g


ENV = dict(ACCOUNT_PLAN='builder50', ACCOUNT_LABEL='Builder 50K', ACCOUNT_PHASE='evaluation',
           ACCOUNT='50000', START_BALANCE='50000', TARGET_BALANCE='53000',
           DD_FLOOR='48161.90', DD_TRAIL_USD='2000', DD_FLOOR_CAP='50100',
           SETUP_GROUP_RISK_USD='250', EXEC_MAX_QTY='15', EXEC_TIF='day',
           ACCOUNT_STARTED_ON='2026-08-30', GUARD_TOKEN='t' * 40,
           EXEC_STRATEGY_POLICY='V3_ONLY', V3_ENTRY_ARMED='0', EXEC_MODE='manual',
           EXEC_WEBHOOK='https://example.invalid/test-only')


class BuilderProfileRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (('DATA_DIR', self.tmp.name),
                            ('GSTATE', str(Path(self.tmp.name) / 'guard_state.json')),
                            ('GLOG', str(Path(self.tmp.name) / 'guard_log.json'))):
            p = mock.patch.object(g, name, value); p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, ENV, clear=True); p.start(); self.addCleanup(p.stop)
        self.before = dict(mode='manual', equity=99000, eq_high=100123, equity_ts=1,
                           sent_total=11, kill=True, kill_hard=True, kill_reason='manual',
                           loss_streak_handled_key='keep-me', loss_streak_handled_count=4,
                           loss_streak_resume_after_ms=9999999999999)
        Path(g.GSTATE).write_text(json.dumps(self.before))
        self.book = [dict(key='old-real-sent', decision='sent', net=-521.60)]
        Path(g.GLOG).write_text(json.dumps(self.book))
        p = mock.patch.object(g, '_day_stats', return_value=dict(net=-13.25, openpos=False))
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(g, 'flatten_all', side_effect=AssertionError('no broker action allowed'))
        p.start(); self.addCleanup(p.stop)
        self.body = dict(equity=49637, floor=48161.90, expected_route_id=g._exec_route_id(),
                         same_broker_account=True, broker_flat=True, no_pending_broker_orders=True,
                         current_mff_snapshot=True, volume_backup_taken=True)

    def read(self): return json.loads(Path(g.GSTATE).read_text())

    def refused(self, changes=None, env=None):
        before = Path(g.GSTATE).read_bytes()
        with mock.patch.dict(os.environ, env or {}, clear=False), self.assertRaises((ValueError, TypeError, KeyError)):
            g.realign_builder_account(dict(self.body, **(changes or {})))
        self.assertEqual(Path(g.GSTATE).read_bytes(), before)
        self.assertEqual(json.loads(Path(g.GLOG).read_text()), self.book)

    def test_current_trailed_floor_is_valid(self):
        self.assertTrue(g.account_profile()['config_ok'])
        self.assertEqual(g.account_profile()['rules']['starting_floor'], 48000)

    def test_100k_floor_and_nonfinite_floor_are_rejected(self):
        for floor in ('96500', 'nan', 'inf', '47999.99', '50100.01'):
            with self.subTest(floor=floor), mock.patch.dict(os.environ, {'DD_FLOOR': floor}):
                self.assertFalse(g.account_profile()['config_ok'])

    def test_repair_preserves_every_nonbaseline_state_field_and_book(self):
        result = g.realign_builder_account(self.body)
        self.assertTrue(result['ok'])
        after = self.read()
        for key in ('mode', 'sent_total', 'kill', 'kill_hard', 'kill_reason',
                    'loss_streak_handled_key', 'loss_streak_handled_count', 'loss_streak_resume_after_ms'):
            self.assertEqual(after[key], self.before[key])
        self.assertEqual(after['equity'], 49637)
        self.assertAlmostEqual(after['eq_high'], 50161.90)
        self.assertEqual(after['equity_day_net_at_sync'], -13.25)
        self.assertEqual(after['equity_sync_day'], g._today())
        self.assertGreater(after['equity_ts'], 1)
        self.assertAlmostEqual(g._dd_floor(), 48161.90)
        self.assertEqual(json.loads(Path(g.GLOG).read_text()), self.book)
        backup = json.loads((Path(g.DATA_DIR) / result['backup']).read_text())
        self.assertEqual(backup['before'], self.before)
        self.assertEqual(backup['after'], after)
        self.assertFalse(result['broker_feedback'])

    def test_refuses_auto_even_if_env_is_manual(self):
        Path(g.GSTATE).write_text(json.dumps(dict(self.before, mode='auto')))
        self.refused()

    def test_refuses_armed_entries(self): self.refused(env={'V3_ENTRY_ARMED': '1'})
    def test_refuses_legacy_policy(self): self.refused(env={'EXEC_STRATEGY_POLICY': 'LEGACY'})
    def test_refuses_wrong_profile(self): self.refused(env={'ACCOUNT_PLAN': 'pro100'})
    def test_refuses_funded_phase(self): self.refused(env={'ACCOUNT_PHASE': 'sim_funded'})
    def test_refuses_weak_token(self): self.refused(env={'GUARD_TOKEN': 'short'})
    def test_refuses_other_route(self): self.refused({'expected_route_id': 'another'})
    def test_refuses_unconfirmed_operator_checks(self):
        for key in ('same_broker_account', 'broker_flat', 'no_pending_broker_orders',
                    'current_mff_snapshot', 'volume_backup_taken'):
            with self.subTest(key=key): self.refused({key: False})

    def test_refuses_pending_setup(self):
        Path(g.GSTATE).write_text(json.dumps(dict(self.before, pending_group={'group_id': 'unresolved'})))
        self.refused()

    def test_refuses_model_open_commitment(self):
        with mock.patch.object(g, '_day_stats', return_value=dict(net=0, openpos=True)):
            self.refused()

    def test_refuses_corrupt_state(self):
        Path(g.GSTATE).write_text('{bad json')
        self.refused()

    def test_refuses_missing_state(self):
        Path(g.GSTATE).unlink()
        with self.assertRaises(ValueError): g.realign_builder_account(self.body)
        self.assertFalse(Path(g.GSTATE).exists())

    def test_refuses_nonfinite_and_wrong_scale_snapshots(self):
        for equity, floor in ((float('nan'), 48161.90), (49637, float('inf')),
                              (99000, 48161.90), (48000, 48161.90), (49637, 48000),
                              (True, 48161.90), (49637, True)):
            with self.subTest(equity=equity, floor=floor): self.refused(dict(equity=equity, floor=floor))

    def test_never_lowers_previously_verified_floor(self):
        Path(g.GSTATE).write_text(json.dumps(dict(self.before, verified_broker_floor=49000)))
        self.refused()

    def test_never_reuses_state_bound_to_other_route(self):
        Path(g.GSTATE).write_text(json.dumps(dict(self.before, account_realignment={'route_id': 'other'})))
        self.refused()

    def test_backup_failure_does_not_touch_state(self):
        before = Path(g.GSTATE).read_bytes()
        with mock.patch.object(g, '_write_repair_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): g.realign_builder_account(self.body)
        self.assertEqual(Path(g.GSTATE).read_bytes(), before)

    def test_endpoint_requires_token_and_post_and_never_arms(self):
        from flask import Flask
        app = g.register(Flask(__name__))
        c = app.test_client()
        self.assertEqual(c.get('/guard/account-realign').status_code, 405)
        self.assertEqual(c.post('/guard/account-realign', json=self.body).status_code, 401)
        self.assertEqual(c.post('/guard/account-realign', json=self.body,
                                headers={'X-Guard-Token': 'wrong'}).status_code, 401)
        response = c.post('/guard/account-realign', json=self.body,
                          headers={'X-Guard-Token': ENV['GUARD_TOKEN']})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['ok'])
        self.assertEqual(self.read()['mode'], 'manual')
        self.assertTrue(self.read()['kill'])


if __name__ == '__main__': unittest.main()
