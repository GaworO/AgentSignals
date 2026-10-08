"""Isolated, durable closed-bar intake. No broker or execution dependencies."""
import csv
import io
import json
import math
import sqlite3
import time
from pathlib import Path

FIELDS = {'schema_version', 'feed_version', 'symbol', 'ticker', 'timeframe',
          'session', 'standard', 'confirmed', 'bar_open_ms', 'bar_close_ms',
          'sent_at_ms', 'open', 'high', 'low', 'close', 'volume'}


class InvalidBar(ValueError):
    pass


class Conflict(ValueError):
    pass


def validate_bar(b, tickers, now):
    if not isinstance(b, dict) or set(b) != FIELDS:
        raise InvalidBar('Payload fields do not match feed v1')
    if type(b['schema_version']) is not int or b['schema_version'] != 1 or b['feed_version'] != 'tanja-1m-v1':
        raise InvalidBar('Unsupported feed version')
    if b['symbol'] not in tickers or b['ticker'] != tickers[b['symbol']]:
        raise InvalidBar('Symbol/ticker not configured for this service')
    if b['timeframe'] != '1' or b['confirmed'] is not True or b['standard'] is not True:
        raise InvalidBar('Only confirmed standard one-minute bars accepted')
    # CME futures call their default ETH session 'regular'; RTH is 'us_regular'.
    if b['session'] not in ('regular', 'extended'):
        raise InvalidBar('Enable electronic/extended trading hours (ETH)')
    for k in ('bar_open_ms', 'bar_close_ms', 'sent_at_ms'):
        if type(b[k]) is not int:
            raise InvalidBar('Timestamps must be integer UTC milliseconds')
    start, end, sent = (b[k] / 1000 for k in ('bar_open_ms', 'bar_close_ms', 'sent_at_ms'))
    if start <= 0 or b['bar_open_ms'] % 60000 or end - start != 60:
        raise InvalidBar('Incorrect one-minute boundaries')
    if end > now or sent < end or sent > now + 5 or now - end > 180:
        raise InvalidBar('Future, unclosed or stale bar; live intake allows 180 seconds')
    for k in ('open', 'high', 'low', 'close', 'volume'):
        if type(b[k]) not in (int, float) or not math.isfinite(b[k]):
            raise InvalidBar('OHLCV must be finite numbers')
        if b[k] < 0 or (k != 'volume' and b[k] == 0):
            raise InvalidBar('Invalid price or volume')
        if k != 'volume' and abs(b[k] * 4 - round(b[k] * 4)) > 1e-6:
            raise InvalidBar('ES/MNQ prices must be on a 0.25 tick')
    if b['low'] > min(b['open'], b['close']) or b['high'] < max(b['open'], b['close']) or b['low'] > b['high']:
        raise InvalidBar('Impossible OHLC candle')
    return b


class Store:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(directory) / 'tanja.sqlite3')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
              CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT, start INTEGER, end INTEGER, received REAL, payload TEXT,
                PRIMARY KEY(symbol,start));
              CREATE TABLE IF NOT EXISTS jobs (
                cutoff INTEGER PRIMARY KEY, frozen_at REAL, status TEXT, error TEXT,
                packet TEXT, processed_at REAL);
              CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY, cutoff INTEGER, payload TEXT);
              CREATE TABLE IF NOT EXISTS diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, message TEXT);
            ''')

    def bind_tickers(self, tickers):
        value = json.dumps(tickers, sort_keys=True)
        with self.connect() as db:
            existing = db.execute("SELECT value FROM metadata WHERE key='tickers'").fetchone()
            if existing and existing[0] != value:
                raise ValueError('Ticker configuration differs from stored series. Use a new volume for a new series.')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('tickers',?)", (value,))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=1.0)
        db.row_factory = sqlite3.Row
        return db

    def note(self, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO diagnostics(at,kind,message) VALUES(?,?,?)', (time.time(), kind, message))
            db.execute('DELETE FROM diagnostics WHERE id < (SELECT COALESCE(MAX(id),0)-500 FROM diagnostics)')

    def ingest(self, b, received):
        start, end = b['bar_open_ms'] // 1000, b['bar_close_ms'] // 1000
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload FROM bars WHERE symbol=? AND start=?', (b['symbol'], start)).fetchone()
            if prior:
                old = json.loads(prior[0])
                # Transport send time may change on retries; candle content may not.
                if {k:v for k,v in old.items() if k != 'sent_at_ms'} != {k:v for k,v in b.items() if k != 'sent_at_ms'}:
                    raise Conflict('Conflicting duplicate; existing candle retained')
                return {'status': 'duplicate', 'queued': False}
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)', (b['symbol'], start, end, received, json.dumps(b)))
            latest = db.execute('SELECT MAX(cutoff) FROM jobs').fetchone()[0] or 0
            paired = db.execute('SELECT COUNT(*) FROM bars WHERE start=?', (start,)).fetchone()[0] == 2
            queued = paired and end > latest
            if queued:
                db.execute('INSERT INTO jobs(cutoff,frozen_at,status) VALUES(?,?,?)', (end, received, 'queued'))
            return {'status': 'stored', 'queued': queued, 'paired': paired,
                    'late_for_analysis': end <= latest}

    def claim_job(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            r = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY cutoff LIMIT 1").fetchone()
            if r:
                db.execute("UPDATE jobs SET status='processing' WHERE cutoff=?", (r['cutoff'],))
                return dict(r)

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='processing'")

    def history(self, symbol, cutoff, frozen_at):
        with self.connect() as db:
            # Arrival cutoff is independent from the market cutoff. Late arrivals
            # can never rewrite an earlier decision's input snapshot.
            rows = db.execute('SELECT payload FROM bars WHERE symbol=? AND end<=? AND received<=? ORDER BY start DESC LIMIT 12000',
                              (symbol, cutoff, frozen_at)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def finish(self, job, packet, candidates, now):
        with self.connect() as db:
            for c in candidates:
                db.execute('INSERT OR IGNORE INTO candidates VALUES(?,?,?)', (c['id'], job['cutoff'], json.dumps(c)))
            db.execute("UPDATE jobs SET status='done',packet=?,processed_at=? WHERE cutoff=?",
                       (json.dumps(packet, separators=(',', ':')), now, job['cutoff']))
            # Keep the most recent 500 full snapshots; keep job audit + candidates.
            db.execute('UPDATE jobs SET packet=NULL WHERE cutoff < (SELECT cutoff FROM jobs ORDER BY cutoff DESC LIMIT 1 OFFSET 499)')

    def fail(self, cutoff, kind):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE cutoff=?", (kind, cutoff))

    def state(self, now):
        with self.connect() as db:
            feeds = {}
            for symbol in ('ES', 'MNQ'):
                n = db.execute('SELECT COUNT(*) FROM bars WHERE symbol=?', (symbol,)).fetchone()[0]
                last = db.execute('SELECT * FROM bars WHERE symbol=? ORDER BY start DESC LIMIT 1', (symbol,)).fetchone()
                feeds[symbol] = dict(count=n, latest_close=last['end'] if last else None,
                                     age_seconds=round(now-last['end'], 1) if last else None,
                                     state='NO_DATA' if not last else 'CURRENT' if now-last['end'] <= 120 else 'STALE_OR_MARKET_CLOSED')
            jobs = [dict(r) for r in db.execute('SELECT cutoff,frozen_at,status,error,processed_at FROM jobs ORDER BY cutoff DESC LIMIT 60')]
            candidates = [json.loads(r[0]) for r in db.execute('SELECT payload FROM candidates ORDER BY cutoff DESC,id LIMIT 100')]
            diag = [dict(r) for r in db.execute('SELECT at,kind,message FROM diagnostics ORDER BY id DESC LIMIT 20')]
            latest = db.execute('SELECT packet FROM jobs WHERE packet IS NOT NULL ORDER BY cutoff DESC LIMIT 1').fetchone()
        p = json.loads(latest[0]) if latest else None
        return dict(feeds=feeds, jobs=jobs, candidates=candidates, diagnostics=diag,
                    latest_context=p, executions=[], execution_status='NOT_CONNECTED', ai_status='NOT_CONNECTED',
                    mode='OBSERVE_ONLY', account='My Funded Futures · 50K Builder',
                    account_verified=False, broker_connected=False, executable=False)

    def export_csv(self, symbol):
        with self.connect() as db:
            rows = db.execute('SELECT payload,received FROM bars WHERE symbol=? ORDER BY start', (symbol,)).fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['symbol','ticker','bar_open_ms','bar_close_ms','open','high','low','close','volume','received_at_utc_seconds'])
        for row in rows:
            b = json.loads(row['payload'])
            w.writerow([b[k] for k in ('symbol','ticker','bar_open_ms','bar_close_ms','open','high','low','close','volume')] + [row['received']])
        return out.getvalue()
