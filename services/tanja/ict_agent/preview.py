"""Loopback review harness for the SAME Agent50k registrations; never production.
No agent.py import: importing it starts unrelated existing trading workers.
"""
import argparse,tempfile
from pathlib import Path
from flask import Flask,request

def create_preview(db):
    import dashboard,ict_ai_trader,ict_shadow_cloud
    app=Flask(__name__);root=Path(tempfile.mkdtemp(prefix='ict-ai-preview-'))
    cfg=dict(DATA_DIR=str(root),TANJA_DASHBOARD_PASSWORD='local-preview-fixture',ICT_AI_REPLAY_DB=str(Path(db).resolve()),ICT_AI_MODE='OFF')
    @app.before_request
    def local_only():
        if request.remote_addr not in ('127.0.0.1','::1'):return 'Loopback preview only',403
        # Local review of identical protected handlers, without exposing any real password.
        import base64
        request.environ['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:local-preview-fixture').decode()
    dashboard.register(app);ict_shadow_cloud.register(app,cfg,start=False);ict_ai_trader.register(app,cfg,start=False)
    return app
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--port',type=int,default=8877);a=p.parse_args()
    create_preview(a.db).run(host='127.0.0.1',port=a.port,debug=False)
