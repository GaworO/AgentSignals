"""Validated advisory hypothesis history, separate from deterministic setups/orders."""
import json
from .common import digest,encode


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS reasoning_hypotheses(
      id TEXT,variant TEXT,as_of INTEGER,payload TEXT,PRIMARY KEY(id,variant));
    CREATE TABLE IF NOT EXISTS reasoning_transitions(
      id TEXT PRIMARY KEY,review_id TEXT,hypothesis_id TEXT,variant TEXT,as_of INTEGER,payload TEXT);
    CREATE INDEX IF NOT EXISTS reasoning_transition_time ON reasoning_transitions(variant,as_of);''')


def record(db,result):
    if result['status']!='VALIDATED_ADVISORY':return
    a=result['answer'];variant=result['variant'];at=a['as_of']
    for assessment in a['assessments']:
        ident=assessment['hypothesis_id']
        old=db.execute('SELECT as_of,payload FROM reasoning_hypotheses WHERE id=? AND variant=?',(ident,variant)).fetchone()
        if old and old[0]>=at:raise ValueError('REASONING_MEMORY_NONCHRONOLOGICAL')
        previous=json.loads(old[1]) if old else None
        item=dict(id=digest([result['id'],ident])[:24],review_id=result['id'],snapshot_id=a['snapshot_id'],
            hypothesis_id=ident,variant=variant,as_of=at,from_state=previous['state'] if previous else None,
            state=assessment['lifecycle'],assessment=assessment,decision=a['decision'],
            selected=ident==a['preferred_hypothesis_id'],origin=result['origin'],orders_sent=0)
        db.execute('INSERT INTO reasoning_transitions VALUES(?,?,?,?,?,?)',(item['id'],result['id'],ident,variant,at,encode(item)))
        db.execute('INSERT INTO reasoning_hypotheses VALUES(?,?,?,?) ON CONFLICT(id,variant) DO UPDATE SET as_of=excluded.as_of,payload=excluded.payload',(ident,variant,at,encode(item)))
