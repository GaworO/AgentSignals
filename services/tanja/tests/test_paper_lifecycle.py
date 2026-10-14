import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from paper_lifecycle import PaperLifecycle
import test_selection_observer as observer_tests


def observer_decision(direction='long'):
    case = observer_tests.SelectionObserverTests()
    case.setUp()
    if direction == 'short':
        from test_short_selector import short_fixture
        markets, _ = short_fixture()
        with case.s.connect() as db:
            for symbol, rows in markets.items():
                table = 'collection_bars' if symbol == 'NQ' else 'bars'
                for b in rows:
                    payload = dict(symbol=symbol, bar_open_ms=b['time']*1000, bar_close_ms=(b['time']+60)*1000,
                                   **{k:b[k] for k in ('open','high','low','close')})
                    db.execute(f'UPDATE {table} SET payload=? WHERE symbol=? AND start=?', (json.dumps(payload), symbol, b['time']))
    case.add_review(direction); case.o.process_one(case.t+1)
    decision = case.o.audit(case.o.revision, case.t)
    case.doCleanups()
    return decision, case.t


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'paper.sqlite3'
        self.sim = PaperLifecycle(self.path,strategy_mode='legacy_user_core', slippage_points=.25, fee_per_side=.50)
        self.d, self.t = observer_decision()
        self.n = 0

    def quote(self, at, value, **kw):
        self.n += 1
        return self.sim.quote(str(self.n), at=at, received_at=at, market_price=value, **kw)

    def run_state(self): return self.sim.snapshot()['runs'][-1]

    def enter(self, direction='long'):
        if direction == 'short': self.d, self.t = observer_decision('short')
        p = self.d['compiled']['plan']; self.sign = 1 if direction == 'long' else -1
        self.ref = p['entry_reference']['price']
        self.objective = dict(price=self.ref+self.sign*3, available_at=self.t, source='Synthetic selected objective')
        self.sim.submit('entry', decision=self.d, objective=self.objective, now=self.t+1)
        r = self.quote(self.t+2, self.ref)
        self.assertEqual(r['state'], 'OPEN', r)
        return self.run_state()

    def test_long_and_short_full_entry_partials_trailing_target_lifecycle(self):
        for direction in ('long','short'):
            with self.subTest(direction=direction):
                self.path = Path(self.tmp.name)/(direction+'.sqlite3')
                self.sim = PaperLifecycle(self.path,strategy_mode='legacy_user_core', slippage_points=.25, fee_per_side=.5)
                run = self.enter(direction); base = run['original_quantity']; q = base//5
                self.assertGreaterEqual(q,1)
                r = self.quote(self.t+3,self.objective['price']); self.assertEqual(r['pending']['kind'],'LOCAL_PARTIAL')
                self.assertEqual(self.run_state()['quantity'],base)
                self.quote(self.t+4,self.objective['price']); self.assertEqual(self.run_state()['quantity'],base-q)
                # Complete ES RTH history, with an entirely post-entry new extreme.
                start = self.t-20*60
                es = [dict(time=t,open=60,high=61,low=59,close=60) for t in range(start,self.t+120,60)]
                es[-1]['high' if self.sign==1 else 'low'] = 62 if self.sign==1 else 58
                r = self.quote(self.t+120,self.objective['price'],es_bars=es)
                self.assertEqual(r['pending']['kind'],'ES_PARTIAL')
                self.quote(self.t+121,self.objective['price'],es_bars=es)
                self.assertEqual(self.run_state()['quantity'],base-2*q)
                # News protection before release, no news-only partial.
                news = [dict(at=self.t+240,known_at=self.t)]
                r = self.quote(self.t+180,self.ref+self.sign*5,news_events=news)
                self.assertEqual(r['pending']['kind'],'STOP')
                self.quote(self.t+181,self.ref+self.sign*5,news_events=news)
                self.assertEqual(self.run_state()['stop'],run['base_entry']-self.sign*1.5)
                # Mirrored strict M1 swing with a closed right-hand neighbour.
                rows = [dict(time=self.t+180+i*60,open=self.ref+5,high=self.ref+7,low=self.ref+3,close=self.ref+5) for i in range(3)]
                rows[1]['low']=self.ref+2
                if self.sign == -1:
                    rows=[dict(b,open=2*self.ref-b['open'],close=2*self.ref-b['close'],high=2*self.ref-b['low'],low=2*self.ref-b['high']) for b in rows]
                self.quote(self.t+360,self.ref+self.sign*6,mnq_bars=rows)
                r = self.run_state(); self.assertEqual(r['pending']['kind'],'STOP')
                self.quote(self.t+361,self.ref+self.sign*6,mnq_bars=rows)
                self.assertEqual(self.run_state()['stop'],self.ref+self.sign*1.75)
                self.quote(self.t+362,run['target'])
                end = self.run_state(); self.assertEqual(end['state'],'CLOSED'); self.assertEqual(end['quantity'],0)
                self.assertEqual([x['operation'] for x in end['ledger'] if x['event']=='SIMULATED_FILL'],['ENTRY','LOCAL_PARTIAL','ES_PARTIAL','TARGET'])
                self.assertEqual(end['ledger'][-1]['event'],'SIMULATED_BRACKET_CANCELLED')
                self.assertEqual(self.sim.snapshot()['orders_sent'],0)

    def test_pending_partial_stop_wins_and_cancels_request(self):
        run=self.enter(); self.quote(self.t+3,self.objective['price'])
        self.quote(self.t+4,self.objective['price'],acknowledgement='hold')
        self.assertFalse(self.run_state()['local_done'])
        self.quote(self.t+5,run['stop']-2,acknowledgement='hold')
        end=self.run_state(); self.assertEqual(end['state'],'CLOSED')
        fills=[e for e in end['ledger'] if e['event']=='SIMULATED_FILL']
        self.assertEqual([e['operation'] for e in fills],['ENTRY','STOP'])
        self.assertEqual(fills[-1]['price'],run['stop']-2.25)
        self.assertTrue(any(e['event']=='SIMULATED_REQUEST_CANCELLED' for e in end['ledger']))

    def test_duplicate_restart_conflict_and_time_regression(self):
        self.enter()
        args=dict(at=self.t+3,received_at=self.t+3,market_price=self.objective['price'])
        first=self.sim.quote('retry',**args)
        self.sim=PaperLifecycle(self.path,strategy_mode='legacy_user_core',slippage_points=.25,fee_per_side=.5)
        again=self.sim.quote('retry',**args); self.assertTrue(again.pop('duplicate')); self.assertEqual(first,again)
        with self.assertRaises(ValueError):self.sim.quote('retry',**dict(args,market_price=self.ref))
        with self.assertRaises(ValueError):self.sim.quote('older',**dict(args,at=self.t+2,received_at=self.t+2))
        with self.assertRaises(ValueError):PaperLifecycle(self.path,strategy_mode='legacy_user_core',slippage_points=0,fee_per_side=0)

    def test_rejected_request_and_delayed_entry_expiry(self):
        self.enter(); self.quote(self.t+3,self.objective['price'])
        r=self.quote(self.t+4,self.objective['price'],acknowledgement='reject')
        self.assertEqual(r['result'],'REQUEST_REJECTED'); self.assertFalse(self.run_state()['local_done'])
        self.assertIsNone(self.run_state()['pending'])
        with self.assertRaises(ValueError):self.quote(self.t+5,self.ref,acknowledgement='unknown')

    def test_cost_inclusive_entry_risk_and_no_fill_same_quote(self):
        p=self.d['compiled']['plan']; ref=p['entry_reference']['price']
        objective=dict(price=ref+3,available_at=self.t,source='fixture')
        self.sim.submit('s',decision=self.d,objective=objective,now=self.t+1)
        self.sim.quote('same_market_time',at=self.t+1,received_at=self.t+2,market_price=ref)
        self.assertEqual(self.run_state()['state'],'ENTRY_PENDING')
        self.quote(self.t+3,ref+5)
        self.assertEqual(self.run_state()['state'],'REJECTED')

    def test_future_unknown_bars_and_news_do_not_change_decision(self):
        self.enter()
        bars=[dict(time=self.t+60,open=100,high=101,low=99,close=100)]
        r=self.quote(self.t+3,self.ref,mnq_bars=bars,es_bars=bars,news_events=[dict(at=self.t+60,known_at=self.t+100)])
        self.assertEqual(r['result'],'NO_ACTION')
        with self.assertRaises(ValueError):self.sim.quote('stale',at=self.t+4,received_at=self.t+7,market_price=self.ref)

    def test_final_target_priority_over_both_partials(self):
        run=self.enter(); self.quote(self.t+3,run['target'])
        self.assertEqual([x['operation'] for x in self.run_state()['ledger'] if x['event']=='SIMULATED_FILL'],['ENTRY','TARGET'])

    def test_one_add_shares_risk_and_preserves_base_quantity(self):
        run=self.enter()
        self.quote(self.t+3,self.objective['price']); self.quote(self.t+4,self.objective['price'])
        # Synthetic later gated decision uses the actual observer schema. It is
        # not a claimed historical setup or a fresh paid AI answer.
        d=deepcopy(self.d); p=d['compiled']['plan']
        d['frozen_at']=self.t+301; p.update(plan_id='synthetic-later-M5',trigger_available_at=self.t+300,context_valid_until=self.t+315)
        d['account_guard']['max_contracts']=40
        self.quote(self.t+301,self.ref,add_decision=d)
        self.assertEqual(self.run_state()['pending']['kind'],'ADD')
        self.quote(self.t+302,self.ref)
        added=self.run_state(); self.assertEqual(added['additions_done'],1)
        self.assertLessEqual(added['modeled_stop_risk_usd'],added['budget'])
        self.assertEqual(added['original_quantity'],run['original_quantity'])
        self.quote(self.t+303,self.ref,add_decision=d)
        self.assertIsNone(self.run_state()['pending'])
        self.quote(self.t+304,self.objective['price']); self.quote(self.t+305,self.objective['price'])
        self.assertEqual(self.run_state()['quantity'],added['quantity']) # local partial cannot repeat after an add

    def test_no_add_capacity_and_stale_add(self):
        self.enter(); d=deepcopy(self.d); p=d['compiled']['plan']
        d['frozen_at']=self.t+301; p.update(plan_id='later',trigger_available_at=self.t+300,context_valid_until=self.t+315)
        d['account_guard']['max_contracts']=self.run_state()['quantity']
        self.quote(self.t+301,self.ref,add_decision=d)
        self.assertIsNone(self.run_state()['pending'])
        d['account_guard']['max_contracts']=40
        self.quote(self.t+320,self.ref,add_decision=d)
        self.assertIsNone(self.run_state()['pending'])

    def test_entry_expires_and_newly_known_news_cancels_pending_entry(self):
        p=self.d['compiled']['plan']; ref=p['entry_reference']['price']
        obj=dict(price=ref+3,available_at=self.t,source='fixture')
        self.sim.submit('s',decision=self.d,objective=obj,now=self.t+1)
        self.quote(p['context_valid_until'],ref,acknowledgement='hold')
        self.assertEqual(self.run_state()['state'],'EXPIRED')
        sim=PaperLifecycle(Path(self.tmp.name)/'news.sqlite3',strategy_mode='legacy_user_core',slippage_points=.25,fee_per_side=.5)
        sim.submit('s',decision=self.d,objective=obj,now=self.t+1)
        r=sim.quote('q',at=self.t+2,received_at=self.t+2,market_price=ref,news_events=[dict(at=self.t+180,known_at=self.t+2)])
        self.assertEqual(r['state'],'CANCELLED'); self.assertEqual(r['quantity'],0)

    def test_stop_amendment_not_effective_until_acknowledged(self):
        run=self.enter(); news=[dict(at=self.t+120,known_at=self.t)]
        self.quote(self.t+60,self.ref+2,news_events=news)
        self.assertEqual(self.run_state()['stop'],run['stop'])
        self.quote(self.t+61,self.ref+2,news_events=news,acknowledgement='hold')
        self.assertEqual(self.run_state()['stop'],run['stop'])
        self.quote(self.t+62,self.ref+2,news_events=news)
        self.assertEqual(self.run_state()['stop'],run['base_entry']-1.5)
        self.assertFalse(self.run_state()['local_done'])

    def test_add_fill_rejects_cost_overrun_and_short_add_works(self):
        self.enter()
        d=deepcopy(self.d); p=d['compiled']['plan']
        d['frozen_at']=self.t+301; p.update(plan_id='later',trigger_available_at=self.t+300,context_valid_until=self.t+315)
        d['account_guard']['max_contracts']=40
        self.quote(self.t+301,self.ref,add_decision=d)
        self.assertEqual(self.run_state()['pending']['kind'],'ADD')
        self.quote(self.t+302,self.ref+2)
        self.assertEqual(self.run_state()['additions_done'],0)
        self.path=Path(self.tmp.name)/'short-add.sqlite3'
        self.sim=PaperLifecycle(self.path,strategy_mode='legacy_user_core',slippage_points=.25,fee_per_side=.5)
        run=self.enter('short')
        self.quote(self.t+3,self.objective['price']); self.quote(self.t+4,self.objective['price'])
        d=deepcopy(self.d); p=d['compiled']['plan']
        d['frozen_at']=self.t+301; p.update(plan_id='later-short',trigger_available_at=self.t+300,context_valid_until=self.t+315)
        d['account_guard']['max_contracts']=40
        self.quote(self.t+301,self.ref,add_decision=d); self.quote(self.t+302,self.ref)
        self.assertEqual(self.run_state()['additions_done'],1)
        self.assertLessEqual(self.run_state()['modeled_stop_risk_usd'],run['budget'])

    def test_local_and_es_partials_serialize_without_quantity_double_count(self):
        run=self.enter(); start=self.t-1200
        es=[dict(time=t,open=60,high=61,low=59,close=60) for t in range(start,self.t+120,60)]
        es[-1]['high']=62
        self.quote(self.t+120,self.objective['price'],es_bars=es)
        self.assertEqual(self.run_state()['pending']['kind'],'LOCAL_PARTIAL')
        self.quote(self.t+121,self.objective['price'],es_bars=es)
        self.quote(self.t+122,self.objective['price'],es_bars=es)
        self.assertEqual(self.run_state()['pending']['kind'],'ES_PARTIAL')
        self.quote(self.t+123,self.objective['price'],es_bars=es)
        self.assertEqual(self.run_state()['quantity'],run['original_quantity']-2*(run['original_quantity']//5))

    def test_future_right_swing_confirmation_has_no_effect_and_closed_one_does(self):
        self.enter()
        bars=[dict(time=self.t+60+i*60,open=self.ref+2,high=self.ref+3,low=self.ref,close=self.ref+2) for i in range(3)]
        bars[1]['low']=self.ref-.5
        self.quote(self.t+239,self.ref+2,mnq_bars=bars)
        self.assertIsNone(self.run_state()['pending'])
        self.quote(self.t+240,self.ref+2,mnq_bars=bars)
        self.assertEqual(self.run_state()['pending']['stop_price'],self.ref-.75)

if __name__=='__main__':unittest.main()
