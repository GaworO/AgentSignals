from copy import deepcopy
from datetime import datetime,timezone
import json
import tempfile
import unittest
from store import Store
from execution_ledger import ExecutionLedger
from test_broker_feedback import NOW,CONFIG,event

LEDGER_CONFIG=dict(CONFIG,TANJA_ACCOUNTING_PROVIDER='crosstrade',TANJA_BROKER_ACCOUNT_ID='123',
    TANJA_CROSSTRADE_ENVIRONMENT='demo',TANJA_ACCOUNTING_CONTRACT_SPECS=json.dumps({
        s+' 12-26':dict(contract=s+'Z2026',multiplier=str(m),tick_size='0.25')
        for s,m in [('MNQ',2),('ES',50),('NQ',20),('MES',5)]}))


def fill(i,action='Buy',qty=1,price=100,symbol='MNQ',at=None,fee='.5'):
    return dict(executionId='tv:demo:'+str(i),fillId=i,orderId=i,accountId=123,accountName='synthetic',environment='demo',
        instrument=symbol+' 12-26',qty=qty,price=price,action=action,commission=fee,fees='0',
        timestamp=datetime.fromtimestamp(NOW-600+i if at is None else at,timezone.utc).isoformat())


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.ledger=ExecutionLedger(self.store,LEDGER_CONFIG)

    def state(self):return self.ledger.state(NOW)

    def seal(self,end=NOW,start=NOW-36000):
        iso=lambda at:datetime.fromtimestamp(at,timezone.utc).isoformat()
        rows=[r for r in self.ledger.records() if start<=r['at']<=end]
        positions=self.ledger.calculate(rows)[1]
        doc=dict(account_id='123',environment='demo',source='broker_statement',evidence_sha256='a'*64,
            flat_at=iso(start),through=iso(end),fingerprint=self.ledger.fingerprint(rows),positions=positions,
            fees_confirmed=[r['execution_id'] for r in rows])
        self.ledger.reconcile(doc,NOW);self.ledger.note_sync(NOW,'CAPTURED');return doc

    def test_partial_exits_aggregate_once_manual_other_instruments_and_breakeven(self):
        rows=[fill(1,qty=3),fill(2,'Sell',price=99),fill(3,'Sell',qty=2,price=99),
            fill(4,symbol='ES'),fill(5,'Sell',price=99,symbol='ES'),
            fill(6,symbol='NQ'),fill(7,'Sell',price=100.25,symbol='NQ',fee='4.5')]
        self.ledger.ingest(rows,NOW)
        state=self.state();self.assertIsNone(state['loss_count']);self.assertEqual(state['provisional_loss_count'],2)
        self.assertEqual(len(state['trades']),3);self.assertEqual(state['trades'][0]['net_usd'],'-7.50000000')
        self.assertEqual(state['trades'][2]['net_usd'],'0.00000000')
        self.seal();self.assertEqual(self.state()['loss_count'],2)

    def test_delayed_fees_revise_result_without_duplicating_or_rewriting_audit(self):
        rows=[fill(1,fee='0'),fill(2,'Sell',price=100.25,fee='0')];self.ledger.ingest(rows,NOW)
        self.assertEqual(self.state()['provisional_loss_count'],0)
        self.seal();self.assertEqual(self.state()['loss_count'],0)
        for r in rows:r['commission']='.5'
        self.ledger.ingest(rows,NOW)
        state=self.state();self.assertIsNone(state['loss_count']);self.assertEqual(state['provisional_loss_count'],1)
        self.assertIn('RECONCILED_EXECUTIONS_OR_FEES_CHANGED',state['reasons']);self.assertEqual(state['cost_observations'],4)
        self.ledger.ingest(rows,NOW);self.assertEqual(self.state()['cost_observations'],4)
        self.seal();self.assertEqual(self.state()['loss_count'],1)
        self.assertEqual(ExecutionLedger(self.store,LEDGER_CONFIG).state(NOW)['loss_count'],1)

    def test_positive_or_zero_fees_never_automatically_mean_final(self):
        for fee in ('0','.5'):
            self.ledger.ingest([fill(1,fee=fee),fill(2,'Sell',fee=fee)],NOW)
            self.ledger.note_sync(NOW,'CAPTURED')
            self.assertIn('FILL_FEES_NOT_FINAL',self.state()['reasons']);self.assertIsNone(self.state()['loss_count'])

    def test_fee_correction_then_reversion_cannot_resurrect_old_confirmation(self):
        rows=[fill(1),fill(2,'Sell')];self.ledger.ingest(rows,NOW);self.seal()
        self.ledger.ingest([fill(1,fee='1.5')],NOW)
        self.ledger.ingest([fill(1)],NOW)
        self.assertIn('RECONCILED_EXECUTIONS_OR_FEES_CHANGED',self.state()['reasons'])
        self.assertEqual(len(self.ledger.cost_history()),4)
        self.seal();self.assertIsNotNone(self.state()['loss_count'])

    def test_missing_history_rows_are_retained_and_new_late_rows_invalidate_seal(self):
        self.ledger.ingest([fill(1),fill(2,'Sell')],NOW);self.seal()
        self.ledger.ingest([],NOW);self.assertEqual(self.state()['fill_count'],2)
        self.ledger.ingest([fill(3,symbol='MES'),fill(4,'Sell',symbol='MES')],NOW)
        self.assertIn('RECONCILED_EXECUTIONS_OR_FEES_CHANGED',self.state()['reasons'])

    def test_flips_allocate_fees_between_closed_trade_and_new_position(self):
        self.ledger.ingest([fill(1,qty=2,fee='2'),fill(2,'Sell',qty=3,price=99,fee='3'),fill(3,'Buy',price=98,fee='1')],NOW)
        trades=self.state()['trades'];self.assertEqual(len(trades),2)
        self.assertEqual(trades[0]['net_usd'],'-8.00000000');self.assertEqual(trades[1]['net_usd'],'0.00000000')
        self.assertEqual(self.state()['positions'],{})

    def test_fractional_average_prices_do_not_turn_exact_breakeven_into_loss(self):
        rows=[fill(1,qty=2,price=100,fee='0'),fill(2,qty=1,price=100.75,fee='0'),fill(3,'Sell',qty=3,price=100.25,fee='0')]
        self.ledger.ingest(rows,NOW);self.assertEqual(self.state()['trades'][0]['net_usd'],'0.00000000')
        self.assertEqual(self.state()['provisional_loss_count'],0)

    def test_equivalent_decimal_serialization_is_idempotent(self):
        self.ledger.ingest([fill(1,price=100,fee='.50')],NOW)
        self.ledger.ingest([fill(1,price='100.00',fee='0.500')],NOW)
        self.assertEqual(self.state()['fill_count'],1);self.assertEqual(self.state()['cost_observations'],1)

    def test_day_is_new_york_close_date_not_session_trade_date(self):
        midnight=datetime.fromtimestamp(NOW,timezone.utc).replace(hour=4,minute=0,second=0,microsecond=0).timestamp()
        self.ledger.ingest([fill(1,at=midnight-60),fill(2,'Sell',price=99,at=midnight+60),
            fill(3,at=midnight-180,symbol='ES'),fill(4,'Sell',price=99,at=midnight-120,symbol='ES')],NOW)
        self.assertEqual(self.state()['provisional_loss_count'],1)

    def test_core_corrections_are_blocked_durably_and_batch_conflicts_rejected(self):
        self.ledger.ingest([fill(1)],NOW)
        with self.assertRaisesRegex(ValueError,'CORE_CONFLICT'):self.ledger.ingest([fill(1,price=101)],NOW)
        self.assertIn('EXECUTION_CORE_CONFLICT',self.state()['reasons'])
        self.assertEqual(self.ledger.records()[0]['price'],'100')
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.ledger.ingest([fill(2),fill(2,price=101)],NOW)

    def test_wrong_account_unknown_instruments_missing_costs_and_ties_block(self):
        for patch in ({'accountId':999},{'environment':'live'},{'instrument':'CL 12-26'},{'qty':True}):
            row=fill(1);row.update(patch)
            with self.assertRaises(ValueError):self.ledger.ingest([row],NOW)
        self.ledger.ingest([fill(1,fee=None),fill(2,'Sell',at=NOW-599)],NOW)
        self.assertIn('FILL_COSTS_MISSING',self.state()['reasons'])
        self.assertIn('SIMULTANEOUS_FILLS_ORDER_UNRESOLVED',self.state()['reasons'])
        with self.assertRaisesRegex(ValueError,'UNRESOLVED'):self.seal()

    def test_old_or_post_midnight_anchor_cannot_authorize_current_entries(self):
        self.ledger.ingest([fill(1),fill(2,'Sell')],NOW)
        self.seal(end=NOW-10);self.assertIsNone(self.state()['loss_count'])
        self.seal(start=NOW-700);self.assertIsNone(self.state()['loss_count'])
        self.seal();self.assertIsNotNone(self.state()['loss_count'])
        self.assertIsNone(self.ledger.state(NOW+6)['loss_count'])

    def test_selected_ledger_overrides_legacy_claim_of_zero_losses(self):
        from broker_feedback import BrokerFeedback
        feedback=BrokerFeedback(self.store,LEDGER_CONFIG);msg=event();msg['account_id']='123'
        feedback.ingest(msg,NOW)
        self.assertFalse(feedback.state(NOW)['account_verified']);self.assertIsNone(feedback.state(NOW)['loss_count'])


if __name__=='__main__':unittest.main()
