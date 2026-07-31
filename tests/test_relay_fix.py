import os
import unittest
import broker_feedback
from execution_plan import ExecutionConfig, build_execution_plan
from relay_service.core import prepare, relay_result


class RelayFixTests(unittest.TestCase):
    def signal(self):
        return {
            "date": "2026-07-31",
            "model": "Cont",
            "cat": "NYAML",
            "dir": "SHORT",
            "bos": "10:07",
            "bos_ms": 1_785_506_820_000,
            "entry_ms": 1_785_506_880_000,
            "entry": 28229.49,
            "SL": 28260.49,
            "TP": 28170.49,
        }

    def test_plan_payload_has_cancel_after_and_identity_extras(self):
        plan = build_execution_plan(
            self.signal(), 1,
            config=ExecutionConfig(tick_size=0.25, fill_window_min=10),
            signal_key="sig-1",
        )
        payload = plan.broker_payloads()[0]
        self.assertEqual(payload["cancelAfter"], 600)
        self.assertEqual(payload["extras"]["plan_id"], plan.plan_id)
        self.assertEqual(payload["extras"]["signal_key"], "sig-1")
        self.assertTrue(payload["time"].endswith("Z"))

    def test_traderspost_id_is_not_broker_order_id(self):
        event = broker_feedback.normalize({
            "provider": "traderspost-relay",
            "status": "accepted",
            "id": "signal-uuid",
            "relay_signal_id": "signal-uuid",
            "logId": "log-uuid",
        })
        self.assertIsNone(event.order_id)
        self.assertEqual(event.relay_signal_id, "signal-uuid")
        self.assertEqual(event.relay_log_id, "log-uuid")

    def test_relay_prepares_default_cancel_after(self):
        payload, ident = prepare({
            "ticker": "MNQ",
            "action": "sell",
            "orderType": "limit",
            "limitPrice": 28229.5,
            "text": "[plan:ep_abc] [signal:sig-abc] [leg:1/1]",
        })
        self.assertEqual(payload["cancelAfter"], 600)
        self.assertEqual(payload["extras"]["plan_id"], "ep_abc")
        self.assertEqual(ident["signal_key"], "sig-abc")

    def test_relay_result_exposes_signal_id_not_order_id(self):
        result = relay_result(
            {"success": True, "id": "tp-signal", "logId": "tp-log"},
            {"plan_id": "ep_abc", "signal_key": "sig-abc"},
            http_status=200, event_ms=1234, cancel_after=600,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["relay_signal_id"], "tp-signal")
        self.assertNotIn("order_id", result)



if __name__ == "__main__":
    unittest.main()
