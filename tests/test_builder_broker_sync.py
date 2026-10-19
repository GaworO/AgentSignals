import ast
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import builder_broker_sync as broker


class BrokerSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1792385200.0
        self.config = dict(DATA_DIR=self.folder.name, ACCOUNT_PLAN='builder50',
            BUILDER_BROKER_SYNC_ENABLED='1', BUILDER_BROKER_ACCOUNT_VERIFIED='1',
            BUILDER_BROKER_ACCOUNT_ID='2214', BUILDER_BROKER_ACCOUNT_NAME='test account',
            BUILDER_BROKER_ENVIRONMENT='demo', BUILDER_BROKER_API_TOKEN='test-token')
        self.account = dict(accountId=2214,name='test account',environment='demo',active=True,archived=False,
            balance=dict(netLiq=48961.55,totalCashValue=48961.55))
        self.responses = {'/accounts':[self.account], '/accounts/test%20account':self.account,
            '/accounts/test%20account/positions':[], '/accounts/test%20account/orders':[], '/accounts/test%20account/fills':[]}
        self.partial = None
        self.fail = False
        self.calls = []
        def read(path):
            self.calls.append(path)
            if self.fail:raise OSError('private provider detail must not appear in public state')
            return dict(success=True,data=copy.deepcopy(self.responses[path]),partial=path==self.partial)
        self.sync = broker.AccountSync(self.config,reader=read,clock=lambda:self.now)

    def test_complete_snapshot_equity_persistence_and_no_order_client(self):
        self.assertTrue(self.sync.poll())
        state=self.sync.state()
        self.assertTrue(state['current'])
        self.assertEqual(state['cash'],48961.55)
        self.assertEqual(state['open_positions'],0)
        self.assertIsNone(self.sync.entry_blocker())
        self.assertEqual(len(self.calls),5)
        self.assertFalse(hasattr(self.sync,'mutate'))
        with sqlite3.connect(self.sync.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM account_snapshots').fetchone()[0],1)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='orders'").fetchone())

    def test_wrong_identity_partial_and_account_mismatch_reject(self):
        self.responses['/accounts'][0]['accountId']=999
        self.assertFalse(self.sync.poll())
        self.assertIsNone(self.sync.snapshot())
        self.responses['/accounts'][0]['accountId']=2214
        self.partial='/accounts/test%20account/positions'
        self.assertFalse(self.sync.poll())
        self.assertIsNone(self.sync.snapshot())
        self.partial=None
        self.responses['/accounts/test%20account/orders']=[dict(accountId=999,id=5)]
        self.assertFalse(self.sync.poll())
        self.assertIsNone(self.sync.snapshot())

    def test_outage_keeps_evidence_and_blocks_without_exposing_provider_detail(self):
        self.assertTrue(self.sync.poll())
        self.fail=True
        self.assertFalse(self.sync.poll())
        state=self.sync.state()
        self.assertEqual(state['state'],'ERROR')
        self.assertEqual(state['cash'],48961.55)
        self.assertEqual(state['error'],'BROKER_SYNC_FAILED')
        self.assertEqual(self.sync.entry_blocker(),'broker:account_snapshot_not_current')
        self.assertNotIn('private',json.dumps(state))

    def test_stale_foreign_exposure_and_dispatch_refresh(self):
        self.assertTrue(self.sync.poll())
        self.now+=31
        self.assertEqual(self.sync.entry_blocker(),'broker:account_snapshot_not_current')
        self.assertIsNone(self.sync.entry_blocker(refresh=True))
        self.responses['/accounts/test%20account/positions']=[dict(accountId=2214,netPos=1)]
        self.assertEqual(self.sync.entry_blocker(refresh=True),'broker:account_has_positions_or_orders')
        self.responses['/accounts/test%20account/positions']=[]
        self.responses['/accounts/test%20account/orders']=[dict(accountId=2214,id=99)]
        self.assertEqual(self.sync.entry_blocker(refresh=True),'broker:account_has_positions_or_orders')

    def test_durable_fill_identity_and_conflicting_revision(self):
        fill=dict(id=7,accountId=2214,orderId=8,contractId=9,qty=1,price=30000,action='Buy',timestamp='2026-10-19T04:00:00Z')
        self.responses['/accounts/test%20account/fills']=[fill]
        self.assertTrue(self.sync.poll())
        self.now+=1
        self.assertTrue(self.sync.poll())
        self.assertEqual(self.sync.state()['stored_fills'],1)
        fill['price']=30001
        self.assertFalse(self.sync.poll())
        self.assertEqual(self.sync.state()['error'],'IMMUTABLE_BROKER_FILL_CONFLICT')
        self.assertEqual(self.sync.snapshot()['fills'][0]['price'],30000)

    def test_real_equity_overlay_keeps_halts_and_avoids_model_double_count(self):
        self.assertTrue(self.sync.poll())
        saved=dict(equity=48962,kill=True,kill_reason='manual',equity_day_net_at_sync=-50)
        with patch.object(broker,'_INSTANCE',self.sync):
            overlaid=broker.overlay_equity(saved)
            self.assertTrue(overlaid['kill'])
            self.assertEqual(overlaid['kill_reason'],'manual')
            self.assertEqual(overlaid['equity'],48961.55)
            self.assertEqual(saved['equity'],48962)
            source=Path(__file__).resolve().parents[1].joinpath('guardrails.py').read_text()
            fn=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_modeled_equity')
            scope=dict(builder_broker_sync=broker)
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'<guard regression>','exec'),scope)
            actual=scope['_modeled_equity'](overlaid,dict(net=-500))
            self.assertEqual(actual,48961.55)

    def test_disabled_services_need_no_credentials_or_network(self):
        other=broker.AccountSync(dict(DATA_DIR=self.folder.name),reader=lambda p:self.fail('network called'))
        self.assertFalse(other.poll())
        self.assertIsNone(other.entry_blocker())
        self.assertEqual(other.state(),dict(enabled=False,state='DISABLED'))

    def test_snapshot_cannot_increase_budget_with_open_profit(self):
        self.account['balance']['netLiq']=49961.55
        self.assertTrue(self.sync.poll())
        self.assertEqual(self.sync.state()['risk_equity'],48961.55)


if __name__=='__main__':unittest.main()
