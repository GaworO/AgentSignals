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

    def test_hold_has_no_webhook(self):
        self.accepted(); row=live._connect().execute("select * from trades").fetchone()
        with mock.patch.object(live.requests,"post") as post:
            result=live._post_action(row,"hold-1","HOLD",1_800_000_120_000,.5,[1,2])
        self.assertEqual(result,"NO_WEBHOOK"); post.assert_not_called()

    def test_breakeven_and_full_close_payloads(self):
        self.accepted(); row=live._connect().execute("select * from trades").fetchone()
        with mock.patch.object(live.requests,"post",return_value=Response()) as post:
            self.assertEqual(live._post_action(row,"be-1","BREAKEVEN",1,.9,[1]),"WEBHOOK_ACCEPTED")
            self.assertEqual(post.call_args.kwargs["json"]["action"],"breakeven")
        row=live._connect().execute("select * from trades").fetchone()
        with mock.patch.object(live.requests,"post",return_value=Response()) as post:
            self.assertEqual(live._post_action(row,"exit-1","FULL_CLOSE",2,.9,[1]),"EXIT_REQUEST_ACCEPTED")
            self.assertEqual(post.call_args.kwargs["json"],mock.ANY)
            body=post.call_args.kwargs["json"]
            self.assertEqual(body["action"],"exit"); self.assertTrue(body["cancel"]); self.assertTrue(body["ignoreTradingWindows"])

    def test_no_open_position_reconciles_without_second_order(self):
        self.accepted(); row=live._connect().execute("select * from trades").fetchone()
        with mock.patch.object(live.requests,"post",return_value=NoPositionResponse()) as post:
            result=live._post_action(row,"exit-none","FULL_CLOSE",2,.9,[1])
        self.assertEqual(result,"NO_OPEN_POSITION_AT_TRADERSPOST")
        self.assertEqual(post.call_count,1)
        updated=live.rows()[0]
        self.assertEqual(updated["manager_active"],0)
        self.assertEqual(updated["close_reason"],"NO_OPEN_POSITION_AT_TRADERSPOST")

    def test_virtual_stop_keeps_physical_sl_and_exits_once(self):
        self.accepted(); row=live._connect().execute("select * from trades").fetchone()
        with mock.patch.object(live.requests,"post") as post:
            live._post_action(row,"vp-1","VIRTUAL_PROTECTED_STOP",1,.9,[1],98.0)
            post.assert_not_called()
        before=live.rows()[0]; self.assertEqual(before["current_sl"],95.0); self.assertEqual(before["virtual_protected_stop"],98.0)
        # First bar causally detects the entry; second breaches the virtual stop.
        live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5})
        with mock.patch.object(live.requests,"post",return_value=Response()) as post, mock.patch.object(live,"_manager_decision",return_value=("HOLD",None,None,None)):
            live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":101,"low":97.5})
            live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":101,"low":97.5})
            live.on_closed_bar({"ts_event":"2027-01-15T08:03:00Z","high":101,"low":97.0})
            self.assertEqual(post.call_count,1)

    def test_accounts_share_signal_but_not_client_order_id(self):
        first=self.classified()
        with mock.patch.dict(os.environ,{"ACCOUNT_LABEL":"50K"},clear=False):
            second=self.classified()
        self.assertEqual(first["_signal_id"],second["_signal_id"])
        self.assertNotEqual(first["_client_order_id"],second["_client_order_id"])

    def test_local_fill_and_sl_first(self):
        self.accepted()
        live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5})
        with mock.patch.object(live,"_manager_decision",return_value=("HOLD",None,None,None)):
            live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":111,"low":94})
        row=live.rows()[0]
        self.assertEqual(row["local_fill_status"],"CLOSED_LOCALLY")
        self.assertEqual(row["close_reason"],"SL")

    def test_kill_switch_blocks_new_entry(self):
        s=self.classified()
        with mock.patch.dict(os.environ,{"DOL_KILL_SWITCH":"1"},clear=False):
            self.assertFalse(live.claim_entry(s,1,{})[0])

    def test_kill_switch_blocks_manager_action_but_keeps_lifecycle(self):
        self.accepted()
        with mock.patch.dict(os.environ,{"DOL_KILL_SWITCH":"1"},clear=False), \
             mock.patch.object(live,"_manager_decision",return_value=("FULL_CLOSE",.99,[1],None)), \
             mock.patch.object(live.requests,"post") as post:
            live.on_closed_bar({"ts_event":"2027-01-15T08:01:00Z","high":101,"low":99.5,"close":100})
            live.on_closed_bar({"ts_event":"2027-01-15T08:02:00Z","high":101,"low":99,"close":100})
        post.assert_not_called()
        self.assertEqual(live.rows()[0]["local_fill_status"],"LOCAL_FILL_DETECTED")


if __name__=="__main__": unittest.main()
