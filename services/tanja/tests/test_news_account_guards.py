import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from scheduled_news import NewsCalendar,normalize,assess,apply_news,propose_stop_with_calendar
from account_guard import assess as account,apply_account
from manual_account import save_snapshot
from data_health import account_snapshot
from store import Store


def ts(hour,minute=0):
    return int(datetime(2026,10,9,hour,minute,tzinfo=ZoneInfo('America/New_York')).timestamp())

def raw():
    return [dict(title='Synthetic high impact release',country='USD',impact='High',date='2026-10-09T10:00:00-04:00')]

def plan():
    return dict(state='PLAN_READY',ai_context={'source_cutoff':ts(9,45)},compiled={'plan':{'context_valid_until':ts(10,30)}},selected={'selection':{}},orders_enabled=False)

class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.calendar=NewsCalendar(self.store)

    def test_window_boundaries_and_fresh_context(self):
        s=normalize(raw(),ts(9))
        self.assertEqual(assess(s,ts(9,54))['state'],'CLEAR')
        for now in (ts(9,55),ts(9,59),ts(10),ts(10,4)):
            r=apply_news(plan(),assess(s,now))
            self.assertEqual(r['state'],'NEWS_BLOCKED');self.assertIsNone(r['compiled']['plan'])
        news=assess(s,ts(10,5));self.assertEqual(news['state'],'CLEAR')
        r=apply_news(plan(),news);self.assertIn('NEWS_REQUIRES_FRESH_AI_CONTEXT',r['reasons'])
        p=plan();p['ai_context']['source_cutoff']=ts(10,5)
        self.assertEqual(apply_news(p,news)['state'],'PLAN_READY')
        p=apply_news(plan(),assess(s,ts(9,54)))
        self.assertEqual(p['compiled']['plan']['context_valid_until'],ts(9,55))

    def test_bad_missing_stale_and_uncertain_calendar(self):
        for bad in ([],[dict(raw()[0],date='2026-10-09T10:00:00')],[dict(raw()[0],date='2026-10-16T10:00:00-04:00')],[dict(raw()[0],impact='Unknown')]):
            with self.assertRaises((ValueError,KeyError)):normalize(bad,ts(9))
        self.assertEqual(assess(None,ts(9))['state'],'BLOCKED')
        s=normalize(raw(),ts(9))
        for now in (ts(8),ts(12)):
            self.assertEqual(assess(s,now)['reasons'],['NEWS_CALENDAR_STALE_OR_FUTURE'])
        s=normalize([dict(raw()[0],title='Tentative speech')],ts(9))
        self.assertEqual(assess(s,ts(9))['reasons'],['NEWS_TIME_UNCERTAIN'])
        s=normalize([dict(raw()[0],country='EUR')],ts(9))
        self.assertEqual(s['events'],[])

    def test_refresh_restart_failure_and_causal_receipt(self):
        self.calendar.refresh(fetch=raw,clock=lambda:ts(8))
        restarted=NewsCalendar(self.store)
        self.assertFalse(restarted.refresh(fetch=lambda:self.fail('Fetched too early'),clock=lambda:ts(8,30)))
        self.assertEqual(restarted.state(ts(7))['reasons'],['NEWS_CALENDAR_MISSING'])
        restarted.refresh(fetch=raw,clock=lambda:ts(9))
        e=restarted.state(ts(9))['next_event'];self.assertEqual(e['known_at'],ts(8))
        def fail():raise OSError('sensitive diagnostic must not be published')
        restarted.refresh(fetch=fail,clock=lambda:ts(10))
        self.assertEqual(restarted.state(ts(10))['reasons'],['NEWS_FETCH_FAILED'])
        with self.store.connect() as db:
            self.assertNotIn('sensitive',db.execute('SELECT error FROM news_snapshots ORDER BY id DESC LIMIT 1').fetchone()[0])
        changed=lambda:[dict(raw()[0],date='2026-10-09T12:00:00-04:00')]
        restarted.refresh(fetch=changed,clock=lambda:ts(11))
        self.assertEqual(restarted.state(ts(11))['next_event']['known_at'],ts(11))

    def test_calendar_drives_existing_stop_proposal_only(self):
        self.calendar.refresh(fetch=raw,clock=lambda:ts(9))
        for direction,stop,market,expected in [('long',29950,30020,29998.5),('short',30050,29980,30001.5)]:
            r=propose_stop_with_calendar(self.calendar,now=ts(9,59),bars=[],direction=direction,entry=30000,current_stop=stop,opened_at=ts(9,45),market_price=market,price_known_at=ts(9,59))
            self.assertEqual(r['stop_price'],expected);self.assertFalse(r['is_order']);self.assertFalse(r['news_protection_verified'])
        r=propose_stop_with_calendar(self.calendar,now=ts(9,59),bars=[],direction='long',entry=30000,current_stop=30005,opened_at=ts(9,45),market_price=30020,price_known_at=ts(9,59))
        self.assertEqual(r['action'],'NONE')

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.body=dict(balance=48962.2,minimum_balance=48161.9,target_balance=53000,daily_loss_remaining=1000,committed_risk_usd=0,committed_micro_contracts=0)
    def saved(self,**updates):
        return save_snapshot(self.tmp.name,dict(self.body,**updates),ts(9))
    def test_budget_and_exposure_caps(self):
        s=self.saved();g=account(s,ts(10));self.assertEqual(g['budget_usd'],244.81);self.assertEqual(g['max_contracts'],40)
        self.assertFalse(g['broker_verified'])
        g=account(self.saved(committed_risk_usd=100,committed_micro_contracts=5),ts(10))
        self.assertEqual(g['budget_usd'],144.81);self.assertEqual(g['max_contracts'],35)
        g=account(self.saved(daily_loss_remaining=50),ts(10));self.assertEqual(g['budget_usd'],49.99)
        g=account(self.saved(minimum_balance=48920),ts(10));self.assertEqual(g['budget_usd'],42.19)
    def test_block_missing_stale_exhausted_and_target(self):
        for updates in ({'daily_loss_remaining':0},{'committed_risk_usd':250},{'committed_micro_contracts':40},{'balance':48161.9},{'balance':53000}):
            g=account(self.saved(**updates),ts(10));self.assertEqual(g['state'],'BLOCKED')
            r=apply_account(plan(),g);self.assertIsNone(r['compiled']['plan'])
        s=self.saved();self.assertEqual(account(s,ts(10)+86400)['state'],'BLOCKED')
        self.assertEqual(account(s,ts(8))['state'],'BLOCKED')
        del s['daily_loss_remaining'];self.assertEqual(account(s,ts(10))['reasons'],['ACCOUNT_RISK_FIELDS_REQUIRED'])
    def test_validation_roundtrip_and_legacy_does_not_authorize(self):
        self.saved();s=account_snapshot(Path(self.tmp.name));self.assertEqual(s['daily_loss_remaining'],1000)
        for updates in ({'daily_loss_remaining':1001},{'committed_micro_contracts':1.5},{'committed_risk_usd':-1},{'committed_risk_usd':float('nan')},{'daily_loss_remaining':0.001}):
            with self.assertRaises(ValueError):self.saved(**updates)
        old={k:self.body[k] for k in ('balance','minimum_balance','target_balance')}
        s=save_snapshot(self.tmp.name,old,ts(9));self.assertEqual(account(s,ts(10))['state'],'BLOCKED')

if __name__=='__main__':unittest.main()
