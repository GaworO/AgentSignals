"""Append-only evidence + transactional mutable memory. Never opens Tanja's DB for writes."""
import json
import sqlite3
from pathlib import Path
from .common import encode, digest

ALLOWED = {
    'DISCOVERED':{'DEVELOPING','WAITING_FOR_CONFIRMATION','INVALIDATED','EXPIRED','REJECTED','CANCELLED'},
    'DEVELOPING':{'WAITING_FOR_CONFIRMATION','READY','INVALIDATED','EXPIRED','REJECTED','CANCELLED'},
    'WAITING_FOR_CONFIRMATION':{'READY','INVALIDATED','EXPIRED','REJECTED','CANCELLED'},
    'READY':{'ORDER_PENDING','INVALIDATED','EXPIRED','REJECTED','CANCELLED'},
    'ORDER_PENDING':{'ENTERED','CANCELLED','REJECTED'},
    'ENTERED':{'MANAGING','CLOSED'},'MANAGING':{'CLOSED'},
    **{s:set() for s in ('CLOSED','INVALIDATED','EXPIRED','REJECTED','CANCELLED')}}

def transition(setup, state, at, reason, evidence):
    old=setup['state']
    if old==state:return None
    if state not in ALLOWED[old]:raise ValueError('ILLEGAL_SETUP_TRANSITION:'+old+'->'+state)
    setup['state']=state;setup['updated_at']=at
    item=dict(setup_id=setup['id'],before=old,after=state,at=at,reason=reason,evidence_ids=evidence)
    item['id']=digest(item)[:24]
    setup['last_transition']=item
    return item

class Store:
    def __init__(self,path,manifest=None):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,as_of INTEGER NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bars(id TEXT PRIMARY KEY,symbol TEXT,contract TEXT,ts INTEGER,available_at INTEGER,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS frames(id TEXT PRIMARY KEY,symbol TEXT,contract TEXT,timeframe INTEGER,ts INTEGER,available_at INTEGER,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS frame_history ON frames(symbol,contract,timeframe,available_at);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,as_of INTEGER,symbol TEXT,kind TEXT,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,as_of INTEGER NOT NULL,decision TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(as_of);
            CREATE TABLE IF NOT EXISTS transitions(id TEXT PRIMARY KEY,setup_id TEXT,as_of INTEGER,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS setups(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_reviews(id TEXT PRIMARY KEY,snapshot_id TEXT UNIQUE,status TEXT,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS measurements(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            ''')
            if manifest is not None:
                old=db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
                if old and old[0]!=encode(manifest):raise ValueError('RUN_MANIFEST_CHANGED_USE_NEW_RUN')
                db.execute("INSERT OR IGNORE INTO metadata VALUES('manifest',?)",(encode(manifest),))

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def readonly(self):
        return sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)

    def snapshot(self,ident=None):
        with self.readonly() as db:
            row=db.execute('SELECT payload FROM snapshots WHERE id=?',(ident,)).fetchone() if ident else db.execute('SELECT payload FROM snapshots ORDER BY as_of DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None

    def chart(self, snapshot, symbol='NQ', timeframe=1,limit=180):
        contract=snapshot['context'].get(symbol,{}).get('contract')
        if not contract:return []
        with self.readonly() as db:
            rows=db.execute('SELECT payload FROM frames WHERE symbol=? AND contract=? AND timeframe=? AND available_at<=? ORDER BY ts DESC LIMIT ?',
                            (symbol,contract,timeframe,snapshot['as_of'],limit)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def timeline(self,limit=400,before=None):
        with self.readonly() as db:
            rows=db.execute('SELECT id,as_of,decision,payload FROM snapshots WHERE as_of<? ORDER BY as_of DESC LIMIT ?',
                            (before or 2**62,limit)).fetchall()
        return [dict(id=r[0],as_of=r[1],decision=r[2],event_kinds=json.loads(r[3])['event_kinds']) for r in rows]

    def stats(self, integrity=False):
        with self.readonly() as db:
            result={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('bars','frames','events','snapshots','setups','transitions','ai_reviews')}
            result['decisions']=dict(db.execute('SELECT decision,count(*) FROM snapshots GROUP BY decision'))
            row=db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
            result['manifest']=json.loads(row[0]) if row else {}
            result['integrity']=db.execute('PRAGMA integrity_check').fetchone()[0] if integrity else 'NOT_CHECKED_ON_HTTP_REQUEST'
        return result
