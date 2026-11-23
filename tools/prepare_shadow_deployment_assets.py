"""Stage verified private research artifacts offline; no upload, secrets or activation.
Historical SQLite is snapshotted read-only. Original research hashes are retained.
"""
import argparse,hashlib,json,os,shutil,sqlite3
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def prepare(source,target):
    source=Path(source).resolve();target=Path(target).resolve()
    if target.exists():raise ValueError('NEW_EMPTY_RELEASE_REQUIRED')
    target.mkdir();files={};origins={}
    def copy(src,rel,snapshot=False):
        dst=target/rel;dst.parent.mkdir(parents=True,exist_ok=True)
        if snapshot:
            with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True) as a,sqlite3.connect(dst) as b:a.backup(b)
        else:shutil.copy2(src,dst)
        files[rel]=sha(dst);origins[rel]={'source_sha256':sha(src),'snapshot':snapshot,'bytes':dst.stat().st_size}
    for name,folder in [('frequency','strategy-frequency-20261123'),('episode6','episode6-preraid-20261123/research'),('episode6-parent','episode6-qualification-20261123')]:
        src=source/folder
        names=['summary.json','manifest.json','publication.json']+(['research.sqlite'] if name=='frequency' else ['qualification.sqlite'])
        names+=['chronological-split.json'] if name=='frequency' else []
        names += [str(f.relative_to(src)) for f in (src/'source').rglob('*') if f.is_file()] if (src/'source').is_dir() else []
        for n in names:copy(src/n,'artifacts/'+name+'/'+n)
    for f in (source/'inputs/knowledge').rglob('*'):
        if f.is_file():copy(f,'knowledge/'+str(f.relative_to(source/'inputs/knowledge')))
    copy(source/'inputs/library136/library.sqlite','library136/library.sqlite')
    copy(source/'session-2026-05-20-v2/observer.sqlite3','replay/observer.sqlite3',True)
    copy(source/'prospective-20261123/calendar.json','calendar/calendar.json')
    cal=json.loads((target/'calendar/calendar.json').read_text())
    for n,h in cal['proofs'].items():
        if sha(source/'prospective-20261123'/n)!=h:raise ValueError('CALENDAR_PROOF_CHANGED')
        copy(source/'prospective-20261123'/n,'calendar/'+n)
    def save(rel,v):
        p=target/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2));files[rel]=sha(p)
    base='/data/shadow'
    save('runtime/strategy-research.json',{'directory':base+'/artifacts/frequency'})
    save('runtime/episode6-research.json',{'directory':base+'/artifacts/episode6','parent_directory':base+'/artifacts/episode6-parent'})
    save('runtime/prospective-run.json',{'directory':base+'/run'})
    cfg=dict(root=base+'/run',runtime=base+'/runtime',replay_db=base+'/replay/observer.sqlite3',corpus=base+'/knowledge/corpus',atlas=base+'/knowledge/atlas',feed_report=base+'/feed-report.json',activation=base+'/activation.json',release_manifest=base+'/release-manifest.json',feed_origin='http://agent-50k.railway.internal:8080',start_at=1795388400)
    save('worker-config.json',cfg)
    save('activation.PROPOSAL.json',{'status':'NOT_AUTHORIZED','passive_only':True,'orders_enabled':False,'paid_calls_enabled':False,'configuration_sha256':hashlib.sha256(json.dumps(cfg,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'valid_from':None,'expires_at':min(hi for lo,hi in cal['exchange_open_intervals_utc'] if hi==max(v[1] for v in cal['exchange_open_intervals_utc'])),'requirements':['Fresh restricted intake binding receipt at activation','Separate deployment and hosting approval','Verify code and asset manifests','Same symbol mapping checked on every page','No real-time entitlement claim']})
    # No executable feed-report / activation: this package cannot start a real observer.
    (target/'release-manifest.json').write_text(json.dumps({'version':'shadow-private-assets-v1','files':files,'provenance':origins,'activated':False},indent=2))
    return {'files':len(files),'bytes':sum((target/k).stat().st_size for k in files),'manifest_sha256':sha(target/'release-manifest.json'),'activated':False}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps(prepare(a.source,a.output)))
