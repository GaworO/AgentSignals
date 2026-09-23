import os
import unittest
from unittest import mock

os.environ.setdefault("HEARTBEAT", "0")

import agent


def _order(direction="LONG"):
    long = direction == "LONG"
    return {
        "order_id": "order-1",
        "candidate_id": "candidate-1",
        "dol_id": "dol-1",
        "direction": direction,
        "activation_ms": 1_700_000_000_000,
        "entry_price": 100.0,
        "stop_price": 90.0 if long else 110.0,
        "target_price": 130.0 if long else 70.0,
    }


class ContinuationAgentIntegrationTests(unittest.TestCase):
    def _dispatch(self, profile, account, requested):
        captured = {}

        def execute(signal, _text):
            captured.update(signal)
            return {"sent": True, "status": 200, "qty": 4, "route_id": "route"}

        env = {
            "ACCOUNT": str(account),
            "CONTINUATION_RISK_USD_100K": str(requested),
            "CONTINUATION_RISK_USD_50K": str(requested),
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(agent.guardrails, "account_profile", return_value=profile), \
             mock.patch.object(agent.guardrails, "_exec_route_id", return_value="route"), \
             mock.patch.object(agent.guardrails, "exec_mode", return_value="auto"), \
             mock.patch.object(agent.guardrails, "ramp_qty"), \
             mock.patch.object(agent.guardrails, "guard_ok", return_value=(True, "ok")), \
             mock.patch.object(agent.guardrails, "begin_sibling_batch", return_value=True) as begin, \
             mock.patch.object(agent.guardrails, "touch_sibling_batch") as touch, \
             mock.patch.object(agent.guardrails, "note"), \
             mock.patch.object(agent, "flags_for", return_value=([], False)), \
             mock.patch.object(agent, "_feed_age_min", return_value=1.0), \
             mock.patch.object(agent, "_market_open_now", return_value=True), \
             mock.patch.object(agent, "_cal_age_h", return_value=1.0), \
             mock.patch.object(agent, "_exec_order", side_effect=execute):
            result = agent._dispatch_continuation_live(_order())
        return result, captured, begin, touch

    def test_pro100_hard_cap_and_shared_reservation(self):
        result, signal, begin, touch = self._dispatch(
            {"plan": "pro100", "label": "100K", "config_ok": True}, 100_000, 9_999
        )
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(signal["_risk_budget_usd"], 500.0)
        self.assertTrue(signal["_disable_partial"])
        begin.assert_called_once_with("continuation_order-1", 500.0, ["Continuation LONG"])
        touch.assert_called_once()

    def test_builder50_hard_cap(self):
        result, signal, begin, _touch = self._dispatch(
            {"plan": "builder50", "label": "50K", "config_ok": True}, 50_000, 9_999
        )
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(signal["_risk_budget_usd"], 250.0)
        begin.assert_called_once_with("continuation_order-1", 250.0, ["Continuation LONG"])

    def test_session_multiplier_cannot_raise_strict_budget_quantity(self):
        signal = {
            "dir": "LONG", "entry": 100.0, "SL": 90.0, "TP": 130.0,
            "sess": "ASIA", "_risk_budget_usd": 500.0, "_strict_risk_budget": True,
            "_disable_partial": True,
        }
        response = mock.Mock(status_code=200, text="ok")
        env = {
            "EXEC_WEBHOOK": "https://example.invalid/order",
            "EXEC_QTY": "auto", "EXEC_MAX_QTY": "15", "EXEC_TICK": "0.25",
            "SESSION_SIZE_MULT": "ASIA:2", "PARTIAL_AT_1R": "0", "PRICE_OFFSET": "0",
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(agent.live_emit, "size_for_budget", return_value=(4, 500.0, 125.0)), \
             mock.patch.object(agent.requests, "post", return_value=response) as post, \
             mock.patch.object(agent.guardrails, "_exec_route_id", return_value="route"):
            result = agent._exec_order(signal)
        self.assertTrue(result["sent"])
        self.assertEqual(post.call_args.kwargs["json"]["quantity"], 4)


if __name__ == "__main__":
    unittest.main()
