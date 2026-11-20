import argparse
import json
from pathlib import Path
from .common import OUTPUT,save

def main():
    p=argparse.ArgumentParser(description='Tanja ICT observer: local read-only/replay; no live orders.')
    sub=p.add_subparsers(dest='command',required=True)
    r=sub.add_parser('replay');r.add_argument('--start',default='2026-05-01');r.add_argument('--end',default='2026-05-31');r.add_argument('--output',type=Path,default=OUTPUT)
    s=sub.add_parser('serve');s.add_argument('--db',type=Path);s.add_argument('--port',type=int,default=8876)
    a=sub.add_parser('audit');a.add_argument('--output',type=Path,default=OUTPUT/'knowledge-audit.json')
    b=sub.add_parser('observe-tanja');b.add_argument('--source',type=Path,required=True);b.add_argument('--db',type=Path,required=True);b.add_argument('--follow',action='store_true')
    args=p.parse_args()
    if args.command=='replay':
        from .replay import run
        result=run(args.start,args.end,output=args.output)
        print(json.dumps({k:v for k,v in result.items() if k not in ('statistics','outcomes','baselines')},ensure_ascii=False))
    elif args.command=='serve':
        from .web import create_app
        db=args.db or Path(json.loads((OUTPUT/'latest.json').read_text())['database'])
        create_app(db).run(host='127.0.0.1',port=args.port,debug=False)
    elif args.command=='audit':
        from .knowledge import Knowledge
        k=Knowledge(args.output);print(json.dumps({k:v for k,v in k.audit.items() if k not in ('sections','case_rule_ids')}))
    elif args.command=='observe-tanja':
        import time
        from .knowledge import Knowledge
        from .store import Store
        from .observer import Observer
        from .bridge import observe_once
        from .common import code_hash
        k=Knowledge()
        manifest=dict(mode='TANJA_READ_ONLY',source=str(args.source.resolve()),knowledge=k.version,index=k.index_version,code=code_hash(),live_enabled=False)
        observer=Observer(Store(args.db,manifest),k)
        while True:
            print(json.dumps(observe_once(observer,args.source,int(time.time()))),flush=True)
            if not args.follow:break
            time.sleep(1)

if __name__=='__main__':main()
