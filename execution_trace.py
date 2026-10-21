"""Durable local timing evidence. No account credentials or raw provider payloads."""
import json
import os
import sqlite3
import time
from pathlib import Path


def record(signal_id, stage, *, at_ms=None, **facts):
    path = Path(os.environ.get('DATA_DIR', str(Path(__file__).parent))) / 'execution_trace.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS trace (
            id INTEGER PRIMARY KEY, signal_id TEXT NOT NULL, stage TEXT NOT NULL,
            observed_ms INTEGER NOT NULL, event_ms INTEGER NOT NULL, facts TEXT NOT NULL,
            UNIQUE(signal_id,stage,event_ms,facts))''')
        observed = int(time.time()*1000)
        db.execute('INSERT OR IGNORE INTO trace(signal_id,stage,observed_ms,event_ms,facts) VALUES(?,?,?,?,?)',
                   (str(signal_id), stage, observed, observed if at_ms is None else int(at_ms),
                    json.dumps(facts, sort_keys=True, allow_nan=False)))


def recent(limit=100):
    path = Path(os.environ.get('DATA_DIR', str(Path(__file__).parent))) / 'execution_trace.sqlite3'
    if not path.is_file():
        return []
    with sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            'SELECT signal_id,stage,observed_ms,event_ms,facts FROM trace ORDER BY id DESC LIMIT ?',
            (max(1, min(int(limit), 500)),))]
    for row in rows:
        row['facts'] = json.loads(row['facts'])
    return rows
