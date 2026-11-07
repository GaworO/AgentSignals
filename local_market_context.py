"""The existing Tanja closed-candle context engine hosted inside Agent50k.

Only intake, deterministic market features and a bounded journal are started.
No Tanja AI, candidate selector, transcript, broker capture or trading loop.
Selection and execution remain owned by the existing Agent50k modules.
"""
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path

from local_context_engine.store import Store, validate_bar, InvalidBar, Conflict, bar_table
from local_context_engine.context_v3 import build_v3_packet
from local_context_engine.tanya_replay import Bar
from local_context_engine.daily_atr import calculate, attach, Policy
from local_context_engine.atr_review_context import attach as attach_atr
from local_context_engine.review_calendar import review_day

VERSION = 'agent50k-local-market-context-v1'
INSTANCE = None


def build_context(histories, cutoff, frozen_at, processed_at, policy):
    """Same evidence as deployed Tanja; omit its unused setup_scanner output."""
    markets = {s: [Bar(b['bar_open_ms']//1000, b['open'], b['high'], b['low'], b['close'])
                   for b in rows] for s, rows in histories.items()}
    packet = build_v3_packet(markets, cutoff, max_bars=64)
    attach(packet, calculate(histories['MNQ'], cutoff=cutoff, now=frozen_at, policy=policy))
    attach_atr(packet, histories, frozen_at)
    readiness = {s: packet['coverage'][s]['retained_bars_per_timeframe'] for s in markets}
    return dict(packet=packet, frozen_at=frozen_at, processed_at=processed_at,
                coverage=readiness, processing_delay_seconds=round(processed_at-cutoff, 1))


class ContextStore(Store):
    """Bounded operational snapshots; exact paid review inputs remain in AI journals."""
    def finish(self, job, context, candidates, now):
        with self.connect() as db:
            db.execute('UPDATE nq_jobs SET status=\'done\',packet=?,processed_at=? WHERE cutoff=?',
                       (json.dumps(context, separators=(',', ':')), now, job['cutoff']))
            db.execute('UPDATE nq_jobs SET packet=NULL WHERE cutoff < '
                       '(SELECT cutoff FROM nq_jobs WHERE packet IS NOT NULL ORDER BY cutoff DESC LIMIT 1 OFFSET 15)')
            db.execute("DELETE FROM nq_jobs WHERE cutoff<? AND status IN ('done','failed') AND packet IS NULL",
                       (job['cutoff']-14*86400,))
            # Raw closed candles are not deleted here. Old full review inputs
            # live in the paid-review journals; snapshot retention is separate.



class LocalContext:
    def __init__(self, config):
        self.config=dict(config)
        self.root=Path(config.get('DATA_DIR','/data/builder50'))/'market_context'
        self.root.mkdir(parents=True, exist_ok=True)
        self.store=ContextStore(self.root, analysis_symbol='NQ')
        self.tickers={s:config.get('MARKET_CONTEXT_'+s+'_TICKER','CME_MINI:'+s+'1!') for s in ('ES','NQ','MNQ')}
        self.store.bind_tickers(self.tickers)
        self.policy=Policy.from_config(config)
        self.token=config.get('MARKET_CONTEXT_FEED_TOKEN','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.token):
            raise ValueError('market_context_feed_token_required')
        self.wake=threading.Event()
        self.thread=None
        self.last_error=None
        self.worker_lock=None

    def process_one(self):
        job=self.store.claim_job()
        if not job:return False
        try:
            histories={s:self.store.history(s,job['cutoff'],job['frozen_at']) for s in ('ES','NQ','MNQ')}
            context=build_context(histories,job['cutoff'],job['frozen_at'],time.time(),self.policy)
            self.store.finish(job,context,[],time.time())
            self.last_error=None
        except Exception as exc:
            self.last_error=type(exc).__name__
            self.store.fail(job['cutoff'],self.last_error)
        return True

    def run(self):
        # One writer for the shared volume, including multi-worker deployments.
        lock=(self.root/'worker.lock').open('a+')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close();return
        self.worker_lock=lock
        self.store.recover()
        while True:
            self.wake.clear()
            try:
                if self.process_one():continue
            except Exception as exc:self.last_error=type(exc).__name__
            self.wake.wait(30)

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,name='local-market-context',daemon=True)
        self.thread.start()

    def source(self, now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            row=db.execute("SELECT packet FROM nq_jobs WHERE status='done' AND packet IS NOT NULL ORDER BY cutoff DESC LIMIT 1").fetchone()
        return dict(context=json.loads(row[0]) if row else None,ai_status='DISABLED',calls_today=0,
                    calendar=review_day(now),source=VERSION)

    def state(self):
        with self.store.connect() as db:
            feeds={s:dict(zip(('bars','latest_close','last_received'),db.execute(
                f'SELECT count(*),max(end),max(received) FROM {bar_table(s)} WHERE symbol=?',(s,)).fetchone())) for s in ('ES','NQ','MNQ')}
            jobs=dict(db.execute('SELECT status,count(*) FROM nq_jobs GROUP BY status'))
            packet=db.execute("SELECT cutoff,processed_at FROM nq_jobs WHERE status='done' AND packet IS NOT NULL ORDER BY cutoff DESC LIMIT 1").fetchone()
        return dict(version=VERSION,feeds=feeds,jobs=jobs,latest_context=dict(packet) if packet else None,
                    worker_alive=bool(self.thread and self.thread.is_alive()),last_error=self.last_error,
                    ai_worker=False,broker_worker=False,context_snapshots_retained=16,raw_history_deleted=False)


def source(config):
    if INSTANCE is None:raise ValueError('local_market_context_not_started')
    return INSTANCE.source()


def register(app, config=None, *, start=True):
    global INSTANCE
    config=os.environ if config is None else config
    if config.get('MARKET_CONTEXT_ENABLED','false')!='true':return None
    instance=LocalContext(config)
    INSTANCE=instance
    from flask import request, jsonify

    @app.post('/feed/<token>')
    def local_context_feed(token):
        if not hmac.compare_digest(token, instance.token):return jsonify(error='not_found'),404
        if request.content_length is None or not 1<=request.content_length<=12000:return jsonify(error='invalid_payload_size'),413
        try:
            now=time.time();body=validate_bar(request.get_json(silent=True),instance.tickers,now)
            result=instance.store.ingest(body,now)
            if result.get('queued'):instance.wake.set()
            return jsonify(ok=True,**result)
        except Conflict:return jsonify(error='conflicting_duplicate'),409
        except (InvalidBar,ValueError,TypeError):return jsonify(error='invalid_closed_candle'),422

    @app.post('/reference/<token>')
    def local_dated_reference(token):
        # Retain the existing dated-contract alert as observations only.
        # No reference monitor, broker lookups or history substitution worker.
        if not hmac.compare_digest(token, instance.token):return jsonify(error='not_found'),404
        if request.content_length is None or not 1<=request.content_length<=12000:return jsonify(error='invalid_payload_size'),413
        try:
            contracts=json.loads(config.get('MARKET_CONTEXT_REFERENCE_CONTRACTS','{}'))
            if set(contracts)!={'ES','NQ','MNQ'} or not all(re.fullmatch(s+r'[HMUZ]20\d{2}',str(v)) for s,v in contracts.items()):
                return jsonify(error='dated_reference_not_configured'),503
            now=time.time();event=request.get_json(silent=True)
            if not isinstance(event,dict) or set(event)!={'version','bars'} or event['version']!='tanja-dated-reference-v1' or not isinstance(event['bars'],list) or len(event['bars'])!=3:
                raise ValueError()
            tickers={s:'CME_MINI:'+c for s,c in contracts.items()}
            bars=[validate_bar(b,tickers,now) for b in event['bars']]
            if {b['symbol'] for b in bars}!={'ES','NQ','MNQ'} or len({b['bar_open_ms'] for b in bars})!=1:raise ValueError()
            binding=json.dumps(contracts,sort_keys=True)
            with instance.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS reference_observations(binding TEXT,symbol TEXT,start INTEGER,received REAL,payload TEXT,PRIMARY KEY(binding,symbol,start))')
                db.execute('BEGIN IMMEDIATE')
                for b in bars:
                    key=(binding,b['symbol'],b['bar_open_ms']//1000)
                    old=db.execute('SELECT payload FROM reference_observations WHERE binding=? AND symbol=? AND start=?',key).fetchone()
                    if old and {k:v for k,v in json.loads(old[0]).items() if k!='sent_at_ms'}!={k:v for k,v in b.items() if k!='sent_at_ms'}:raise Conflict()
                    db.execute('INSERT OR IGNORE INTO reference_observations VALUES(?,?,?,?,?)',(*key,now,json.dumps(b,separators=(',',':'))))
            return jsonify(ok=True,status='reference_recorded',is_order=False,used_for_execution=False)
        except Conflict:return jsonify(error='conflicting_duplicate'),409
        except (InvalidBar,ValueError,TypeError,KeyError):return jsonify(error='invalid_dated_reference'),422

    @app.get('/market-data/state')
    def local_context_state():
        password=config.get('AB_CONTEXT_SOURCE_PASSWORD','')
        expected='Basic '+base64.b64encode(('tanja:'+password).encode()).decode()
        if not password or not hmac.compare_digest(request.headers.get('Authorization',''),expected):return jsonify(error='unauthorized'),401
        return jsonify(instance.state())

    if start:instance.start()
    return instance
