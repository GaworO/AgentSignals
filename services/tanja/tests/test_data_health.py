import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from data_health import data_health

class TestDataHealth(unittest.TestCase):
    def state(self, date='2026-10-09T10:00:00'):
        now=datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
        return now,dict(feeds={s:dict(age_seconds=15,state='CURRENT') for s in ('ES','NQ','MNQ')},jobs=[dict(cutoff=now-15,status='done')],diagnostics=[])
    def test_fresh(self):
        now,s=self.state();self.assertEqual(data_health(s,now)['level'],'ok')
    def test_one_feed_stops(self):
        now,s=self.state();s['feeds']['NQ']=dict(age_seconds=180,state='STALE_OR_MARKET_CLOSED')
        h=data_health(s,now);self.assertEqual(h['level'],'error');self.assertIn('NQ:',h['messages'][0])
    def test_missing_feed(self):
        now,s=self.state();s['feeds'].pop('ES');self.assertEqual(data_health(s,now)['level'],'error')
    def test_pause_and_reopen(self):
        for date,level in [('2026-10-09T17:10:00','info'),('2026-10-10T12:00:00','info'),('2026-10-11T18:05:00','error'),('2026-10-12T17:30:00','info')]:
            now,s=self.state(date);s['feeds']={k:dict(age_seconds=400,state='STALE_OR_MARKET_CLOSED') for k in s['feeds']}
            self.assertEqual(data_health(s,now)['level'],level)
    def test_processing_failure(self):
        now,s=self.state();s['jobs'][0]['status']='failed';self.assertEqual(data_health(s,now)['level'],'error')
    def test_diagnostics_expire(self):
        now,s=self.state();s['diagnostics']=[dict(at=now-10,kind='rejected',message='Wrong ticker')]
        self.assertEqual(data_health(s,now)['level'],'warning')
        s['diagnostics'][0]['at']=now-301;self.assertEqual(data_health(s,now)['level'],'ok')
    def test_worker_delay(self):
        now,s=self.state();s['jobs'][0]['cutoff']=now-240;self.assertEqual(data_health(s,now)['level'],'warning')
