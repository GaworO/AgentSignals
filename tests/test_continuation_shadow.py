import ast
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import pandas as pd

try:
    import flask  # noqa: F401
except ImportError:  # logic-only local test runtime; production requirements include Flask
    stub = types.ModuleType("flask")
    stub.Response = object
    stub.jsonify = lambda *args, **kwargs: None
    stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = stub

import continuation_shadow as subject


def _frame(rows):
    return pd.DataFrame(rows, columns=["ts_event", "instrument_id", "open", "high", "low", "close", "volume"])


def _reset(path):
    subject.DB_PATH = path / "continuation.sqlite3"
    subject._init_db()


class ContinuationShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _reset(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_broker_inert_module_has_no_broker_or_network_imports(self):
        tree = ast.parse(Path(subject.__file__).read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("requests", imports)
        self.assertNotIn("guardrails", imports)
        self.assertNotIn("live_emit", imports)

    def test_warmup_does_not_create_fake_forward_order_and_restart_dedups(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        raw = _frame([(base, 7, 100, 101, 99, 100, 1), (base + pd.Timedelta(minutes=1), 7, 100, 101, 99, 100, 1)])
        candidate = {
            "candidate_id": "CONT_WARMUP", "entry_ms": int(base.timestamp() * 1000), "bos_ms": int(base.timestamp() * 1000),
            "eligible": True, "estimated_fill": False, "trading_day": base.normalize(), "jade_thesis": "LONG",
            "final_entry": 100.0, "final_structural_sl": 98.0, "policy_B_target": 105.0, "dol_id": "DOL_1",
            "source_event": {"bsl_name": "PDH"},
        }
        order = {
            "order_id": "ORDER_WARMUP", "candidate_id": "CONT_WARMUP", "instrument_id": 7,
            "activation_timestamp": base, "expiry_timestamp": base + pd.Timedelta(minutes=10),
            "entry_price": 100.0, "structural_sl_price": 98.0, "policy_B_target": 105.0,
            "initial_risk_points": 2.0, "dol_id": "DOL_1",
        }
        subject._upsert_scan(raw, [], [], [candidate], [order], {})
        with subject._connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM continuation_orders").fetchone()[0], 0)

        later = base + pd.Timedelta(minutes=2)
        raw2 = pd.concat([raw, _frame([(later, 7, 100, 101, 99, 100, 1)])], ignore_index=True)
        candidate2 = dict(candidate, candidate_id="CONT_FORWARD", entry_ms=int(later.timestamp() * 1000))
        order2 = dict(order, order_id="ORDER_FORWARD", candidate_id="CONT_FORWARD",
                      activation_timestamp=later, expiry_timestamp=later + pd.Timedelta(minutes=10))
        subject._upsert_scan(raw2, [], [], [candidate2], [order2], {})
        subject._upsert_scan(raw2, [], [], [candidate2], [order2], {})
        with subject._connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM continuation_orders").fetchone()[0], 1)

    def test_fill_bar_is_inert_then_adverse_first_gap_and_variable_cost_r(self):
        base = pd.Timestamp("2026-01-01T12:00:00Z")
        with subject._connect() as con:
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            con.execute(
                "INSERT INTO continuation_candidates(candidate_id,event_kind,decision_ms,stage,status,eligible,forward_eligible,payload_json,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("C1", "CANONICAL_OUTPUT", int(base.timestamp() * 1000), "ORDER", "ORDER", 1, 1, "{}", now, now),
            )
            con.execute(
                "INSERT INTO continuation_orders(order_id,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,stop_price,target_price,risk_points,dol_id,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("O1", "C1", 7, int(base.timestamp() * 1000), int((base + pd.Timedelta(minutes=10)).timestamp() * 1000),
                 100.0, 98.0, 104.0, 2.0, "D1", "PENDING", "{}", now, now),
            )
        raw = _frame([
            (base, 7, 100.0, 105.0, 97.0, 101.0, 1),
            (base + pd.Timedelta(minutes=1), 7, 97.0, 105.0, 96.0, 100.0, 1),
        ])
        subject._reconcile(raw)
        subject._reconcile(raw)
        with subject._connect() as con:
            trade = con.execute("SELECT * FROM continuation_trades").fetchone()
            self.assertEqual(con.execute("SELECT COUNT(*) FROM continuation_trades").fetchone()[0], 1)
        self.assertEqual(trade["exit_reason"], "STRUCTURAL_SL")
        self.assertEqual(trade["exit_price"], 97.0)
        self.assertEqual(trade["raw_r"], -1.5)
        self.assertEqual(trade["cost_r"], 3.50 / (2.0 * 2.0))
        self.assertEqual(trade["net_r"], -1.5 - 0.875)

    def test_frozen_development_population_reference_and_freeze_hashes(self):
        root = Path(subject.__file__).resolve().parent / "MNQ_CONTINUATION_HTF_CANONICAL_BASELINE_V1_OUTCOME_FREE_FREEZE"
        orders = [json.loads(line) for line in (root / "order_manifest.jsonl").read_text().splitlines() if line]
        self.assertEqual(len(orders), 334)
        self.assertEqual(sum(bool(x["estimated_fill"]) for x in orders), 126)
        provenance = subject._verify_freeze()
        self.assertEqual(provenance["configuration_sha256"], "959f2ab4c497f9db4661c321f2acd3b1f66cc7071d32bdf08c1362d65afcbe17")

    def test_routes_navigation_and_independent_bar_hook_are_wired(self):
        class FakeApp:
            def __init__(self):
                self.routes = []

            def get(self, path):
                def decorator(fn):
                    self.routes.append(path)
                    return fn
                return decorator

        app = FakeApp()
        original = subject.notify_bar
        subject.notify_bar = lambda *args, **kwargs: {"scheduled": False}
        try:
            subject.register(app, archive_path=Path(self.temp.name) / "archive.csv")
        finally:
            subject.notify_bar = original
        self.assertIn("/continuation", app.routes)
        self.assertIn("/continuation/candidates", app.routes)
        self.assertIn("/continuation/api/candidate/<candidate_id>", app.routes)
        root = Path(subject.__file__).resolve().parent
        dashboard = (root / "dashboard.py").read_text(encoding="utf-8")
        agent = (root / "agent.py").read_text(encoding="utf-8")
        self.assertIn("['continuation','Continuation','target']", dashboard)
        self.assertIn("['dolrev','DOL Delivery Reversal · Shadow','target']", dashboard)
        self.assertIn("dolrevmgr:{name:'DOL Reversal Manager Shadow'", dashboard)
        self.assertIn("continuation_shadow.on_bar(b)", agent)
        self.assertIn("continuation_shadow.register(app, archive_path=ARCHIVE)", agent)
        self.assertIn("dol_delivery_reversal_shadow.register(app)", agent)
        self.assertIn("dol_reversal_manager_shadow_v1.register(app)", agent)


if __name__ == "__main__":
    unittest.main()
