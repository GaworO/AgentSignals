from copy import deepcopy
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import tempfile
import unittest
from broker_feedback import BrokerFeedback
from trading_loop import TradingLoop
from store import Store
from test_broker_feedback import CONFIG,event
from test_nq_analysis import nq_fixture
from entry_review import compile_review
from daily_atr import Policy


class TradingLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name,analysis_symbol='NQ');self.feedback=BrokerFeedback(self.store,CONFIG)
        p,d=nq_fixture();self.t=p['as_of'];self.source=compile_review(p,d,selected_at=self.t+1,now=self.t+1,budget=250,cap=40)
        self.source.update(frozen_at=self.t+1,revision='test',ai_context=dict(state='PERMITTED',direction='long'),
            scheduled_news=dict(state='CLEAR'),news_entry_reasons=[],account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
        self.source['cutoff']=self.t
        Path(self.tmp.name,'account_snapshot.json').write_text(json.dumps(dict(balance=50000,minimum_balance=48000,target_balance=53000,
            daily_loss_remaining=1000,committed_risk_usd=0,committed_micro_contracts=0,as_of=datetime.fromtimestamp(self.t,ZoneInfo('America/New_York')).date().isoformat(),reported_at=self.t)))
        test=self
        class Observer:
            revision='test';atr_policy=Policy();max_contracts=40
            class news:
                @staticmethod
                def state(now):return dict(state='CLEAR',events=[])
            def state(self):return dict(records=[test.source])
            def audit(self,revision,cutoff):return deepcopy(test.source)
        self.observer=Observer();self.sequence=0
        # Lifecycle unit fixture has no market-history feed. Production defaults
        # require it; the separate market-readiness tests exercise that guard.
        self.loop=TradingLoop(self.store,self.observer,self.feedback,dict(TANJA_TRADING_LOOP_MODE='paper',TANJA_PAPER_SLIPPAGE_POINTS='0',TANJA_FEE_PER_CONTRACT_SIDE='0',TANJA_REQUIRE_MARKET_READINESS='false'))

    def quotes(self,at,price):
        for sym in ('ES','NQ','MNQ'):
            self.sequence+=1;e=event(self.sequence,'quote',at)
            value=price if sym=='MNQ' else self.source['packet']['evidence']['NQ:trigger-bar']['close'] if sym=='NQ' else 60
            e['data'].update(symbol=sym,contract=sym+'Z2026',bid=value,ask=value+.25,last=value)
            self.feedback.ingest(e,at)

    def test_background_path_enters_partials_restarts_and_exits_without_broker_orders(self):
        plan=self.source['compiled']['plan'];entry=plan['entry_reference']['price']
        self.quotes(self.t+1,entry);self.loop.process_one(self.t+1)
        self.assertEqual(self.loop.sim.snapshot()['runs'][0]['state'],'ENTRY_PENDING')
        self.quotes(self.t+2,entry);self.loop.process_one(self.t+2)
        run=self.loop.sim.snapshot()['runs'][0];q=run['quantity'];self.assertEqual(run['state'],'OPEN')
        self.assertFalse(self.loop.process_one(self.t+2));self.assertFalse(self.loop.process_one(self.t+3))
        objective=run['management_spec']['partials'][0]['objective']['price']
        self.quotes(self.t+4,objective);self.loop.process_one(self.t+4)
        self.assertEqual(self.loop.sim.snapshot()['runs'][0]['pending']['quantity'],q//3)
        self.loop=TradingLoop(self.store,self.observer,self.feedback,dict(TANJA_TRADING_LOOP_MODE='paper',TANJA_PAPER_SLIPPAGE_POINTS='0',TANJA_FEE_PER_CONTRACT_SIDE='0',TANJA_REQUIRE_MARKET_READINESS='false'))
        self.quotes(self.t+5,objective);self.loop.process_one(self.t+5)
        self.assertEqual(self.loop.sim.snapshot()['runs'][0]['quantity'],q-q//3)
        self.quotes(self.t+6,run['target']);self.loop.process_one(self.t+6)
        self.assertEqual(self.loop.sim.snapshot()['runs'][0]['state'],'CLOSED')
        self.assertEqual(self.loop.sim.snapshot()['orders_sent'],0)
        self.quotes(self.t+7,entry);self.loop.process_one(self.t+7)
        self.assertEqual(len(self.loop.sim.snapshot()['runs']),1)

    def test_stale_quote_cannot_enter_and_default_loop_is_off(self):
        self.quotes(self.t+1,112);self.loop.process_one(self.t+4)
        self.assertEqual(self.loop.state(self.t+4)['status'],'LIVE_QUOTES_REQUIRED')
        self.assertEqual(self.loop.sim.snapshot()['runs'],[])
        off=TradingLoop(self.store,self.observer,self.feedback,{})
        self.assertFalse(off.process_one(self.t+4));self.assertFalse(off.state(self.t+4)['orders_enabled'])

    def test_unresolved_accounting_blocks_new_entries(self):
        from execution_ledger import ExecutionLedger
        from test_execution_ledger import LEDGER_CONFIG
        self.feedback.accounting=ExecutionLedger(self.store,LEDGER_CONFIG)
        self.quotes(self.t+1,112);self.loop.process_one(self.t+1)
        self.assertEqual(self.loop.sim.snapshot()['runs'],[])
        self.assertIn('account',self.loop.state(self.t+1)['reason'].lower())

    def test_unresolved_accounting_keeps_managing_an_existing_position(self):
        entry=self.source['compiled']['plan']['entry_reference']['price']
        self.quotes(self.t+1,entry);self.loop.process_one(self.t+1)
        self.quotes(self.t+2,entry);self.loop.process_one(self.t+2)
        run=self.loop.sim.snapshot()['runs'][0];self.assertEqual(run['state'],'OPEN')
        from execution_ledger import ExecutionLedger
        from test_execution_ledger import LEDGER_CONFIG
        self.feedback.accounting=ExecutionLedger(self.store,LEDGER_CONFIG)
        self.quotes(self.t+3,run['stop']-1);self.loop.process_one(self.t+3)
        self.quotes(self.t+4,run['stop']-1);self.loop.process_one(self.t+4)
        self.assertEqual(self.loop.sim.snapshot()['runs'][0]['state'],'CLOSED')

    def test_entry_draft_is_idempotent_test_only_and_uses_mnq_prices(self):
        first=self.loop.prepare_entry(self.source,self.t+1);again=self.loop.prepare_entry(self.source,self.t+2)
        self.assertEqual(first,again);self.assertTrue(first['test']);self.assertEqual(first['ticker'],'MNQZ2026')
        self.assertEqual(first['stopLoss']['stopPrice'],self.source['compiled']['price_review']['stop'])
        self.assertEqual(len(self.loop.state(self.t+2)['intents']),1)

    def test_production_default_cannot_enter_without_market_readiness(self):
        self.loop.require_market_readiness=True
        self.quotes(self.t+1,112);self.loop.process_one(self.t+1)
        self.assertEqual(self.loop.sim.snapshot()['runs'],[])
        self.assertIn('MARKET_READINESS_REQUIRED',self.loop.state(self.t+1)['reason'])

    def test_one_attempt_per_scanner_setup_survives_new_inversions(self):
        from entry_rules import compile_entry
        selected=self.source['selected']['selection'];selected['setup_id']='same-selected-swing'
        self.source['compiled']=compile_entry(self.source['packet'],selected,self.source['selected']['observations'],now=self.t+1)
        self.quotes(self.t+1,112);self.loop.process_one(self.t+1)
        self.quotes(self.t+2,112);self.loop.process_one(self.t+2)
        self.quotes(self.t+3,125);self.loop.process_one(self.t+3)
        selected['id']='new-inversion-same-setup'
        self.source['compiled']=compile_entry(self.source['packet'],selected,self.source['selected']['observations'],now=self.t+4)
        self.quotes(self.t+4,112);self.loop.process_one(self.t+4)
        self.assertEqual(len(self.loop.sim.snapshot()['runs']),1)
        self.assertEqual(self.loop.state(self.t+4)['status'],'SETUP_ALREADY_ATTEMPTED')


if __name__=='__main__':unittest.main()
