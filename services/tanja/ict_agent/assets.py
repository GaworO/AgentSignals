"""Build a verified private Railway knowledge release; no upload or deployment."""
import argparse,fcntl,hashlib,json,os,shutil,sqlite3,tempfile
from pathlib import Path
from .common import read_bytes,save,digest
from .knowledge import Knowledge

def build(corpus,atlas,destination):
    corpus=Path(corpus).resolve();atlas=Path(atlas).resolve();destination=Path(destination).resolve()
    destination.mkdir(parents=True,exist_ok=True)
    with (corpus/'parallel/coordinator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        knowledge=Knowledge(corpus=corpus,atlas=atlas)
        release_id=digest(dict(rules=knowledge.version,coverage=hashlib.sha256(read_bytes(corpus/'exhaustive/coverage.json')).hexdigest(),atlas=hashlib.sha256(read_bytes(atlas/'reference.json')).hexdigest()))[:20]
        final=destination/release_id
        if final.exists():verify(final);return final
        temp=Path(tempfile.mkdtemp(prefix='.building-',dir=destination))
        try:
            files=['catalogue.json','detailed-rules.json','knowledge-registry.json','exhaustive/coverage.json']
            for vid in {r['video_id'] for rule in knowledge.rules if rule['id'] in knowledge.verified for r in rule['references']}:
                files.append('exhaustive/private_sources/'+vid+'.json.gz')
            for relative in files:
                target=temp/'corpus'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(read_bytes(corpus/relative))
            (temp/'atlas').mkdir();(temp/'atlas/reference.json').write_bytes(read_bytes(atlas/'reference.json'))
            with sqlite3.connect((corpus/'knowledge.sqlite').as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(temp/'corpus/knowledge.sqlite') as dst:src.backup(dst)
            check=Knowledge(corpus=temp/'corpus',atlas=temp/'atlas')
            if check.version!=knowledge.version:raise ValueError('SOURCE_CHANGED_DURING_RELEASE')
            save(temp/'knowledge-audit.json',check.audit)
            entries={str(p.relative_to(temp)):hashlib.sha256(read_bytes(p)).hexdigest() for p in temp.rglob('*') if p.is_file()}
            save(temp/'manifest.json',dict(release_id=release_id,files=entries,private_source_vault=True,live_enabled=False))
            os.rename(temp,final)
        except Exception:
            # Incomplete export is scratch only. No canonical or live file touched.
            shutil.rmtree(temp);raise
    return final

def verify(root):
    root=Path(root);m=json.loads(read_bytes(root/'manifest.json'))
    for rel,sha in m['files'].items():
        path=(root/rel).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(read_bytes(path)).hexdigest()!=sha:raise ValueError('RELEASE_HASH_MISMATCH')
    return m

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--corpus',required=True);p.add_argument('--atlas',required=True);p.add_argument('--destination',required=True);a=p.parse_args()
    print(build(a.corpus,a.atlas,a.destination))
