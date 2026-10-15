import io
import json
import tempfile
import unittest
from urllib.error import HTTPError
from store import Store
from execution_ledger import ExecutionLedger
from crosstrade_capture import CrossTradeReader,CrossTradeCapture,NoRedirect
from test_execution_ledger import LEDGER_CONFIG,fill
from test_broker_feedback import NOW


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=ExecutionLedger(Store(self.tmp.name),LEDGER_CONFIG)

    def capture(self,rows=None,session=None):
        class Reader:
            def history(self,*args):return rows if rows is not None else [fill(1),fill(2,'Sell')]
            def get(self,path):return session if session is not None else dict(success=True,data=[dict(environment='demo',data=[
                dict(id=r['fillId'],**{k:r[k] for k in ('qty','price','action','timestamp','instrument')}) for r in [fill(1),fill(2,'Sell')]])])
        return CrossTradeCapture(self.ledger,dict(LEDGER_CONFIG,TANJA_CROSSTRADE_CAPTURE_ENABLED='true',
            TANJA_CROSSTRADE_ACCOUNT_NAME='synthetic'),Reader())

    def test_capture_does_not_certify_coverage_or_final_fees(self):
        self.assertTrue(self.capture().poll(NOW))
        state=self.ledger.state(NOW);self.assertEqual(state['fill_count'],2)
        self.assertIsNone(state['loss_count']);self.assertIn('ACCOUNT_WIDE_COVERAGE_UNVERIFIED',state['reasons'])
        self.assertIn('FILL_FEES_NOT_FINAL',state['reasons'])

    def test_identity_failure_and_unresolved_session_fills_cannot_be_empty_history(self):
        for session in (dict(success=True,data=[dict(environment='demo',data=dict(error='upstream'))]),
            dict(success=True,data=[dict(environment='demo',data=[dict(id=99)])])):
            self.assertFalse(self.capture(session=session).poll(NOW))
            self.assertIn('ACCOUNT_CAPTURE_NOT_CURRENT',self.ledger.state(NOW)['reasons'])

    def test_wrong_account_unknown_contract_and_failed_history_block_sync(self):
        for patch in ({'accountId':999},{'instrument':'CL 12-26'}):
            row=fill(1);row.update(patch)
            self.assertFalse(self.capture(rows=[row]).poll(NOW));self.assertEqual(self.ledger.state(NOW)['fill_count'],0)

    def test_current_session_contradictions_block_delayed_history(self):
        row=fill(1,price=101);row['id']=row.pop('fillId')
        session=dict(success=True,data=[dict(environment='demo',data=[row])])
        self.assertFalse(self.capture(session=session).poll(NOW))
        self.assertEqual(self.ledger.state(NOW)['capture_detail']['reason'],'SESSION_CORE_CONFLICT')

    def test_deleted_rows_and_late_costs_merge_without_losing_local_evidence(self):
        self.capture().poll(NOW)
        row=fill(1,fee='1.5');self.capture(rows=[row],session=dict(success=True,data=[])).poll(NOW+1)
        state=self.ledger.state(NOW+1);self.assertEqual(state['fill_count'],2);self.assertEqual(state['cost_observations'],3)

    def test_defaults_do_not_connect_and_reader_has_no_order_routes(self):
        capture=CrossTradeCapture(self.ledger,{})
        self.assertFalse(capture.poll(NOW))
        reader=CrossTradeReader('synthetic-not-a-real-key')
        for route in ('/orders','/orders/1/cancel','https://example.com','/fills?x=1'):
            with self.assertRaisesRegex(ValueError,'READ_ROUTE_NOT_ALLOWED'):reader.get(route)
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_http_get_sanitizes_errors_and_never_follows_redirects(self):
        reader=CrossTradeReader('synthetic-not-a-real-key');requests=[]
        class Opener:
            def open(self,request,timeout):
                requests.append(request)
                raise HTTPError('https://secret.example/account/private',429,'secret-body',{},None)
        reader.opener=Opener()
        with self.assertRaisesRegex(ValueError,'^CROSSTRADE_RATE_LIMITED$'):reader.get('/fills')
        self.assertEqual(requests[0].get_method(),'GET')
        self.assertEqual(requests[0].full_url,'https://app.crosstrade.io/v1/api/tv/fills')

    def test_history_paginates_and_rejects_cursor_cycles_or_missing_page(self):
        reader=CrossTradeReader('synthetic-not-a-real-key');calls=[]
        def get(path,params):
            calls.append(dict(params))
            return dict(data=[fill(len(calls))],nextCursor='page2' if len(calls)==1 else None)
        reader.get=get;self.assertEqual(len(reader.history('start','end','synthetic','demo')),2)
        self.assertEqual(calls[1]['cursor'],'page2')
        reader.get=lambda *args:dict(data=[],nextCursor='repeat')
        with self.assertRaisesRegex(ValueError,'CURSOR_INVALID'):reader.history('start','end','synthetic','demo')
        reader.get=lambda *args:dict(data=[])
        with self.assertRaisesRegex(ValueError,'SCHEMA_INVALID'):reader.history('start','end','synthetic','demo')


if __name__=='__main__':unittest.main()
