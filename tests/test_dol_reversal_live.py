import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dol_reversal_live as live


def signal():
    return {"date":"2026-09-23","model":"reversal","cat":"PDL","dir":"LONG",
            "bos":"10:22","bos_ms":1_800_000_000_000,"entry_ms":1_800_000_060_000,
            "entry":100.0,"SL":95.0,"TP":113.0,"_strat":"A/B",
            "_dol":{"metadata_status":"ATTACHED","narrative_class":"COMPLETE_DOL_NARRATIVE",
                    "dol_status":"OPEN","direction_aligned_with_dol":True,"selected_dol":"PDH"}}


class Response:
    status_code=200
    text='{"success":true,"id":"sig-1","logId":"log-1"}'
    def json(self): return {"success":True,"id":"sig-1","logId":"log-1"}


class NoPositionResponse(Response):
    text='{"success":false,"message":"No open position found"}'
    def json(self): return {"success":False,"message":"No open position found"}


class DolLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        self.db=root/"dol.sqlite3"; self.audit=root/"audit.csv"
        self.paths=(live.DB,live.AUDIT); live.DB=self.db; live.AUDIT=self.audit
        self.env=mock.patch.dict(os.environ,{"DOL_REVERSAL_MODE":"LIVE","DOL_MANAGER_MODE":"LIVE",
          "DOL_MANAGER_EXECUTION":"TRADERSPOST_WEBHOOK","DOL_KILL_SWITCH":"0","ACCOUNT_LABEL":"100K",
          "EXEC_WEBHOOK":"https://example.invalid/hook","EXEC_TICKER":"MNQZ6"},clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop(); live.DB,live.AUDIT=self.paths; self.tmp.cleanup()

    def classified(self):
        s=signal()
        with mock.patch.object(live.strategy,"observe",return_value=True):
            gate=live.classify(s,"unused.csv")
        self.assertTrue(gate["accepted"]); return s

    def accepted(self):
        s=self.classified(); payload={}
        self.assertTrue(live.claim_entry(s,4,payload)[0])
        live.record_entry_response(s,Response())
        return s

    def test_one_order_identity_and_fixed_2r(self):
        s=self.classified()
        self.assertEqual(s["_strat"],"DOL_DELIVERY_REVERSAL")
        self.assertEqual((s["entry"],s["SL"],s["TP"]),(100.0,95.0,110.0))
        self.assertEqual(s["_base_strat"],"A/B")
        self.assertFalse(live.classify({"_strat":"Continuation","model":"Continuation"},"x")["accepted"])

    def test_entry_audit_ids_and_restart_idempotency(self):
        s=self.accepted(); row=live.rows()[0]
        self.assertEqual(row["traderspost_signal_id"],"sig-1")
        self.assertEqual(row["traderspost_log_id"],"log-1")
        self.assertEqual(row["traderspost_message"],"WEBHOOK_ACCEPTED")
        self.assertFalse(live.claim_entry(s,4,{})[0])
        with self.audit.open() as f:
            self.assertEqual(next(csv.DictReader(f))["local_fill_status"],"NOT_FILLED")

    def test_retired_manager_variables_cannot_trigger_exit_or_change_stop(self):
        import requests
        self.accepted()
        # Simulate a persisted position managed by an earlier release.
        with live._connect() as con:
            con.execute("UPDATE trades SET virtual_protected_stop=98,manager_active=1,manager_action_status='VIRTUAL_PROTECTED_STOP'")
            con.execute("CREATE TABLE manager_actions(action_id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO manager_actions VALUES('historical-action')")
        with mock.patch.object(requests, "post") as post:
            for minute, low in [(1,99.5),(2,97.5),(3,97.0)]:
                live.on_closed_bar({"ts_event":f"2027-01-15T08:0{minute}:00Z","high":101,"low":low})
        post.assert_not_called()
        row=live.rows()[0]
        self.assertEqual(row["current_sl"],95.0)
        self.assertEqual(row["local_fill_status"],"LOCAL_FILL_DETECTED")
        self.assertEqual(row["manager_active"],0)
        with live._connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM manager_actions").fetchone()[0],1)

    def test_accounts_share_signal_but_not_client_order_id(self):
        first=self.classified()
        with mock.patch.dict(os.environ,{"ACCOUNT_LABEL":"50K"},clear=False):
            second=self.classified()
        self.assertEqual(first["_signal_id"],second["_signal_id"])
        self.assertNotEqual(first["_client_order_id"],second["_client_order_id"])

    def test_local_fill_and_sl_first(self):
        self.accepted()
        live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5})
        live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":111,"low":94})
        row=live.rows()[0]
        self.assertEqual(row["local_fill_status"],"CLOSED_LOCALLY")
        self.assertEqual(row["close_reason"],"SL")

    def test_kill_switch_blocks_new_entry(self):
        s=self.classified()
        with mock.patch.dict(os.environ,{"DOL_KILL_SWITCH":"1"},clear=False):
            self.assertFalse(live.claim_entry(s,1,{})[0])

    def test_local_target_remains_fixed_2r(self):
        self.accepted()
        live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5})
        live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":111,"low":99.0})
        row=live.rows()[0]
        self.assertEqual(row["close_reason"],"TP")
        self.assertEqual(row["realized_r"],2.0)

    def test_open_position_ignores_out_of_order_bars_after_manager_removal(self):
        self.accepted()
        live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5})
        live.on_closed_bar({"ts_event":"2027-01-15T08:03:00Z","high":102,"low":99.0})
        live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":111,"low":99.0})
        self.assertEqual(live.rows()[0]["local_fill_status"],"LOCAL_FILL_DETECTED")


if __name__=="__main__": unittest.main()
