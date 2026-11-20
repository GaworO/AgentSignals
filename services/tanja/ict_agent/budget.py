"""One persistent lifetime cap shared by live/replay/ablation journals.

Reservations survive crashes and uncertain transport outcomes. No automatic retry,
daily reset, raising a stored cap, or credential storage.
"""
import json
import math
import sqlite3
import time
from pathlib import Path


def reserve(cfg, ident, amount):
    path=cfg.get('ICT_AI_TOTAL_BUDGET_DB')
    if not path:
        if cfg.get('ICT_AI_TOTAL_USD'):raise ValueError('SHARED_BUDGET_DATABASE_REQUIRED')
        return
    cap=float(cfg['ICT_AI_TOTAL_USD']);approval=cfg['ICT_AI_APPROVAL_ID']
    if not math.isfinite(cap) or not 0<cap<=8 or not math.isfinite(amount) or amount<=0:
        raise ValueError('INVALID_TOTAL_BUDGET')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path,timeout=30) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS approval(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT,cap REAL);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,reserved REAL,actual REAL,state TEXT,at REAL);
        CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT);''')
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT name,cap FROM approval WHERE id=1').fetchone()
        if old and old!=(approval,cap):raise ValueError('TOTAL_BUDGET_APPROVAL_IMMUTABLE')
        db.execute('INSERT OR IGNORE INTO approval VALUES(1,?,?)',(approval,cap))
        if db.execute('SELECT 1 FROM control').fetchone():raise ValueError('TOTAL_BUDGET_RECONCILIATION_REQUIRED')
        if db.execute('SELECT 1 FROM attempts WHERE id=?',(ident,)).fetchone():raise ValueError('TOTAL_ALREADY_RESERVED_NO_RETRY')
        used=db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM attempts').fetchone()[0]
        if used+amount>cap:raise ValueError('TOTAL_EXPERIMENT_BUDGET_CAP')
        db.execute('INSERT INTO attempts VALUES(?,?,NULL,?,?)',(ident,amount,'RESERVED_NO_RETRY',time.time()))


def finish(cfg, ident, actual):
    path=cfg.get('ICT_AI_TOTAL_BUDGET_DB')
    if not path:return
    with sqlite3.connect(path,timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT reserved FROM attempts WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('TOTAL_RESERVATION_MISSING')
        if actual is not None and (not math.isfinite(actual) or actual<0):raise ValueError('INVALID_ACTUAL_COST')
        if actual is not None and actual>row[0]:db.execute("INSERT OR REPLACE INTO control VALUES('halt','USAGE_EXCEEDS_RESERVATION')")
        db.execute('UPDATE attempts SET actual=?,state=? WHERE id=?',(actual,'ACCOUNTED_CEILING' if actual is not None else 'UNKNOWN_RESERVED',ident))


def status(cfg):
    path=cfg.get('ICT_AI_TOTAL_BUDGET_DB')
    empty=dict(status='NOT_STARTED',cap_usd=float(cfg.get('ICT_AI_TOTAL_USD',0)),calls=0,accounted_usd=0,reserved_unknown_usd=0,consumed_ceiling_usd=0,currency='USD',tax_included=False)
    if not path or not Path(path).is_file():return empty
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT name,cap FROM approval WHERE id=1').fetchone()
        if not row:return empty
        n,known,unknown,total=db.execute('SELECT count(*),coalesce(sum(actual),0),coalesce(sum(case when actual is null then reserved else 0 end),0),coalesce(sum(coalesce(actual,reserved)),0) FROM attempts').fetchone()
        halted=bool(db.execute('SELECT 1 FROM control').fetchone())
    return dict(empty,status='HALTED' if halted else 'RECORDED',approval_id=row[0],cap_usd=row[1],calls=n,accounted_usd=known,reserved_unknown_usd=unknown,consumed_ceiling_usd=total,remaining_usd=max(0,row[1]-total))
