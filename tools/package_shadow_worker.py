"""Create a minimal internal-worker build context. Never upload or deploy it."""
import argparse,ast,hashlib,json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MODULES=('__init__ common resilient_io market store observer scenarios knowledge sequences ai risk bridge prospective_shadow shadow_mvp shadow_chart shadow_sessions shadow_views shadow_transport shadow_gateway shadow_session_report strategy_research_web episode6_web episode6_preraid episode6_qualification').split()
def build(target):
    target=Path(target).resolve()
    if target.exists():raise ValueError('NEW_EMPTY_CONTEXT_REQUIRED')
    for name in MODULES:
        tree=ast.parse((ROOT/'services/tanja/ict_agent'/(name+'.py')).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom) and node.level==1 and node.module and node.module.split('.')[0] not in MODULES:
                raise ValueError('INCOMPLETE_READ_ONLY_IMPORT_CLOSURE:'+node.module)
    files=[Path('services/tanja/ict_agent')/(n+'.py') for n in MODULES]
    files += [Path('services/tanja/ict_agent/static')/n for n in ('shadow-evidence.js','strategy-research.js','episode6-research.js')]
    files += [Path('services/ict_shadow')/n for n in ('worker.py','wsgi.py','activate.py','gunicorn_conf.py','Dockerfile','railway.json','requirements.txt')]
    files += [Path('tools')/n for n in ('observe_railway_shadow.py','run_passive_shadow.py')]
    hashes={}
    for rel in files:
        p=target/rel;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,p);hashes[str(rel)]=hashlib.sha256(p.read_bytes()).hexdigest()
    (target/'code-manifest.json').write_text(json.dumps({'version':'shadow-private-code-v1','files':hashes},indent=2))
    return hashes
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps({'files':len(build(a.output)),'uploaded':False}))
