import unittest
from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from daily_atr import Policy,calculate,expected_minutes,bounds,trade_date
from entry_controls import continuation_check

NY=ZoneInfo('America/New_York')


def history(cutoff,policy=None):
    policy=policy or Policy();day=trade_date(cutoff-1,policy);dates=[day];d=day
    while len(dates)<policy.period+2:
        d-=timedelta(days=1)
        if bounds(d,policy):dates.append(d)
    rows=[]
    for date in reversed(dates):
        for t in expected_minutes(date,policy,cutoff):
            rows.append(dict(time=t,received_at=t+60,open=110,high=120,low=100,close=110))
    return rows


class DailyATRTest(unittest.TestCase):
    def setUp(self):self.cutoff=int(datetime(2026,10,14,9,45,tzinfo=NY).timestamp())

    def test_five_completed_full_sessions_and_overnight_range(self):
        rows=history(self.cutoff);current=trade_date(self.cutoff-1,Policy());start=bounds(current,Policy())[0]
        rows=[dict(b,high=125) if b['time']==start else b for b in rows]
        r=calculate(rows,cutoff=self.cutoff,now=self.cutoff+1)
        self.assertTrue(r['complete'],r['missing_sessions']);self.assertEqual(r['atr'],20)
        self.assertEqual(len(r['completed_sessions']),5);self.assertEqual(r['range'],25)
        self.assertTrue(r['reached']);self.assertEqual(r['current_session']['start'],start)
        self.assertEqual(continuation_check(r,'continuation',self.cutoff+1)['state'],'BLOCKED')
        self.assertEqual(continuation_check(r,'reversal',self.cutoff+1)['state'],'CLEAR')

    def test_true_range_includes_gap_from_previous_close(self):
        rows=history(self.cutoff);d=trade_date(self.cutoff-1,Policy())-timedelta(days=1);start,end=bounds(d,Policy())
        rows=[dict(b,open=135,high=140,low=130,close=135) if start<=b['time']<end else b for b in rows]
        r=calculate(rows,cutoff=self.cutoff,now=self.cutoff)
        self.assertEqual(r['completed_sessions'][-1]['true_range'],30)
        self.assertEqual(r['atr'],22)

    def test_missing_history_and_late_receipt_do_not_fake_completeness(self):
        rows=history(self.cutoff)
        for modified in (rows[1:], [dict(b,received_at=self.cutoff+10) if i==100 else b for i,b in enumerate(rows)]):
            r=calculate(modified,cutoff=self.cutoff,now=self.cutoff+1)
            self.assertFalse(r['complete']);self.assertIsNone(r['atr']);self.assertTrue(r['missing_sessions'])

    def test_future_changes_do_not_affect_baseline_or_hash(self):
        rows=history(self.cutoff);r=calculate(rows,cutoff=self.cutoff,now=self.cutoff)
        future=dict(time=self.cutoff,open=-1,high=float('nan'),low=-1,close=-1)
        other=calculate(rows+[future],cutoff=self.cutoff,now=self.cutoff)
        self.assertEqual(other,r)
        self.assertEqual(calculate(list(reversed(rows)),cutoff=self.cutoff,now=self.cutoff),r)

    def test_weekend_and_dst_session_boundaries(self):
        p=Policy();sunday=int(datetime(2026,11,1,18,1,tzinfo=NY).timestamp())
        self.assertEqual(trade_date(sunday,p).isoformat(),'2026-11-02')
        start,end=bounds(trade_date(sunday,p),p)
        self.assertEqual(end-start,23*3600)
        saturday=int(datetime(2026,10,17,10,tzinfo=NY).timestamp())
        self.assertEqual(calculate([],cutoff=saturday,now=saturday)['state'],'OUTSIDE_ATR_SESSION')

    def test_sourced_early_close_and_closed_session_override(self):
        p=Policy(overrides={'2026-10-13':dict(close='13:00',source='synthetic schedule'),
            '2026-10-12':dict(closed=True,source='synthetic holiday')})
        rows=history(self.cutoff,p);r=calculate(rows,cutoff=self.cutoff,now=self.cutoff,policy=p)
        self.assertTrue(r['complete']);self.assertNotIn('2026-10-12',[s['trade_date'] for s in r['completed_sessions']])
        self.assertEqual(datetime.fromtimestamp(r['completed_sessions'][-1]['end'],NY).hour,13)

    def test_configurable_regular_session_is_explicit(self):
        p=Policy.from_config({'TANJA_DAILY_ATR_SESSION':'regular'})
        r=calculate(history(self.cutoff,p),cutoff=self.cutoff,now=self.cutoff,policy=p)
        self.assertTrue(r['complete']);self.assertEqual(r['session'],'regular')
        self.assertEqual(datetime.fromtimestamp(r['current_session']['start'],NY).hour,9)


if __name__=='__main__':unittest.main()
