from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import tempfile
import unittest
from store import Store
from broker_feedback import BrokerFeedback
from trading_loop import TradingLoop


CONFIG=dict(TANJA_BROKER_FEEDBACK_TOKEN='x'*40,TANJA_BROKER_ACCOUNT_ID='synthetic-account',
    TANJA_EXECUTION_CONTRACT='MNQZ2026',TANJA_BROKER_FEEDBACK_SOURCE='synthetic-tests-only')
NOW=int(datetime(2026,10,14,10,tzinfo=ZoneInfo('America/New_York')).timestamp())


def event(sequence=1,kind='snapshot',now=NOW):
    start=int(datetime.fromtimestamp(now,ZoneInfo('America/New_York')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
    data=dict(symbol='MNQ',contract='MNQZ2026',bid=100,ask=100.25,last=100) if kind=='quote' else dict(
        complete=True,scope='entire_broker_account',history_start=start,history_end=now,
        account=dict(balance=50000,minimum_balance=48000,target_balance=53000,daily_loss_remaining=1000,
            committed_risk_usd=0,committed_micro_contracts=0),positions=[],orders=[],fills=[],closed_trades=[])
    return dict(schema_version=1,event_id='synthetic:'+str(sequence),sequence=sequence,account_id=CONFIG['TANJA_BROKER_ACCOUNT_ID'],
        source=CONFIG['TANJA_BROKER_FEEDBACK_SOURCE'],kind=kind,observed_at=now,data=data)


class BrokerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.feedback=BrokerFeedback(self.store,CONFIG)

    def test_account_wide_net_losses_exclude_breakeven_and_partial_trade_counts(self):
        e=event()
        for i,(symbol,gross,fee) in enumerate([('MNQZ2026',-10,2),('ESZ2026',-5,2),('NQZ2026',2,2),('MESZ2026',20,2)]):
            e['data']['closed_trades'].append(dict(trade_id=str(i),contract=symbol,opened_at=NOW-100,closed_at=NOW-1,gross_usd=gross,fees_usd=fee))
        self.feedback.ingest(e,NOW)
        self.assertEqual(self.feedback.state(NOW)['loss_count'],2)
        self.assertEqual(self.feedback.ingest(e,NOW)['status'],'duplicate')
        self.assertEqual(BrokerFeedback(self.store,CONFIG).state(NOW)['loss_count'],2)
        self.assertIsNone(self.feedback.state(NOW+6)['loss_count'])

    def test_missing_day_history_or_strategy_only_snapshot_is_rejected(self):
        for patch in (dict(complete=False),dict(scope='tanja_strategy_only'),dict(history_start=NOW-30),dict(history_end=NOW-3)):
            e=event();e['data'].update(patch)
            with self.assertRaises(ValueError):self.feedback.ingest(e,NOW)

    def test_identity_conflicts_cannot_rewrite_prior_fill_or_pnl(self):
        e=event();trade=dict(trade_id='loss',contract='ESZ2026',opened_at=NOW-10,closed_at=NOW-1,gross_usd=-20,fees_usd=2)
        e['data']['closed_trades']=[trade];self.feedback.ingest(e,NOW)
        e=event(2);e['data']['closed_trades']=[dict(trade,gross_usd=100)]
        with self.assertRaisesRegex(ValueError,'BROKER_TRADE_CONFLICT'):self.feedback.ingest(e,NOW)
        e=event();e['data']['account']['balance']=60000
        with self.assertRaisesRegex(ValueError,'BROKER_EVENT_CONFLICT'):self.feedback.ingest(e,NOW)

    def test_stale_quotes_wrong_account_contract_and_non_advancing_sequence_fail(self):
        for mutation in ('old','account','contract','spread','boolean-schema'):
            e=event(kind='quote')
            if mutation=='old':e['observed_at']-=3
            elif mutation=='account':e['account_id']='other-account'
            elif mutation=='contract':e['data']['contract']='MNQH2027'
            elif mutation=='boolean-schema':e['schema_version']=True
            else:e['data']['bid']=101
            with self.assertRaises(ValueError):self.feedback.ingest(e,NOW)
        self.feedback.ingest(event(2),NOW)
        with self.assertRaisesRegex(ValueError,'BROKER_SEQUENCE_MUST_ADVANCE'):self.feedback.ingest(event(1),NOW)

    def test_broker_mode_never_treats_traderspost_receipt_or_paper_as_broker_state(self):
        class Observer:revision='synthetic'
        loop=TradingLoop(self.store,Observer(),self.feedback,dict(TANJA_TRADING_LOOP_MODE='broker',TANJA_REQUIRE_MARKET_READINESS='false'))
        loop.process_one(NOW)
        self.assertEqual(loop.state(NOW)['status'],'LIVE_QUOTES_REQUIRED')
        for i,symbol in enumerate(('MNQ','ES','NQ'),1):
            e=event(i,kind='quote');e['data'].update(symbol=symbol,contract=symbol+'Z2026');self.feedback.ingest(e,NOW)
        loop.process_one(NOW);self.assertEqual(loop.state(NOW)['status'],'BROKER_FEEDBACK_REQUIRED')
        self.feedback.ingest(event(4),NOW);loop.process_one(NOW)
        self.assertEqual(loop.state(NOW)['status'],'BROKER_MANAGEMENT_ADAPTER_REQUIRED')
        self.assertFalse(loop.state(NOW)['orders_enabled'])

    def test_feedback_http_requires_separate_bearer_authentication(self):
        from app import App
        import io
        a=App(dict(CONFIG,DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='f'*40,TANJA_DASHBOARD_PASSWORD='p'*20))
        def request(token):
            body=json.dumps(event(now=int(__import__('time').time()))).encode();result={}
            env=dict(PATH_INFO='/api/broker-feedback',REQUEST_METHOD='POST',CONTENT_LENGTH=str(len(body)),
                HTTP_AUTHORIZATION=token,**{'wsgi.input':io.BytesIO(body)})
            a(env,lambda status,headers:result.update(status=status));return result['status']
        self.assertTrue(request('Bearer wrong').startswith('401'))
        self.assertTrue(request('Bearer '+CONFIG['TANJA_BROKER_FEEDBACK_TOKEN']).startswith('200'))


if __name__=='__main__':unittest.main()
