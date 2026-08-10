import datetime as dt
import os
import tempfile
import unittest
import types
from unittest import mock

import broker_feedback
import ab_shallow
import execution_plan
import guardrails


class ExecutionPlanTests(unittest.TestCase):
    def signal(self, strategy="A/B"):
        return {
            "_strat": strategy,
            "dir": "LONG",
            "entry": 20000.0,
            "SL": 19970.0,
            "TP": 20060.0,
            "_size_mult": 1.5,
            "_select": True,
        }

    def env(self, **extra):
        out = {
            "CHALLENGE_MODE": "1",
            "ACCOUNT": "100000",
            "RISK_PCT": "0.35",
            "POINT_VALUE": "2",
            "EXEC_TICK": "0.25",
            "EXEC_MAX_QTY": "15",
            "DD_SLIPPAGE_PTS": "2",
            "DD_COMMISSION_RT_USD": "1.24",
            "EXEC_QTY": "99",
            "SELECT_SIZE_MULT": "2",
            "GUARD_DYN_RISK": "1",
        }
        out.update(extra)
        return out

    def test_challenge_plan_ignores_every_upsize_path(self):
        plan = execution_plan.build(self.signal(), self.env())
        self.assertEqual(plan["qty"], 5)
        self.assertEqual(plan["stop_risk_usd"], 300.0)
        self.assertAlmostEqual(plan["projected_risk_usd"], 326.2)

    def test_group_risk_sums_final_quantities(self):
        deep = self.signal()
        shallow = self.signal("A/B-shallow")
        shallow["_risk_pct_override"] = 0.35
        execution_plan.attach(deep, self.env())
        execution_plan.attach(shallow, self.env())
        self.assertAlmostEqual(execution_plan.group_risk([deep, shallow]), 652.4)

    def test_rejects_every_non_ab_strategy(self):
        with self.assertRaisesRegex(execution_plan.PlanError, "strategy_not_allowed"):
            execution_plan.build(self.signal("C"), self.env())

    def test_default_profile_is_point_35_each_and_point_70_combined(self):
        deep = self.signal()
        shallow = self.signal("A/B-shallow")
        meta = ab_shallow.risk_metadata(deep, shallow, {
            "ACCOUNT": "100000", "POINT_VALUE": "2"
        })
        self.assertEqual(meta["deep_risk_pct"], 0.35)
        self.assertEqual(meta["shallow_risk_pct"], 0.35)
        self.assertEqual(meta["combined_max_risk_pct"], 0.70)
        self.assertEqual(meta["combined_max_budget"], 700.0)


class BrokerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        broker_feedback.DATA_DIR = self.tmp.name
        broker_feedback.BROKER_STATE = os.path.join(self.tmp.name, "broker_state.json")
        broker_feedback.EXECUTIONS = os.path.join(self.tmp.name, "execution_ledger.json")
        broker_feedback.EVENTS = os.path.join(self.tmp.name, "broker_events.json")
        guardrails.DATA_DIR = self.tmp.name
        guardrails.GSTATE = os.path.join(self.tmp.name, "guard_state.json")
        guardrails.GLOG = os.path.join(self.tmp.name, "guard_log.json")
        self.env = mock.patch.dict(os.environ, {
            "BROKER_FEEDBACK_REQUIRED": "1",
            "BROKER_ACCOUNT_ID": "acct-1",
            "BROKER_CALLBACK_TOKEN": "secret",
            "BROKER_STATE_MAX_SEC": "90",
            "START_BALANCE": "100000",
            "TARGET_BALANCE": "106000",
            "DD_FLOOR": "97000",
            "MIN_TRADING_DAYS": "2",
            "CONSISTENCY_LIMIT": "0.5",
            "PASS_REQUIRE_BROKER_STATUS": "1",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def snapshot(self, **extra):
        out = {
            "snapshot_id": "s1",
            "account_id": "acct-1",
            "equity": 100000,
            "position_qty": 0,
            "working_orders_count": 0,
            "trading_days": 1,
            "best_day_profit": 0,
            "evaluation_status": "active",
            "drawdown_floor": 97000,
            "trading_day": "2026-08-10",
        }
        out.update(extra)
        return out

    def plan(self):
        return {
            "execution_id": "exec-1", "group_id": "g1", "strategy": "A/B",
            "direction": "LONG", "ticker": "MNQ", "entry": 100, "stop": 90,
            "target": 120, "qty": 1, "cancel_after_sec": 600,
        }

    def test_order_lifecycle_is_idempotent_and_becomes_broker_truth(self):
        broker_feedback.sync_snapshot(self.snapshot())
        self.assertTrue(broker_feedback.register_plan(self.plan()))
        broker_feedback.mark_relay_result("exec-1", True, 200)
        self.assertTrue(broker_feedback.has_live_commitment())
        event = {
            "event_id": "ev-close", "execution_id": "exec-1", "status": "closed",
            "realized_pnl": -42, "event_ms": broker_feedback._now_ms(),
        }
        first = broker_feedback.apply_event(event)
        second = broker_feedback.apply_event(event)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(broker_feedback.has_live_commitment())
        self.assertEqual(broker_feedback.trade_truth("exec-1")["outcome"], "loss")
        self.assertEqual(broker_feedback.trade_truth("exec-1")["net"], -42)

    def test_pass_requires_days_consistency_and_broker_confirmation(self):
        broker_feedback.sync_snapshot(self.snapshot(
            snapshot_id="ready", equity=106100, trading_days=2,
            best_day_profit=2500, evaluation_status="active"))
        progress = guardrails.eval_progress()
        self.assertTrue(progress["pass_ready"])
        self.assertTrue(progress["awaiting_pass_confirmation"])
        self.assertFalse(progress["passed"])
        broker_feedback.sync_snapshot(self.snapshot(
            snapshot_id="passed", equity=106100, trading_days=2,
            best_day_profit=2500, evaluation_status="passed"))
        self.assertTrue(guardrails.eval_progress()["passed"])

    def test_consistency_failure_does_not_false_pass(self):
        broker_feedback.sync_snapshot(self.snapshot(
            snapshot_id="not-ready", equity=106100, trading_days=2,
            best_day_profit=4000, evaluation_status="active"))
        progress = guardrails.eval_progress()
        self.assertTrue(progress["target_reached"])
        self.assertFalse(progress["consistency_met"])
        self.assertFalse(progress["pass_ready"])

    def test_cancel_requires_2xx_and_retries(self):
        responses = [mock.Mock(status_code=500), mock.Mock(status_code=200)]
        fake_requests = types.SimpleNamespace(post=mock.Mock(side_effect=responses))
        with mock.patch.object(guardrails, "requests", fake_requests), \
             mock.patch.object(broker_feedback, "mark_cleanup_requested"):
            with mock.patch.dict(os.environ, {"EXEC_WEBHOOK": "https://relay", "GUARD_ACTION_RETRIES": "2"}):
                self.assertTrue(guardrails.flatten_cancel_only())
        fake_requests = types.SimpleNamespace(post=mock.Mock(return_value=mock.Mock(status_code=500)))
        with mock.patch.object(guardrails, "requests", fake_requests), \
             mock.patch.object(broker_feedback, "mark_cleanup_requested"):
            with mock.patch.dict(os.environ, {"EXEC_WEBHOOK": "https://relay", "GUARD_ACTION_RETRIES": "2"}):
                self.assertFalse(guardrails.flatten_cancel_only())

    def test_failed_state_write_blocks_batch_reservation(self):
        with mock.patch("guardrails.os.replace", side_effect=OSError("disk")):
            self.assertFalse(guardrails.begin_sibling_batch("g1", 500, ["A/B"]))

    def test_projected_daily_loss_blocks_next_full_risk_group(self):
        sample = {
            '_strat': 'A/B', 'dir': 'LONG', 'entry': 100, 'SL': 90, 'TP': 120,
            '_planned_group_risk_usd': 620,
            'bos_ms': int(dt.datetime.now().timestamp() * 1000), 'sess': 'NYAM',
        }
        with mock.patch.object(guardrails, '_day_stats', return_value={
                'sent': 1, 'losses': 1, 'net': -400, 'openpos': False,
                'loss_mode': 'group'}), \
             mock.patch.object(guardrails, '_state', return_value={}), \
             mock.patch.object(guardrails, 'beat'), \
             mock.patch.object(guardrails, 'is_duplicate', return_value=False), \
             mock.patch.object(guardrails, '_active_pending_group', return_value=None), \
             mock.patch.object(guardrails, 'eval_progress', return_value={
                 'passed': False, 'breached': False,
                 'awaiting_pass_confirmation': False}), \
             mock.patch.object(broker_feedback, 'is_fresh', return_value=True), \
             mock.patch.object(broker_feedback, 'broker_open', return_value=False), \
             mock.patch.object(broker_feedback, 'has_live_commitment', return_value=False), \
             mock.patch.dict(os.environ, {
                 'BROKER_CALLBACK_TOKEN': 'x', 'NEWS_GUARD': '0',
                 'DAY_LOSS_USD': '900', 'MAX_TRADES_DAY': '2',
                 'DAY_LOSS_N': '2', 'SKIP_SESSIONS': '',
                 'GUARD_LAST_ENTRY_ET': '0', 'DD_PROXIMITY_MODE': 'off',
             }, clear=False):
            ok, reason = guardrails.guard_ok(sample, feed_age_min=0, market_open=True)
        self.assertFalse(ok)
        self.assertEqual(reason, 'projected_day_loss')


if __name__ == "__main__":
    unittest.main()
