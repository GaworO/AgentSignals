import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store
from history_import import import_history
from manual_ai_review import ManualObservationReview
from unittest.mock import patch


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);self.store.bind_tickers({'ES':'CME_MINI:ES1!','MNQ':'CME_MINI:MNQ1!'})
        self.path=Path(self.temp.name)/'history.csv';self.start=1791400000//60*60;self.now=self.start+6000
        self.write()
        self.existing=dict(symbol='MNQ',open=100.,high=102.,low=99.,close=101.,volume=10.,marker='live')
        with self.store.connect() as db:
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('MNQ',self.start+60,self.start+120,self.start+121,json.dumps(self.existing)))
            db.execute("INSERT INTO jobs(cutoff,frozen_at,status,packet) VALUES(?,?,'done','original')",(self.start+120,self.start+121))

    def write(self, conflict=False, invalid=False):
        with self.path.open('w') as f:
            w=csv.writer(f);w.writerow(['ts_event','open','high','low','close','volume'])
            for i in [2,0,1,1]:
                w.writerow([datetime.fromtimestamp(self.start+i*60,timezone.utc).isoformat(),100,102,99,
                            103 if invalid else 100 if conflict and i==1 else 101,10])

    def test_apply_freezes_preserves_live_and_never_queues(self):
        before=self.store.history('MNQ',self.now,self.now-1)
        result=import_history(self.store,self.path,apply=True,now=self.now,min_overlap=1)
        self.assertEqual(result['inserted'],2);self.assertEqual(result['identical_duplicates'],1)
        self.assertEqual(self.store.history('MNQ',self.now,self.now-1),before)
        self.assertEqual(len(self.store.history('MNQ',self.now,self.now)),3)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT packet FROM jobs').fetchone()[0],'original')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_bars').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT original_csv FROM history_imports').fetchone()[0],self.path.read_bytes())
        self.assertEqual(import_history(self.store,self.path,apply=True,now=self.now+1,min_overlap=1)['mode'],'already_imported')
        self.assertEqual(len(self.store.history('MNQ',self.now,self.now+1)),3)

    def test_conflict_is_atomic(self):
        self.write(conflict=True)
        with self.assertRaises(ValueError):import_history(self.store,self.path,apply=True,now=self.now,min_overlap=1)
        self.assertEqual(len(self.store.history('MNQ',self.now,self.now)),1)

    def test_preview_no_mutation(self):
        self.assertEqual(import_history(self.store,self.path,now=self.now,min_overlap=1)['would_insert'],2)
        self.assertEqual(len(self.store.history('MNQ',self.now,self.now)),1)

    def test_rejects_recent_invalid_and_insufficient_overlap(self):
        with self.assertRaises(ValueError):import_history(self.store,self.path,now=self.start+200,min_overlap=1)
        with self.assertRaises(ValueError):import_history(self.store,self.path,now=self.now)
        self.write(invalid=True)
        with self.assertRaises(ValueError):import_history(self.store,self.path,now=self.now,min_overlap=1)

    def test_manual_only_waives_schedule(self):
        obj=object.__new__(ManualObservationReview);obj.make_payload=lambda p: {'test':True}
        for status in ['WAITING_FOR_HISTORY','WAITING_FOR_FRESH_DATA','WAITING_FOR_CONTIGUOUS_DATA','MISSING_API_KEY','DISABLED']:
            with patch('ai_review.AIReview.gate',return_value=status):self.assertEqual(obj.gate(0,{'packet':{}}),status)
        with patch('ai_review.AIReview.gate',return_value='OUTSIDE_REVIEW_WINDOW'):
            self.assertEqual(obj.gate(0,{'packet':{}}),'READY')
            obj.make_payload=lambda p:'x'*500001
            self.assertEqual(obj.gate(0,{'packet':{}}),'INPUT_TOO_LARGE')


if __name__=='__main__':unittest.main()
