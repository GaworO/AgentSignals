import base64
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App
from data_health import account_snapshot
from manual_account import save_snapshot,snapshot_current


class ManualAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.app=App(dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='t'*40,TANJA_DASHBOARD_PASSWORD='test_dashboard_password'))
        self.now=datetime(2026,10,9,10,0,tzinfo=ZoneInfo('America/New_York')).timestamp()
        self.body=dict(balance=49000.12,minimum_balance=48000.5,target_balance=53000)

    def request(self,body,auth=True,csrf=True):
        raw=json.dumps(body).encode();status=[]
        env=dict(PATH_INFO='/api/account-snapshot',REQUEST_METHOD='POST',CONTENT_LENGTH=str(len(raw)),**{'wsgi.input':io.BytesIO(raw)})
        if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test_dashboard_password').decode()
        if csrf:env['HTTP_X_TANJA_CSRF']=self.app.csrf
        with patch('app.time.time',return_value=self.now):
            reply=json.loads(b''.join(self.app(env,lambda s,h:status.append(s))))
        return status[0],reply

    def test_authenticated_save_and_reported_budget(self):
        status,reply=self.request(self.body)
        self.assertTrue(status.startswith('200'))
        self.assertEqual(reply['risk_budget_usd'],245.0)
        self.assertFalse(reply['orders_enabled'])
        snap=account_snapshot(self.root)
        self.assertEqual(snap['balance'],49000.12)
        self.assertFalse(snap['broker_verified'])
        self.assertTrue(snapshot_current(snap,self.now))
        self.assertEqual(self.app.selection_observer.sizing_context(self.now)['budget_usd'],245)
        self.assertEqual(self.app.selection_observer.sizing_context(self.now+86400)['state'],'DAILY_BALANCE_UPDATE_REQUIRED')
        self.assertIsNone(self.app.selection_observer.sizing_context(self.now+86400)['budget_usd'])

    def test_auth_and_csrf_before_any_write(self):
        self.assertTrue(self.request(self.body,auth=False)[0].startswith('401'))
        self.assertTrue(self.request(self.body,csrf=False)[0].startswith('403'))
        self.assertFalse((self.root/'account_snapshot.json').exists())

    def test_invalid_amounts_leave_previous_snapshot_intact(self):
        self.request(self.body);old=(self.root/'account_snapshot.json').read_bytes()
        for bad in [dict(self.body,balance=True),dict(self.body,balance=float('nan')),dict(self.body,balance=123.456),dict(self.body,balance=-1),dict(self.body,minimum_balance=53000),dict(self.body,reported_at=self.now),{}]:
            with self.subTest(bad=bad):
                self.assertTrue(self.request(bad)[0].startswith('422'))
                self.assertEqual((self.root/'account_snapshot.json').read_bytes(),old)

    def test_new_york_midnight_and_future_snapshot(self):
        now=datetime(2026,10,9,23,59,tzinfo=ZoneInfo('America/New_York')).timestamp()
        snap=save_snapshot(self.root,self.body,now)
        self.assertTrue(snapshot_current(snap,now+59))
        self.assertFalse(snapshot_current(snap,now+60))
        self.assertFalse(snapshot_current(snap,now-1))
        self.assertFalse(snapshot_current(dict(as_of='unknown'),now))
        self.assertTrue(snapshot_current(dict(as_of=snap['as_of']),now))


if __name__=='__main__':unittest.main()
