"""Offline operator import: historical MNQ only, no jobs or orders are created."""
import argparse
import csv
import hashlib
import io
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

PRICE_FIELDS = ('open', 'high', 'low', 'close', 'volume')


def parse_csv(path):
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if reader.fieldnames != ['ts_event', *PRICE_FIELDS]:
        raise ValueError('Expected ts_event,open,high,low,close,volume')
    bars, duplicates, count = {}, 0, 0
    for count, row in enumerate(reader, 1):
        dt = datetime.fromisoformat(row['ts_event'])
        if dt.tzinfo is None or dt.microsecond or dt.timestamp() % 60:
            raise ValueError(f'Row {count}: timezone or minute boundary missing')
        start = int(dt.timestamp())
        values = tuple(float(row[k]) for k in PRICE_FIELDS)
        o, h, l, c, v = values
        if (not all(math.isfinite(x) for x in values) or min(o,h,l,c) <= 0
                or v < 0 or h < max(o,l,c) or l > min(o,c)
                or any(abs(x*4-round(x*4)) > 1e-6 for x in values[:4])):
            raise ValueError(f'Row {count}: invalid OHLCV')
        if start in bars:
            if bars[start] != values:
                raise ValueError(f'Row {count}: conflicting duplicate')
            duplicates += 1
        bars[start] = values
    if not bars:
        raise ValueError('Empty archive')
    return raw, bars, dict(sha256=hashlib.sha256(raw).hexdigest(), rows=count,
        unique=len(bars), identical_duplicates=duplicates,
        first_start=min(bars), last_start=max(bars))


def import_history(store, path, *, apply=False, now=None, limit=12000, min_overlap=100):
    now = time.time() if now is None else now
    if type(limit) is not int or not 1 <= limit <= 12000:
        raise ValueError('Import limit must be 1..12000')
    raw, bars, report = parse_csv(path)
    if max(bars)+60 > now-180:
        raise ValueError('Archive must end at least three minutes before import')
    # This validates the recent series only, not historical roll conventions.
    chosen = sorted(bars)[-limit:]
    with store.connect() as db:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM metadata WHERE key='tickers'").fetchone()
        ticker = json.loads(row[0]).get('MNQ') if row else None
        if ticker != 'CME_MINI:MNQ1!':
            raise ValueError('This import requires the verified continuous MNQ1! binding')
        existing = {r['start']: json.loads(r['payload']) for r in db.execute(
            "SELECT start,payload FROM bars WHERE symbol='MNQ' AND start BETWEEN ? AND ?",
            (min(chosen),max(chosen)))}
        overlaps = [t for t in chosen if t in existing]
        conflicts = [t for t in overlaps if tuple(existing[t][k] for k in PRICE_FIELDS) != bars[t]]
        if len(overlaps) < min_overlap or conflicts:
            raise ValueError(f'Series check failed: {len(overlaps)} overlaps, {len(conflicts)} conflicts')
        new = [t for t in chosen if t not in existing]
        report.update(ticker=ticker, timestamp_semantics='UTC bar open; exact overlap verified',
            overlapping_exact=len(overlaps), conflicts=0, selected=len(chosen),
            inserted=len(new) if apply else 0, would_insert=len(new),
            active_first_start=min(chosen), active_last_start=max(chosen),
            imported_at=now if apply else None, jobs_created=0, orders_sent=0,
            older_history='Archived only; historical roll/adjustment conventions unverified',
            mode='applied' if apply else 'preview')
        if not apply:
            return report
        # Original bytes and per-row ownership persist on the same durable volume.
        db.execute('CREATE TABLE IF NOT EXISTS history_imports (sha256 TEXT PRIMARY KEY, imported_at REAL, source_name TEXT, original_csv BLOB, report TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS history_import_rows (sha256 TEXT, symbol TEXT, start INTEGER, PRIMARY KEY(sha256,symbol,start))')
        if db.execute('SELECT 1 FROM history_imports WHERE sha256=?',(report['sha256'],)).fetchone():
            report.update(mode='already_imported', inserted=0)
            return report
        for start in new:
            b = dict(schema_version=1,feed_version='tanja-1m-v1',symbol='MNQ',ticker=ticker,
                timeframe='1',session='regular',standard=True,confirmed=True,
                bar_open_ms=start*1000,bar_close_ms=(start+60)*1000,sent_at_ms=int(now*1000),
                **dict(zip(PRICE_FIELDS,bars[start])))
            # received=import time keeps this data out of every older frozen decision.
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('MNQ',start,start+60,now,json.dumps(b)))
            db.execute('INSERT INTO history_import_rows VALUES(?,?,?)',(report['sha256'],'MNQ',start))
        db.execute('INSERT INTO history_imports VALUES(?,?,?,?,?)',
            (report['sha256'],now,Path(path).name,raw,json.dumps(report)))
    return report


if __name__ == '__main__':
    from store import Store
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv'); parser.add_argument('--data-dir',default='/data')
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    store = Store(args.data_dir)
    if args.apply:
        # Consistent SQLite backup; never copy a running WAL database as a plain file.
        backup = str(Path(args.data_dir)/f'before-history-{int(time.time())}.sqlite3')
        with store.connect() as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
    print(json.dumps(import_history(store,args.csv,apply=args.apply),indent=2))
