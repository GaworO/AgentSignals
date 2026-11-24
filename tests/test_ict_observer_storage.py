"""Live storage pressure, bounded startup and restart evidence; no provider/broker."""
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from services.tanja.ict_agent import storage_guard
from services.tanja.ict_agent.bridge import observe_once
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.runtime import Worker
from services.tanja.ict_agent.storage_guard import WriteGuard, WritePaused, MIB
from services.tanja.ict_agent.store import Store
from services.tanja.ict_agent.tests.test_observer import FakeKnowledge, bar

NOW = 1795521600


def intake(path, starts):
    with sqlite3.connect(path) as db:
        for table in ('bars', 'collection_bars'):
            db.execute(f'CREATE TABLE IF NOT EXISTS {table}(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
        for start in starts:
            for symbol in ('ES', 'NQ', 'MNQ'):
                b = bar(start, symbol)
                p = dict(symbol=symbol, ticker=b['contract'], confirmed=True, standard=True,
                         bar_open_ms=start*1000, bar_close_ms=(start+60)*1000,
                         **{k: b[k] for k in ('open', 'high', 'low', 'close')})
                table = 'collection_bars' if symbol == 'NQ' else 'bars'
                db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',
                           (symbol, start, start+60, start+61, json.dumps(p)))


def disk(monkeypatch, mb):
    monkeypatch.setattr(storage_guard.shutil, 'disk_usage', lambda p: SimpleNamespace(free=mb*MIB))


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_full_history_is_not_replayed_and_restart_is_deduplicated(tmp_path):
    source = tmp_path/'source.db'
    intake(source, range(NOW-86400, NOW, 60))
    original = source.read_bytes()
    store = Store(tmp_path/'observer.db', {'test': True})
    observer = Observer(store, FakeKnowledge())
    result = observe_once(observer, source, NOW+2)
    assert result['processed'] == 5
    assert result['history_not_replayed_before'] == NOW-300
    with store.readonly() as db:
        assert db.execute('SELECT min(ts),max(ts),count(*) FROM bars').fetchone() == (NOW-300, NOW-60, 15)
    previous = store.snapshot()
    assert previous['context']['NQ']['latest']['ts'] == NOW-60
    assert observe_once(Observer(store, FakeKnowledge()), source, NOW+2)['processed'] == 0
    assert store.snapshot() == previous
    assert source.read_bytes() == original


def test_backlog_and_one_poll_are_bounded_and_gap_is_explicit(tmp_path):
    source = tmp_path/'source.db'
    intake(source, range(NOW-3600, NOW, 60))
    store = Store(tmp_path/'observer.db', {})
    observer = Observer(store, FakeKnowledge())
    observer.push([bar(NOW-7200, sym) for sym in ('NQ', 'ES', 'MNQ')])
    result = observe_once(observer, source, NOW+2, max_age_seconds=300, max_batches=2)
    assert result['processed'] == 2
    assert result['previous_latest_start'] == NOW-7200
    with store.readonly() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='DATA_GAP'").fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM bars').fetchone()[0] == 9


def test_low_space_blocks_actual_scanner_transaction_and_status_writes(tmp_path, monkeypatch):
    disk(monkeypatch, 800)
    cfg = dict(DATA_DIR=str(tmp_path), ICT_AI_MODE='OBSERVE')
    guard = WriteGuard(cfg)
    store = Store(tmp_path/'ict_ai/observe.db', {}, write_guard=guard)
    observer = Observer(store, FakeKnowledge())
    source = tmp_path/'source.db'; intake(source, [NOW-120])
    observer.push([bar(NOW-180, s) for s in ('NQ', 'ES', 'MNQ')])
    worker = Worker(cfg)
    # Settle setup-only SQLite connections before simulating pressure. A later
    # cyclic GC checkpoint is not a write attempted by the guarded operation.
    import gc
    gc.collect()
    disk(monkeypatch, 100)
    before = hashes(tmp_path)
    with pytest.raises(WritePaused, match='LOW_FREE_SPACE'):
        observe_once(observer, source, NOW)
    worker.status(state='OBSERVING')
    assert worker.current_status['state'] == 'PAUSED_WRITES'
    assert worker.current_status['storage']['free_bytes'] == 100*MIB
    assert hashes(tmp_path) == before
    with store.readonly() as db:
        assert db.execute('SELECT count(*) FROM batches').fetchone()[0] == 1
    disk(monkeypatch, 800)
    assert observe_once(observer, source, NOW)['processed'] == 1


def test_pressure_between_batches_stops_next_write(tmp_path, monkeypatch):
    disk(monkeypatch, 800)
    source = tmp_path/'source.db'; intake(source, [NOW-180, NOW-120])
    guard = WriteGuard(dict(DATA_DIR=str(tmp_path), ICT_AI_MODE='OBSERVE'))
    store = Store(tmp_path/'observer.db', {}, write_guard=guard)
    observer = Observer(store, FakeKnowledge())
    push = observer.push
    def consume(*args, **kwargs):
        result = push(*args, **kwargs)
        disk(monkeypatch, 100)
        return result
    monkeypatch.setattr(observer, 'push', consume)
    with pytest.raises(WritePaused):observe_once(observer, source, NOW)
    with store.readonly() as db:
        assert db.execute('SELECT count(*) FROM batches').fetchone()[0] == 1


def test_low_space_at_start_creates_no_files_and_thread_survives(tmp_path, monkeypatch):
    disk(monkeypatch, 0)
    worker = Worker(dict(DATA_DIR=str(tmp_path), ICT_AI_MODE='OBSERVE'))
    before = hashes(tmp_path)
    worker.start()
    try:
        deadline = time.monotonic()+3
        while worker.current_status is None and time.monotonic()<deadline:time.sleep(.01)
        assert worker.thread.is_alive()
        assert worker.current_status['storage']['reason'] == 'LOW_FREE_SPACE'
        assert not (tmp_path/'ict_ai').exists()
        assert hashes(tmp_path) == before
    finally:
        worker.stop();worker.thread.join(3)
    assert not worker.thread.is_alive()


def test_private_off_or_pause_file_blocks_only_observer(tmp_path, monkeypatch):
    disk(monkeypatch, 800)
    root = tmp_path/'ict_ai';root.mkdir()
    guard = WriteGuard(dict(DATA_DIR=str(tmp_path), ICT_AI_MODE='OBSERVE'))
    config = root/'runtime-config.json';config.write_text('{"ICT_AI_MODE":"OFF"}')
    with pytest.raises(WritePaused, match='PRIVATE_MODE_CHANGED'):guard()
    config.write_text('{"ICT_AI_MODE":"OBSERVE"}')
    guard()
    (root/'observer.pause').touch()
    with pytest.raises(WritePaused, match='OPERATOR_PAUSE'):guard()


def test_live_limits_cannot_enable_unbounded_backfill(tmp_path):
    for options in ({'max_age_seconds':86400}, {'max_batches':15000}):
        with pytest.raises(ValueError, match='LIVE_CATCHUP'):
            observe_once(None, tmp_path/'absent', NOW, **options)
