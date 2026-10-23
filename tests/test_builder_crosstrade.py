"""Native broker protocol tests: all HTTP is an injected in-memory fixture."""
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

import builder_broker_sync as broker
import builder_crosstrade as native
import directional_ai_review
import execution_validation

ROOT=Path(__file__).resolve().parents[1]

def functions(path,names,namespace):
    tree=ast.parse(path.read_text())
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),str(path),'exec'),namespace)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1792400000.;self.posts=[];self.life={};self.history=[];self.timeout=False;self.bad_ack=False
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,dict(BUILDER_ORDER_PROVIDER='crosstrade',ACCOUNT_LABEL='Builder 50K',
            LIVE_EXECUTION_ENABLED='true', EXEC_KILL_SWITCH='false', EXEC_STRATEGY_POLICY='V3_ONLY',
            AB_V3_MODE='LIVE',V3_ENTRY_ARMED='1',AB_DIRECTIONAL_LIVE_LONG='1',AB_DIRECTIONAL_LIVE_SHORT='1',
            GUARD_TOKEN='x'*32,EXEC_TICKER='MNQZ2026',AB_V3_CONTRACT='MNQZ2026',
            AB_V3_DETECTOR_CONTRACT_VERIFIED='1',AB_V3_EXCLUSIVE_ROUTE='1', DATA_DIR=self.tmp.name,
            AB_V3_DB=str(Path(self.tmp.name)/'v3.sqlite3'))).start()
        # Protocol unit tests isolate the independently tested final market gate.
        self.final_gate = patch.object(execution_validation, 'check', return_value=None).start()
        self.api=types.SimpleNamespace()
        ns=dict(Path=Path,os=os,sqlite3=sqlite3,HERE=Path(self.tmp.name),json=json,hashlib=hashlib,math=__import__('math'))
        manager=types.SimpleNamespace(INVALIDATIONS=('NO_FILL','CANCELLED','EXPIRED'),LIFECYCLE='ASSUMED_BOUNDARY_M3_RUNNER_V1')
        ns.update(position_manager=manager,contract=lambda:'MNQZ2026',route=lambda:'test-route',now_ms=lambda:int(self.now*1000),exit_mode=lambda:'real')
        functions(ROOT/'ab_v3_live.py',{'db_path','connect','dump','_exit_transition','broker_event'},ns)
        for k in ('connect','dump','_exit_transition','broker_event','contract','route','now_ms','exit_mode'):setattr(self.api,k,ns[k])
        mgr=dict(hydrate=lambda order:None,json=json,math=__import__('math'),live=lambda:self.api,TERMINAL=('NO_FILL','CANCELLED','EXPIRED','CLOSED'),threading=__import__('threading'),builder_crosstrade=native)
        functions(ROOT/'v3_position_manager.py',{'invalidate','accept_entry','_native_exit'},mgr)
        manager.invalidate=mgr['invalidate'];self.accept=mgr['accept_entry'];self.exit=mgr['_native_exit']
        self.config=dict(DATA_DIR=self.tmp.name,ACCOUNT_PLAN='builder50',BUILDER_BROKER_SYNC_ENABLED='1',
            BUILDER_BROKER_ACCOUNT_VERIFIED='1',BUILDER_BROKER_ACCOUNT_ID='2214',BUILDER_BROKER_ACCOUNT_NAME='Builder account',
            BUILDER_BROKER_ENVIRONMENT='demo',BUILDER_BROKER_API_TOKEN='fixture-token')
        self.account=dict(accountId=2214,name='Builder account',environment='demo',active=True,archived=False,
                          balance=dict(netLiq=48961.55,totalCashValue=48961.55))
        self.values={'/accounts':[self.account],'/accounts/Builder%20account':self.account,
            '/accounts/Builder%20account/positions':[], '/accounts/Builder%20account/orders':[], '/accounts/Builder%20account/fills':[]}
        self.sync=broker.AccountSync(self.config,reader=lambda path:dict(success=True,data=copy.deepcopy(self.values[path])),clock=lambda:self.now)
        self.assertTrue(self.sync.poll())
        self.t=native.Transport(self.api,self.sync,request=self.request,clock=lambda:self.now)
        patch.object(native,'_TRANSPORT',self.t).start();patch.object(native,'_API',self.api).start()
        self.order=dict(order_id='order-one',strategy='AB_DIRECTIONAL',route_id='test-route',contract='MNQZ2026',account_label='Builder 50K',
            activation_ms=int(self.now*1000)-1000,expiry_ms=int(self.now*1000)+600000,direction='LONG',quantity=2,
            entry_price=30000.,stop_price=29990.,target_price=30030.,risk_points=10,lifecycle=manager.LIFECYCLE)
        self.prepare()

    def prepare(self):
        with self.api.connect() as c:
            c.execute('INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?)',(self.order['order_id'],'PREPARED',json.dumps(self.order),None,int(self.now*1000)))

    def request(self,method,path,body=None):
        if method=='GET':
            if path.startswith('/contracts/find?'):return dict(success=True,data=dict(id=100,name='MNQZ6'))
            if path.startswith('/fills/history?'):return dict(success=True,data=copy.deepcopy(self.history),nextCursor=None)
            if path=='/orders':return dict(success=True,data=[dict(environment='demo',data=[copy.deepcopy(v['order']) for v in self.life.values()])])
            if path.endswith('/lifecycle'):
                oid=int(path.split('/')[-2]);return dict(success=True,data=copy.deepcopy(self.life[oid]))
            oid=int(path.split('/')[-1]);return dict(success=True,data=copy.deepcopy(self.life[oid]['order']))
        self.posts.append((path,copy.deepcopy(body)))
        if self.timeout:raise TimeoutError('fixture')
        if path.endswith('/orders/place'):
            return dict(success=True,destination='tradovate',api='place_with_brackets',account='foreign' if self.bad_ack else 'Builder account',instrument='MNQZ6',
                        response=dict(orderId=11,oso1Id=12,oso2Id=13,osoChildIds=[12,13]))
        return dict(success=True,destination='tradovate',account='Builder account',
                    api='liquidate_position' if path.endswith('/positions/close') else 'cancel_order',response=dict(orderId=14,commandId=888))

    def payload(self):
        return dict(ticker='MNQZ2026',action='buy' if self.order['direction']=='LONG' else 'sell',orderType='limit',
            limitPrice=self.order['entry_price'],quantity=2,stopLoss=dict(stopPrice=self.order['stop_price']),
            takeProfit=dict(limitPrice=self.order['target_price']),cancelAfter=60)

    def submit(self):
        result=self.t.submit(self.payload(),self.order['order_id']);self.receipt=result
        self.client=result['client_id'];self.lifecycles();return result

    def lifecycles(self):
        buy=self.order['direction']=='LONG'
        for oid,kind,price,status,action in [(11,'Limit',self.order['entry_price'],'Working','Buy' if buy else 'Sell'),
            (12,'Limit',self.order['target_price'],'Suspended','Sell' if buy else 'Buy'),
            (13,'Stop',self.order['stop_price'],'Suspended','Sell' if buy else 'Buy')]:
            self.life[oid]=dict(order=dict(id=oid,accountId=2214,contractId=100,action=action,ordStatus=status,
                **(dict(parentId=11,ocoId=99) if oid!=11 else {})),
                version=dict(orderId=oid,orderQty=2,orderType=kind,**{('stopPrice' if kind=='Stop' else 'price'):price}),
                commands=[dict(commandType='New',clOrdId=self.client)] if oid==11 else [],reports=[])

        # Native OSO: only the target has parentId; the stop is linked by OCO.
        self.life[13]['order'].pop('parentId',None)

    def fill(self,identity,oid,qty,price,action,offset=1):
        return dict(id=identity,orderId=oid,accountId=2214,contractId=100,qty=qty,price=price,action=action,
                    timestamp=datetime.fromtimestamp(self.now-offset,timezone.utc).isoformat())

    def snapshot(self,fills=(),position=0,orders=()):
        self.values['/accounts/Builder%20account/fills']=list(fills)
        self.values['/accounts/Builder%20account/positions']=[dict(accountId=2214,contractId=100,netPos=position)] if position else []
        self.values['/accounts/Builder%20account/orders']=list(orders)
        self.assertTrue(self.sync.poll());return self.sync.snapshot()

    def open(self,qty=2):
        self.now+=5
        self.life[11]['order']['ordStatus']='Filled' if qty==2 else 'Working'
        for oid in (12,13):self.life[oid]['order']['ordStatus']='Working';self.life[oid]['version']['orderQty']=qty
        action='Buy' if self.order['direction']=='LONG' else 'Sell'
        self.entry=self.fill(101,11,qty,self.order['entry_price'],action)
        snap=self.snapshot([self.entry],qty if action=='Buy' else -qty)
        self.t.reconcile_one(self.t.row(self.order['order_id']),snap);return snap

    def complete(self,winning=True):
        self.now+=5
        for oid in (11,12,13):self.life[oid]['order']['ordStatus']='Filled' if oid in (11,12 if winning else 13) else 'Canceled'
        direction=1 if self.order['direction']=='LONG' else -1
        entry=self.fill(101,11,2,self.order['entry_price'],'Buy' if direction==1 else 'Sell',offset=4)
        px=self.order['target_price'] if winning else self.order['stop_price']
        exit=self.fill(102,12 if winning else 13,2,px,'Sell' if direction==1 else 'Buy')
        # Keep immutable entry timestamp if it has been seen already.
        if hasattr(self,'entry'):entry=self.entry
        gross=(px-self.order['entry_price'])*direction*4;cost=getattr(self,'fixture_round_trip',1.2);net=gross-2*cost
        self.account['balance'].update(netLiq=48961.55+net,totalCashValue=48961.55+net)
        self.history=[dict(fillId=f['id'],orderId=f['orderId'],accountId=2214,accountName='Builder account',environment='demo',instrument='MNQ 12-26',
            qty=f['qty'],price=f['price'],action=f['action'],timestamp=f['timestamp'],commission=cost/2,fees=cost/2) for f in (entry,exit)]
        return self.snapshot([entry,exit]),net

    def local(self):
        with self.api.connect() as c:return dict(c.execute('SELECT * FROM orders WHERE order_id=?',(self.order['order_id'],)).fetchone())

    def test_long_and_short_native_bracket_uses_scalar_levels_gtd_and_waits_for_real_fill(self):
        for direction in ('LONG','SHORT'):
            with self.subTest(direction=direction):
                if direction=='SHORT':
                    with self.api.connect() as c:c.execute('DELETE FROM native_orders');c.execute('DELETE FROM orders')
                    self.order.update(direction='SHORT',stop_price=30010.,target_price=29970.);self.prepare()
                result=self.submit();self.assertTrue(result['sent'])
                body=self.posts[-1][1]
                self.assertEqual(body['clOrdId'],body['orderId'])
                self.assertEqual(body['clOrdId'],self.t.row('order-one')['client_id'])
                self.assertEqual((body['takeProfit'],body['stopLoss']),(self.order['target_price'],self.order['stop_price']))
                self.assertEqual(body['tif'],'gtd');self.assertEqual(body['requireMarketPosition'],'flat')
                self.accept(self.order,dict(state='SENT',quantity=2,broker=result))
                self.assertEqual(self.local()['state'],'WAITING_BROKER_FILL');self.assertIsNone(self.local()['position_json'])
                self.assertEqual(json.loads(self.t.row('order-one')['receipt_json'])['orderId'],11)

    def test_transport_timeout_is_durable_and_never_retried_even_after_restart(self):
        self.timeout=True;result=self.submit();self.assertTrue(result['submission_unknown'])
        restarted=native.Transport(self.api,self.sync,request=self.request,clock=lambda:self.now)
        with self.assertRaises(native.TransportError):restarted.submit(self.payload(),'order-one')
        self.assertEqual(len(self.posts),1);self.assertEqual(restarted.row('order-one')['state'],'UNKNOWN')
        self.assertIn('pending',restarted.entry_blocker())

    def test_wrong_account_receipt_does_not_establish_ownership(self):
        self.bad_ack=True;self.assertTrue(self.submit()['submission_unknown'])
        self.assertIsNone(self.t.row('order-one')['receipt_json'])

    def test_rejected_entry_and_cancelled_children_become_no_fill_and_are_idempotent(self):
        self.submit();self.life[11]['order']['ordStatus']='Rejected'
        for oid in (12,13):self.life[oid]['order']['ordStatus']='Canceled'
        for _ in range(2):
            self.now+=16;self.t.reconcile_one(self.t.row('order-one'),self.snapshot())
        self.assertEqual(self.local()['state'],'NO_FILL');self.assertEqual(self.t.resolution('order-one')['outcome'],'no_fill')
        self.assertIsNone(self.t.entry_blocker());self.assertEqual(len(self.posts),1)

    def test_expired_entry_cancels_only_its_suspended_children_then_waits_for_confirmation(self):
        self.submit();self.life[11]['order']['ordStatus']='Expired';self.now+=1
        self.t.reconcile_one(self.t.row('order-one'),self.snapshot())
        self.assertEqual(self.t.row('order-one')['state'],'WAITING_CHILD_CANCEL')
        self.assertEqual([p[0].split('/')[-2] for p in self.posts[1:]],['12','13'])
        for oid in (12,13):self.life[oid]['order']['ordStatus']='Canceled'
        self.now+=1;self.t.reconcile_one(self.t.row('order-one'),self.snapshot())
        self.now+=16;self.t.reconcile_one(self.t.row('order-one'),self.snapshot())
        self.assertEqual(self.t.row('order-one')['state'],'NO_FILL')

    def test_full_fill_promotes_to_real_position_and_partial_waits(self):
        self.submit();self.open(qty=1)
        self.assertEqual(self.t.row('order-one')['state'],'PARTIAL_OPEN');self.assertIsNone(self.local()['position_json'])
        # A second fill completes the entry at the same native limit.
        self.now+=2;second=self.fill(102,11,1,30000,'Buy')
        self.life[11]['order']['ordStatus']='Filled'
        for oid in (12,13):self.life[oid]['version']['orderQty']=2
        snap=self.snapshot([self.entry,second],2);self.t.reconcile_one(self.t.row('order-one'),snap)
        self.assertEqual(self.local()['state'],'BROKER_CONFIRMED_OPEN')
        self.assertFalse(json.loads(self.local()['position_json'])['assumed'])

    def test_foreign_same_contract_fill_does_not_get_adopted(self):
        self.submit();self.open();self.now+=1
        foreign=self.fill(202,777,1,30000,'Buy')
        with self.assertRaisesRegex(native.TransportError,'FOREIGN'):
            self.t.reconcile_one(self.t.row('order-one'),self.snapshot([self.entry,foreign],2))
        self.assertIsNotNone(self.t.entry_blocker())

    def test_fast_round_trip_records_actual_fills_and_net_only_after_costs_agree(self):
        self.submit();snap,net=self.complete()
        self.t.reconcile_one(self.t.row('order-one'),snap)
        self.assertEqual(self.local()['state'],'CLOSED')
        self.assertEqual(self.t.resolution('order-one')['outcome'],'closed_pnl_pending')
        self.assertIsNotNone(self.t.entry_blocker())
        self.t.reconcile_costs(snap)
        self.assertAlmostEqual(self.t.resolution('order-one')['net'],net)
        self.assertIsNone(self.t.entry_blocker())
        self.account['balance']['totalCashValue']-=1;self.now+=1;self.snapshot([*snap['fills']])
        self.assertIn('cash_change',self.t.entry_blocker())

    def test_short_loss_closes_confirmed_position_and_counts_actual_net(self):
        self.order.update(direction='SHORT',stop_price=30010.,target_price=29970.);self.prepare();self.submit();self.open()
        snap,net=self.complete(winning=False);self.t.reconcile_one(self.t.row('order-one'),snap);self.t.reconcile_costs(snap)
        self.assertEqual(self.t.resolution('order-one')['outcome'],'loss');self.assertAlmostEqual(self.t.resolution('order-one')['net'],net)

    def test_history_fill_core_or_missing_costs_cannot_certify_pnl(self):
        self.submit();snap,net=self.complete();self.t.reconcile_one(self.t.row('order-one'),snap)
        self.history[0]['qty']=20;self.t.reconcile_costs(snap)
        self.assertEqual(self.t.row('order-one')['state'],'CLOSED_PENDING_COSTS')
        self.history[0]['qty']=2;self.history[0]['fees']=0;self.history[0]['commission']=0
        self.now+=61;self.t.reconcile_costs(snap)
        self.assertEqual(self.t.row('order-one')['state'],'CLOSED_PENDING_COSTS')

    def test_unknown_submission_recovers_only_exact_client_id_and_bracket_links(self):
        self.timeout=True;self.submit();self.timeout=False;self.now+=1
        self.t.reconcile_one(self.t.row('order-one'),self.snapshot())
        self.assertIsNotNone(self.t.row('order-one')['receipt_json']);self.assertEqual(len(self.posts),1)
        with self.api.connect() as c:c.execute('UPDATE native_orders SET receipt_json=NULL')
        self.life[11]['commands'][0]['clOrdId']='unrelated'
        with self.assertRaisesRegex(native.TransportError,'EXACT_CLIENT_ID'):
            self.t.reconcile_one(self.t.row('order-one'),self.snapshot())

    def test_native_manager_exit_releases_db_lock_and_duplicate_or_unknown_close_never_resends(self):
        self.submit();self.open()
        aid='exit-one';payload=self.original_exit_fixture(aid)
        self.api._send_broker_exit=native.exit_payload
        self.exit(aid,'order-one',payload)
        self.assertEqual(self.local()['state'],'EXIT_PENDING');self.assertEqual(len(self.posts),2)
        self.life[14]=dict(order=dict(id=14,accountId=2214,contractId=100,action='Sell',ordStatus='Working'),
            version=dict(orderId=14,orderQty=2,orderType='Market'),commands=[],reports=[])
        self.t.action('order-one','close','another-action',snapshot=self.snapshot([self.entry],2))
        self.assertEqual(len(self.posts),2)

    def original_exit_fixture(self,aid='exit-one'):
        # Synthetic proof is confined to the network-denied unit test process.
        # It is never written to a policy file or treated as broker evidence.
        now=int(self.now*1000)
        self.manager_policy=dict(quote_source='crosstrade_nt8',max_quote_age_ms=3000,max_spread_ticks=4,
            broker_workflow_verification=dict(verified=True,simulator_only=False,provider='CrossTrade',
                surface='tradovate',contract='MNQZ2026',account_binding=self.t.binding,
                paper_evidence_reference='UNIT_TEST_ONLY',native_oso_oco=True,partial_fill_repair=True,
                restart_recovery=True,net_accounting=True,management=dict(full_exit=True,terminal_children=True,
                    duplicate_timeout_recovery=True,paper_evidence_reference='UNIT_TEST_ONLY',
                    verified_at_ms=now-1,expires_at_ms=now+60000)))
        self.exit_quote=dict(source='crosstrade_nt8',contract='MNQZ2026',execution_eligible=True,
            timestamp_semantics_verified=True,market_open=True,bid=30000.,ask=30000.25,as_of_ms=now)
        patch.object(execution_validation,'policy',return_value=self.manager_policy).start()
        patch('directional_market_data.QuoteClient.fetch',side_effect=lambda *a,**k:copy.deepcopy(self.exit_quote)).start()
        self.api.exit_blockers=lambda:[];self.api.frozen_parity_ok=lambda:True;self.api.runner_parity_ok=lambda:True
        payload=dict(ticker='MNQZ2026',action='exit',orderType='market',quantity=2,
            extras=dict(v3OrderId='order-one',v3ActionId=aid,v3PositionId='ct:11',
                v3Policy='V3_M3_OBFH_10M_RUNNER_V1',v3PrimaryReason='F2'))
        with self.api.connect() as c:
            c.execute("UPDATE orders SET state='EXIT_UNKNOWN'")
            c.execute('INSERT INTO actions VALUES(?,?,?,?,?,?)',(aid,'order-one',now,'EXIT_UNKNOWN',json.dumps(payload),now))
            c.execute("UPDATE exit_trades SET primary_manager='M3',primary_reason='F2',signal_time=?",(now,))
            c.execute('INSERT INTO manager_states VALUES(?,?)',('order-one',json.dumps(dict(decision='CLOSE',
                m3_confirmed=True,reason='F2',known_ms=now,boundary=dict(classification='FAILURE')))))
        return payload

    def test_partial_lifecycle_and_modified_protection_fail_closed(self):
        self.submit();self.life[12]['commands']=[dict(commandType='Modify')]
        self.open()
        self.now+=1;self.t.reconcile(self.snapshot([self.entry],2))
        self.assertEqual(self.t.row('order-one')['state'],'RECONCILIATION_REQUIRED')
        self.assertEqual(self.local()['state'],'OWNERSHIP_MISMATCH')
        self.assertIsNotNone(self.t.entry_blocker())

    def test_guard_request_survives_restart_and_waits_for_terminal_entry(self):
        self.submit();self.t.queue_exit('order-one','EOD');self.now+=1
        self.t.reconcile(self.snapshot())
        self.assertEqual(len(self.posts),2);self.assertTrue(self.posts[-1][0].endswith('/11/cancel'))
        # No retry while cancellation has only been acknowledged.
        self.now+=1;self.t.reconcile(self.snapshot());self.assertEqual(len(self.posts),2)
        for oid in (11,12,13):self.life[oid]['order']['ordStatus']='Canceled'
        self.now+=1;self.t.reconcile(self.snapshot())
        self.now+=16;self.t.reconcile(self.snapshot())
        self.assertEqual(self.t.row('order-one')['state'],'NO_FILL');self.assertEqual(len(self.posts),2)

    def test_agent_executor_routes_prepared_entry_without_webhook_fallback(self):
        import datetime as dt
        from unittest.mock import Mock
        requests=Mock();requests.post.side_effect=AssertionError('legacy webhook used')
        api=types.SimpleNamespace(mode=lambda:'LIVE',prepared_entry_blocker=lambda x:None,
            now_ms=lambda:int(self.now*1000),record_quantity=lambda oid,qty:None)
        namespace=dict(os=os,dt=dt,builder_crosstrade=native,ab_v3_live=api,requests=requests,
            execution_policy=types.SimpleNamespace(entry_blocker=lambda x:None,mode=lambda:'V3_ONLY'),
            guardrails=types.SimpleNamespace(_exec_route_id=lambda:'test-route'),
            _entry_cancel_after_sec=lambda:60,_signal_reject_after_sec=lambda:60)
        functions(ROOT/'agent.py',{'_exec_order'},namespace)
        signal=dict(dir='LONG',entry=30000,SL=29990,TP=30030,_v3_directional=True,
            _continuation_order_id='order-one',_v3_expiry_ms=self.order['expiry_ms'],_disable_partial=True,
            _exec_qty_override=2)
        with patch.dict(os.environ,dict(EXEC_WEBHOOK='',EXEC_TICKER='MNQZ2026',SESSION_SIZE_MULT='')):
            result=namespace['_exec_order'](signal)
        self.assertTrue(result['sent']);self.assertEqual(result['provider'],'crosstrade')
        requests.post.assert_not_called()
        self.assertEqual(len(self.posts),1)

    def test_guard_uses_native_outcome_instead_of_modeled_win(self):
        self.submit();snap,net=self.complete(winning=False)
        self.t.reconcile_one(self.t.row('order-one'),snap);self.t.reconcile_costs(snap)
        namespace=dict(builder_crosstrade=native)
        functions(ROOT/'guardrails.py',{'_actualize'},namespace)
        row=namespace['_actualize'](dict(continuation_order_id='order-one'),dict(outcome='win',net=999))
        self.assertEqual(row['outcome'],'loss');self.assertAlmostEqual(row['net'],net)
        self.assertEqual(row['outcome_source'],'crosstrade_owned_fills')

    def test_account_poll_invokes_reconciliation_after_commit_and_keeps_valid_equity_on_error(self):
        def callback(snapshot):
            self.assertEqual(self.sync.snapshot(),snapshot)
            raise ValueError('private broker detail')
        self.sync.after_poll=callback
        self.assertTrue(self.sync.poll())
        self.assertEqual(self.sync.state()['cash'],48961.55)
        self.assertEqual(self.sync.entry_blocker(),'broker:native_reconciliation_failed')
        self.assertNotIn('private',json.dumps(self.sync.state()))

    def test_broker_oco_order_links_and_exact_dated_history_aliases(self):
        self.submit()
        self.life[12]['order']['ocoId']=13;self.life[13]['order']['ocoId']=12
        self.open();snap,net=self.complete();self.t.reconcile_one(self.t.row('order-one'),snap)
        self.t.reconcile_costs(snap)
        self.assertEqual(self.t.row('order-one')['state'],'CLOSED')
        self.assertAlmostEqual(self.t.resolution('order-one')['net'],net)
        self.assertNotIn('MNQ1!',native.contract_aliases('MNQZ2026'))

    def test_native_routing_requires_fresh_completed_m1(self):
        namespace=dict(os=os,json=json,mode=lambda:'LIVE',native_fixed_bracket=lambda:False,exit_blockers=lambda:[],
            directional_ai_review=directional_ai_review,
            frozen_parity_ok=lambda:True,builder_broker_sync=self.sync,
            now_ms=lambda:int(self.now*1000),connect=self.api.connect,contract=self.api.contract,route=self.api.route)
        functions(ROOT/'ab_v3_live.py',{'prepared_entry_blocker','m1_fresh'},namespace)
        signal=dict(_v3_directional=True,_continuation_order_id='order-one',
            entry=30000.,SL=29990.,TP=30030.,dir='LONG')
        self.assertEqual(namespace['prepared_entry_blocker'](signal),'v3:m1_feed_not_fresh')
        self.assertEqual(self.posts,[])

    def test_changed_price_or_quantity_is_rejected_before_any_post(self):
        payload=self.payload();payload['limitPrice']+=.25
        with self.assertRaisesRegex(native.TransportError,'EXACT_PREPARED'):
            self.t.submit(payload,'order-one')
        payload=self.payload();payload['quantity']=3
        with self.assertRaisesRegex(native.TransportError,'EXACT_PREPARED'):
            self.t.submit(payload,'order-one')
        self.assertEqual(self.posts,[])

    def test_oco_stop_without_parent_is_owned_but_foreign_or_missing_anchor_is_rejected(self):
        self.submit();self.open()
        self.assertEqual(self.local()['state'],'BROKER_CONFIRMED_OPEN')
        self.life[12]['order']['parentId']=999
        self.now+=1
        with self.assertRaisesRegex(native.TransportError,'PARENT_CHAIN'):
            self.t.reconcile_one(self.t.row('order-one'),self.snapshot([self.entry],2))

    def test_terminal_lifecycle_cache_verifies_current_raw_order_and_survives_restart(self):
        self.submit();self.life[11]['order']['ordStatus']='Canceled'
        requests=[];original=self.request
        def record(method,path,body=None):
            requests.append(path);return original(method,path,body)
        self.t.request=record;self.t.lifecycle(11)
        self.assertTrue(requests[-1].endswith('/lifecycle'))
        restarted=native.Transport(self.api,self.sync,request=record,clock=lambda:self.now)
        restarted.lifecycle(11);self.assertTrue(requests[-1].endswith('/orders/11'))
        self.life[11]['order']['ordStatus']='Working'
        restarted.lifecycle(11);self.assertTrue(requests[-1].endswith('/lifecycle'))


if __name__=='__main__':unittest.main()


