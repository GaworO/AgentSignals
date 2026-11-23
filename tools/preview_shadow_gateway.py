"""LOCAL ONLY: actual production mount + actual private worker HTTP reader.
No observer started, external provider/broker routes disabled. Not in worker image.
"""
import base64,json,os,sys,threading
from pathlib import Path
from flask import Flask,request,abort
from werkzeug.serving import make_server
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.ict_shadow.worker import create_app
from services.tanja.ict_agent.shadow_gateway import register
from services.tanja.ict_agent.shadow_transport import PrivateReader
BASE=Path('/private/tmp/ict-shadow-recovery-20261123')
runtime=BASE/'integration-preview-runtime';runtime.mkdir(exist_ok=True)
for name in ('strategy-research.json','episode6-research.json','prospective-run.json','observer-incidents.json'):
    src=BASE/'web-v2'/name
    if src.exists():(runtime/name).write_bytes(src.read_bytes())
cfg=dict(root=str(BASE/'prospective-20261123/run'),runtime=str(runtime),replay_db=str(BASE/'session-2026-05-20-v2/observer.sqlite3'))
conf=runtime/'private-view-config.json';conf.write_text(json.dumps(cfg))
token='local-integration-fixture-not-production-123456'
os.environ['ICT_SHADOW_VIEW_TOKEN']=token
worker=create_app(conf,start=False)
api=make_server('127.0.0.1',8881,worker,threaded=True);threading.Thread(target=api.serve_forever,daemon=True).start()
app=Flask(__name__)
config={'DATA_DIR':str(runtime),'TANJA_DASHBOARD_PASSWORD':'integration-preview-only','ICT_AI_MODE':'OFF','ICT_AI_REPLAY_DB':cfg['replay_db'],'ICT_AI_LIBRARY_DB':str(BASE/'inputs/library136/library.sqlite'),'ICT_SHADOW_UI_ENABLED':'true'}
header='Basic '+base64.b64encode(b'tanja:integration-preview-only').decode()
@app.before_request
def local_fixture():
    if request.remote_addr not in ('127.0.0.1','::1') or request.method not in ('GET','HEAD'):abort(403)
    # Preview-only fixture. The deployable mount uses unchanged actual user auth.
    request.environ['HTTP_AUTHORIZATION']=header
register(app,config,lambda:request.headers.get('Authorization')==header,reader=PrivateReader('http://127.0.0.1:8881',token,allow_loopback=True))
import dashboard,ict_shadow_cloud
dashboard.register(app);ict_shadow_cloud.register(app,config,start=False)
server=make_server('127.0.0.1',8880,app,threaded=True)
print('LOCAL_INTEGRATION_PREVIEW_ONLY :8880 → private read API :8881; observer not started',flush=True)
try:server.serve_forever()
finally:api.shutdown()
