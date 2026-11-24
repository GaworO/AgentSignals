"""Explicit offline code-only manifest handoff; never resets observation memory.

Run only for the reviewed IFVG observation release after a verified recovery
backup. The observer lock must be free (new code refuses the old manifest).
Snapshots retain their original manifest hashes; both manifests remain readable.
No automatic migrations, startup bypass, trading flag or source DB changes.
"""
import argparse
import fcntl
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.tanja.ict_agent.common import code_hash, digest, encode
from services.tanja.ict_agent.storage_guard import WriteGuard


def advance(path, lock_path, expected_old, expected_new, *, guard):
    guard()
    with Path(lock_path).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw', uri=True) as db:
            db.execute('BEGIN IMMEDIATE')
            old = json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
            if old['code_hash'] != expected_old:
                raise ValueError('EXPECTED_OLD_MANIFEST_REQUIRED')
            if expected_old == expected_new:
                raise ValueError('NEW_CODE_REQUIRED')
            if old.get('mode') != 'READ_ONLY_OBSERVER' or old.get('orders_enabled') is not False:
                raise ValueError('OBSERVATION_ONLY_REQUIRED')
            if old.get('intake_policy') != dict(kind='BOUNDED_RECENT_WINDOW', max_age_seconds=300,
                                               max_batches_per_poll=5, older_history='NOT_REPLAYED'):
                raise ValueError('INTAKE_LIMITS_MUST_BE_PRESERVED')
            new = dict(old, code_hash=expected_new)
            for manifest in (old, new):
                key = 'manifest:'+digest(manifest)
                prior = db.execute('SELECT payload FROM metadata WHERE key=?',(key,)).fetchone()
                if prior and prior[0] != encode(manifest):raise ValueError('IMMUTABILITY_CONFLICT')
                db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',(key, encode(manifest)))
            evidence = dict(kind='IFVG_OBSERVATION_CODE_ONLY_HANDOFF', at=time.time(),
                            before=digest(old), after=digest(new), changed_fields=['code_hash'])
            db.execute('INSERT INTO metadata VALUES(?,?)',('manifest-handoff:'+digest(evidence), encode(evidence)))
            guard()
            db.execute("UPDATE metadata SET payload=? WHERE key='manifest'",(encode(new),))
        return evidence


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--expected-old',required=True);p.add_argument('--expected-new',required=True)
    p.add_argument('--root',type=Path,default=Path('/data/builder50'));a=p.parse_args()
    cfg=json.loads((a.root/'ict_ai/runtime-config.json').read_text())
    if cfg['ICT_AI_MODE']!='OBSERVE' or cfg.get('ICT_AI_PAID_APPROVED')!='false':raise ValueError('OBSERVE_WITHOUT_PAID_ONLY')
    if int(cfg.get('ICT_AI_MIN_FREE_MB',512))!=512:raise ValueError('KEEP_512_MIB')
    if code_hash()!=a.expected_new:raise ValueError('DEPLOYED_CODE_MISMATCH')
    cfg['DATA_DIR']=str(a.root)
    print(json.dumps(advance(cfg['ICT_AI_OBSERVE_DB'],a.root/'ict_ai/observer.lock',
                             a.expected_old,a.expected_new,guard=WriteGuard(cfg)),indent=2))
