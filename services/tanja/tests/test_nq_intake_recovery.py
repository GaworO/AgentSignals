"""NQ feed regression checks; isolated SQLite, no AI or broker transport."""
import base64
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from store import Store


class NQIntakeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.end = int(time.time() // 60) * 60
        self.config = dict(DATA_DIR=self.tmp.name, TANJA_FEED_TOKEN='x' * 32,
                           TANJA_DASHBOARD_PASSWORD='test-password-long-enough')

    def make_app(self):
        # Only external AI/connection components are replaced; actual application
        # initialization, ticker bindings, HTTP validation and storage all run.
        modules = {
            'ai_review': types.SimpleNamespace(AIReview=lambda *args: None),
            'connection_test': types.SimpleNamespace(ConnectionTest=lambda *args: None),
        }
        with patch.dict(sys.modules, modules):
            return App(self.config, start_worker=False)

    def bar(self, symbol='NQ'):
        return dict(schema_version=1, feed_version='tanja-1m-v1', symbol=symbol,
                    ticker='CME_MINI:' + symbol + '1!', timeframe='1', session='regular',
                    standard=True, confirmed=True, bar_open_ms=(self.end-60)*1000,
                    bar_close_ms=self.end*1000, sent_at_ms=(self.end+1)*1000,
                    open=20000, high=20002, low=19999, close=20001, volume=10)

    def request(self, app, payload=None, path=None):
        raw = json.dumps(payload).encode() if payload is not None else b''
        env = dict(PATH_INFO=path or '/feed/' + self.config['TANJA_FEED_TOKEN'],
                   REQUEST_METHOD='POST' if payload is not None else 'GET',
                   CONTENT_LENGTH=str(len(raw)), **{'wsgi.input': io.BytesIO(raw)})
        env['HTTP_AUTHORIZATION'] = 'Basic ' + base64.b64encode(
            ('tanja:' + self.config['TANJA_DASHBOARD_PASSWORD']).encode()).decode()
        codes = []
        with patch('app.time.time', return_value=self.end+2):
            body = b''.join(app(env, lambda code, headers: codes.append(code)))
        return int(codes[0].split()[0]), body

    def test_existing_bindings_and_nq_history_survive_restart(self):
        old = Store(self.tmp.name)
        old.bind_tickers({'ES':'CME_MINI:ES1!', 'MNQ':'CME_MINI:MNQ1!'})
        with old.connect() as db:
            original = db.execute("SELECT value FROM metadata WHERE key='tickers'").fetchone()[0]
            db.execute("INSERT INTO metadata VALUES('collection_tickers', ?)",
                       (json.dumps({'NQ':'CME_MINI:NQ1!'}, sort_keys=True),))
            b = self.bar()
            db.execute('INSERT INTO collection_bars VALUES(?,?,?,?,?)',
                       ('NQ', self.end-60, self.end, self.end+1, json.dumps(b)))
        app = self.make_app()
        with app.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='tickers'").fetchone()[0], original)
        self.assertEqual(app.store.state(self.end+2)['feeds']['NQ']['count'], 1)
        self.assertEqual(self.request(app, self.bar())[0], 200)
        self.assertEqual(self.make_app().store.state(self.end+2)['feeds']['NQ']['count'], 1)

    def test_nq_http_acceptance_separate_storage_and_csv(self):
        app = self.make_app()
        status, body = self.request(app, self.bar())
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['queued'])
        with app.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_bars').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(len(app.store.history('NQ', self.end, self.end+2)), 1)
        status, csv = self.request(app, path='/api/bars/NQ.csv')
        self.assertEqual(status, 200)
        self.assertIn(b'CME_MINI:NQ1!', csv)

    def test_all_arrival_orders_preserve_one_es_mnq_job(self):
        for order in itertools.permutations(('ES', 'MNQ', 'NQ')):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                self.config['DATA_DIR'] = directory
                app = self.make_app()
                queued = 0
                for symbol in order:
                    status, body = self.request(app, self.bar(symbol))
                    self.assertEqual(status, 200)
                    queued += json.loads(body)['queued']
                self.assertEqual(queued, 1)
                with app.store.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
                    self.assertEqual({r[0] for r in db.execute('SELECT symbol FROM bars')}, {'ES','MNQ'})

    def test_duplicate_and_conflicting_nq_do_not_overwrite_history(self):
        app = self.make_app()
        self.assertEqual(self.request(app, self.bar())[0], 200)
        self.assertEqual(json.loads(self.request(app, self.bar())[1])['status'], 'duplicate')
        changed = self.bar(); changed['close'] = 20001.25
        self.assertEqual(self.request(app, changed)[0], 409)
        self.assertEqual(app.store.history('NQ', self.end, self.end+2)[0]['close'], 20001)

    def test_invalid_ticker_stale_and_synthetic_bars_still_rejected(self):
        app = self.make_app()
        for change in ({'ticker':'CME_MINI:NQ2!'}, {'symbol':'NQ1!'},
                       {'standard':False}, {'timeframe':'5'}, {'session':'us_regular'},
                       {'sent_at_ms':(self.end+100)*1000}, {'close':20001.1}):
            with self.subTest(change=change):
                self.assertEqual(self.request(app, dict(self.bar(), **change))[0], 422)
        stale = self.bar()
        for k in ('bar_open_ms','bar_close_ms','sent_at_ms'): stale[k] -= 300000
        self.assertEqual(self.request(app, stale)[0], 422)

    def test_rebinding_nq_to_different_series_is_blocked(self):
        self.make_app()
        self.config['TANJA_NQ_TICKER'] = 'CME_MINI:NQ2!'
        with self.assertRaisesRegex(ValueError, 'Ticker configuration differs'):
            self.make_app()


if __name__ == '__main__':
    unittest.main()
