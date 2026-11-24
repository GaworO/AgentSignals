"""Build and verify an OFFLINE candidate. Never replaces/deletes the source or WAL.

Use a diagnostic copy on a filesystem with working space, not the full volume.
No production activation is performed by this command.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.tanja.ict_agent.common import encode
from services.tanja.ict_agent import retrieval_storage as rs


def fingerprints(db, tables):
    result={}
    for table in tables:
        cols=db.execute('PRAGMA table_info("'+table+'")').fetchall()
        order=[x[1] for x in sorted(cols,key=lambda c:c[5]) if x[5]] or [x[1] for x in cols]
        h=hashlib.sha256();count=0
        query='SELECT * FROM "'+table+'" ORDER BY '+','.join('"'+c+'"' for c in order)
        for row in db.execute(query):
            row=list(row)
            if table=='retrievals':row[1]=encode(rs.unpack(db,row[1],row[0]))
            for i,v in enumerate(row):
                if isinstance(v,bytes):row[i]={'bytes_hex':v.hex()}
            raw=encode(row).encode();h.update(len(raw).to_bytes(8,'big'));h.update(raw);count+=1
        result[table]=dict(rows=count,sha256=h.hexdigest())
    return result


def compact(source,target):
    source=Path(source).resolve();target=Path(target).resolve()
    if source==target or target.exists():raise ValueError('NEW_OFFLINE_DESTINATION_REQUIRED')
    if not source.is_file():raise ValueError('SOURCE_REQUIRED')
    # The VACUUM runs ONLY on this new copy, and only with ample working space.
    needed=source.stat().st_size*3+512*1024**2
    if shutil.disk_usage(target.parent).free<needed:raise ValueError('INSUFFICIENT_OFFLINE_WORKING_SPACE')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            tables=[r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            before=fingerprints(src,tables)
            with dst:
                rs.initialize(dst)
                for ident,raw in src.execute('SELECT id,payload FROM retrievals'):
                    value=rs.unpack(src,raw,ident)
                    dst.execute('UPDATE retrievals SET payload=? WHERE id=?',(rs.pack(dst,value),ident))
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.execute('VACUUM')
            after=fingerprints(dst,tables)
            if before!=after:raise ValueError('SEMANTIC_FINGERPRINT_MISMATCH')
            if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('QUICK_CHECK_FAILED')
            notes=dst.execute('SELECT count(*) FROM retrieval_notes').fetchone()[0]
    return dict(source=str(source),target=str(target),source_bytes=source.stat().st_size,
                target_bytes=target.stat().st_size,unique_notes=notes,all_original_tables=after,
                verification='EXACT_CANONICAL_PAYLOADS_AND_IDENTICAL_OTHER_ROWS',quick_check='ok',
                source_modified=False,production_activated=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--target',required=True)
    args=p.parse_args();print(json.dumps(compact(args.source,args.target),indent=2))
