"""Internal, no-order observer service. No public UI and no provider SDK.
Only an explicit activation document starts the already-tested passive engine.
"""
import fcntl,hashlib,json,os,signal,sqlite3,threading,time,shutil
from pathlib import Path
from flask import Flask,abort,jsonify,request
from services.tanja.ict_agent.shadow_gateway import scoped_auth
from services.tanja.ict_agent.shadow_transport import PrivateReader,ReadFailure
from services.tanja.ict_agent.common import digest,encode


def atomic(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix('.tmp');t.write_text(encode(value));t.replace(p)


def verify_release(path):
    p=Path(path).resolve();data=json.loads(p.read_text());root=p.parent
    for rel,sha in data['files'].items():
        f=(root/rel).resolve()
        if not f.is_relative_to(root) or not f.is_file():raise ValueError('RELEASE_FILE_MISSING')
        h=hashlib.sha256()
        with f.open('rb') as src:
            for chunk in iter(lambda:src.read(1024*1024),b''):h.update(chunk)
        if h.hexdigest()!=sha:raise ValueError('RELEASE_HASH_MISMATCH')
    return data


def check_environment(env):
    # Refuse inherited broker/provider secrets; do not log names/values.
    allowed={'ICT_SHADOW_FEED_TOKEN','ICT_SHADOW_VIEW_TOKEN'}
    if any(k not in allowed and (any(s in k.upper() for s in ('SECRET','TOKEN','PASSWORD','API_KEY','TRADOVATE','CTRADER','BROKER')) or k in ('AUTO_SUBMIT','EXEC_MODE')) for k in env):
        raise ValueError('UNRELATED_CREDENTIAL_OR_EXECUTION_CONFIG_PRESENT')
    if env.get('ICT_SHADOW_FEED_TOKEN') and env.get('ICT_SHADOW_FEED_TOKEN')==env.get('ICT_SHADOW_VIEW_TOKEN'):
        raise ValueError('SEPARATE_SERVICE_SCOPES_REQUIRED')


def verify_activation(cfg,activation,current,code_manifest='/app/code-manifest.json'):
    if not activation.get('passive_only') or activation.get('orders_enabled') is not False or activation.get('paid_calls_enabled') is not False or not activation['valid_from']<=current<activation['expires_at']:
        raise ValueError('EXPLICIT_CURRENT_PASSIVE_ACTIVATION_REQUIRED')
    if activation['configuration_sha256']!=digest(cfg):raise ValueError('ACTIVATION_CONFIG_MISMATCH')
    for key,path in [('assets_manifest_sha256',cfg['release_manifest']),('code_manifest_sha256',code_manifest)]:
        if activation.get(key)!=hashlib.sha256(Path(path).read_bytes()).hexdigest():
            raise ValueError('ACTIVATION_RELEASE_MISMATCH')


def safe_status(obs,mirror,phase,stop_at,now):
    with obs.store.readonly() as db:
        # Aggregate scalar fields; never load the full journal into HTTP RAM.
        genuine="SELECT d.candidate_id FROM shadow_records d JOIN passive_log p ON p.kind='DURABLE' AND p.key=json_extract(d.payload,'$.persistence_batch') WHERE d.stage='DETECTED' AND json_extract(p.payload,'$.prospective_timing_verified')=1"
        latest="WITH latest AS (SELECT candidate_id,max(as_of) at FROM shadow_records WHERE stage='ASSESSMENT' AND candidate_id IN ("+genuine+") GROUP BY candidate_id) SELECT r.payload FROM latest l JOIN shadow_records r ON r.candidate_id=l.candidate_id AND r.as_of=l.at AND r.stage='ASSESSMENT'"
        counts=dict(db.execute("SELECT json_extract(payload,'$.symbol'),count(*) FROM ("+latest+") GROUP BY 1"))
        decisions=dict(db.execute("SELECT json_extract(payload,'$.decision'),count(*) FROM ("+latest+") GROUP BY 1"))
        scalar=lambda sql:db.execute(sql).fetchone()[0]
        out=dict(mode='PROSPECTIVE_SHADOW',operational_state=phase,heartbeat_at=now,stop_at=stop_at,feed_classification='FEED_UNVERIFIED',llm_status='PENDING_LLM',orders_enabled=False,paid_calls_enabled=False,broker_transport='ABSENT',source_qualified=0,
            prospective_candidates=sum(counts.values()),candidate_counts={s:counts.get(s,0) for s in ('NQ','MNQ','ES')},decisions=decisions,
            catchup_candidates=scalar("SELECT count(*) FROM shadow_records WHERE stage='DETECTED'")-sum(counts.values()),
            prospective_outcomes=scalar("SELECT count(*) FROM shadow_records WHERE stage='OUTCOME' AND candidate_id IN ("+genuine+") AND json_extract(payload,'$.prospective_outcome_eligible')=1"),
            durable_batches=scalar("SELECT count(*) FROM passive_log WHERE kind='DURABLE'"),
            verified_prospective_batches=scalar("SELECT count(*) FROM passive_log WHERE kind='DURABLE' AND json_extract(payload,'$.prospective_timing_verified')=1"),
            unreceipted_intents=scalar("SELECT count(*) FROM passive_log a WHERE kind='INTENT' AND NOT EXISTS(SELECT 1 FROM passive_log b WHERE b.kind='DURABLE' AND b.key=a.key)"),
            source_gaps=scalar("SELECT count(*) FROM passive_log WHERE kind='SOURCE_GAP'"),last_bar_start=scalar('SELECT max(ts) FROM bars'))
    out['feeds']={}
    with sqlite3.connect(Path(mirror).resolve().as_uri()+'?mode=ro',uri=True) as db:
        for s in ('NQ','MNQ','ES'):
            r=db.execute('SELECT end,received FROM '+('collection_bars' if s=='NQ' else 'bars')+' WHERE symbol=? ORDER BY start DESC LIMIT 1',(s,)).fetchone()
            out['feeds'][s]={'latest_close':r[0],'latest_received':r[1],'close_to_receipt_seconds':r[1]-r[0],'age_seconds':now-r[0]} if r else {}
    exchange_open=any(lo<=now<hi for lo,hi in obs.report['exchange_calendar']['open_intervals_utc'])
    out['hosting']='RAILWAY_INTERNAL';out['calendar_valid_until']=obs.report['exchange_calendar'].get('valid_until')
    out['exchange_status']='OPEN' if exchange_open else 'CLOSED_BY_PINNED_CALENDAR'
    out['alerts']=['STALE_FEED_'+s for s,h in out['feeds'].items() if exchange_open and (not h or h['age_seconds']>180)]
    return out


class Poller:
    def __init__(self,obs,mirror,root,reader,start,stop_at,*,clock=time.time,clock_ns=time.time_ns):
        self.obs=obs;self.mirror=Path(mirror);self.root=Path(root);self.reader=reader;self.start=start;self.stop_at=stop_at;self.clock=clock;self.clock_ns=clock_ns
        with obs.store.connect() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS transport_audit(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);CREATE TRIGGER IF NOT EXISTS transport_audit_no_update BEFORE UPDATE ON transport_audit BEGIN SELECT RAISE(ABORT,'APPEND_ONLY');END;CREATE TRIGGER IF NOT EXISTS transport_audit_no_delete BEFORE DELETE ON transport_audit BEGIN SELECT RAISE(ABORT,'APPEND_ONLY');END;")
        with obs.store.readonly() as db:r=db.execute('SELECT payload FROM transport_audit ORDER BY id DESC LIMIT 1').fetchone()
        self.cursor=json.loads(r[0])['cursor'] if r else start-60
    def cycle(self):
        from tools.observe_railway_shadow import mirror_ingest,process_available
        now=self.clock()
        if shutil.disk_usage(self.root).free<256*1024*1024:raise ValueError('STORAGE_LOW_NO_DELETION')
        if now>=self.stop_at:raise ValueError('ACTIVATION_OR_CALENDAR_EXPIRED')
        after=max(self.start-60,self.cursor-180)
        raw,_=self.reader.get('/internal/ict-shadow/candles?after='+str(after))
        seen=self.clock_ns();envelope=json.loads(raw)
        if set(envelope.get('feed_health',{}))!={'NQ','MNQ','ES'}:raise ValueError('SOURCE_HEALTH_REQUIRED')
        end=envelope.get('page_end')
        if type(end) is not int or end<after or end>after+300*60 or end>int(envelope['captured_at']//60)*60-60:raise ValueError('INVALID_PAGE_CURSOR')
        # Receipts, future bars and exact continuous instrument identities checked here.
        for r in envelope['rows']:
            if not after<r[1]<=end:raise ValueError('OUT_OF_PAGE_BAR')
        added=mirror_ingest(self.mirror,envelope,seen,self.obs.report['instrument_binding']['tickers'])
        envhash=digest(envelope)
        p=self.root/'envelopes'/(envhash+'.json');p.parent.mkdir(exist_ok=True)
        if not p.exists():
            with p.open('x') as f:f.write(encode(dict(envelope,local_transport_seen_at_ns=seen)));f.flush();os.fsync(f.fileno())
        processed,gaps=process_available(self.obs,self.mirror,seen)
        # Commit cursor only after durable mirror + decisions. Re-reading after a crash is idempotent.
        with self.obs.store.connect() as db:
            db.execute('PRAGMA synchronous=FULL');db.execute('INSERT INTO transport_audit(payload) VALUES(?)',(encode(dict(cursor=end,envelope_sha256=envhash,seen_at_ns=seen,processed=processed)),))
        self.cursor=end
        state=safe_status(self.obs,self.mirror,'PROSPECTIVE_SHADOW_RUNNING',self.stop_at,self.clock())
        state.update(processed_this_poll=processed,new_mirror_rows=added,pending_pairs=gaps[:100],transport_cursor=end)
        atomic(self.root/'transport-health.json',dict(state='CONNECTED',at=self.clock(),cursor=end))
        atomic(self.root/'status.json',state)
        return state


def run_observer(cfg,shutdown):
    from services.tanja.ict_agent.prospective_shadow import PassiveObserver
    from services.tanja.ict_agent.store import Store
    from services.tanja.ict_agent.knowledge import Knowledge
    root=Path(cfg['root']);root.mkdir(parents=True,exist_ok=True)
    report=json.loads(Path(cfg['feed_report']).read_text());activation=json.loads(Path(cfg['activation']).read_text())
    current=time.time()
    verify_activation(cfg,activation,current)
    end=min(activation['expires_at'],report['exchange_calendar']['valid_until'])
    # New server journal, never overwrite or retroactively reclassify the laptop session.
    manifest=dict(version='agent50k-private-shadow-v1',mode='PROSPECTIVE_SHADOW',session='continuous',start=cfg['start_at'],end=end,coverage={},symbols=['NQ','MNQ','ES'],feed_report_sha256=digest(report),configuration_sha256=digest(cfg),orders_enabled=False,paid_calls_enabled=False)
    mf=root/'manifest.json'
    if mf.exists():
        old=json.loads(mf.read_text());check={k:v for k,v in old.items() if k!='prospective_after_bar_start'}
        if check!=manifest:raise ValueError('SERVER_RUN_CONFIG_CHANGED')
        manifest=old
    else:
        manifest['prospective_after_bar_start']=int(current//60)*60+60
        with mf.open('x') as f:f.write(encode(manifest));f.flush();os.fsync(f.fileno())
    store=Store(root/'observer.sqlite3',manifest)
    knowledge=Knowledge(corpus=cfg['corpus'],atlas=cfg['atlas'])
    obs=PassiveObserver(store,knowledge,report,enabled=True)
    reader=PrivateReader(cfg['feed_origin'],os.environ['ICT_SHADOW_FEED_TOKEN'],timeout=4)
    poller=Poller(obs,root/'intake-mirror.sqlite3',root,reader,cfg['start_at'],end)
    process_key=str(time.time_ns())
    obs._write('PROCESS_START',process_key,dict(at=time.time(),cursor=poller.cursor,configuration_sha256=digest(cfg),code_manifest_sha256=activation['code_manifest_sha256']))
    errors=0
    try:
        while not shutdown.is_set() and time.time()<end:
            try:
                poller.cycle();errors=0
            except ReadFailure:
                errors+=1
                atomic(root/'transport-health.json',dict(state='READ_ONLY_RECONNECT_WAIT',at=time.time(),error_code='INTAKE_TEMPORARILY_UNAVAILABLE',attempts=errors))
                # Read-only retries only. No provider, broker or mutable upstream operations.
            delay=min(60,5*2**min(errors,4)) if errors else (0.1 if poller.cursor<time.time()-600 else 10)
            shutdown.wait(delay)
        if (root/'status.json').exists():
            state=json.loads((root/'status.json').read_text());state.update(operational_state='STOPPED_ACTIVATION_END' if time.time()>=end else 'STOPPED_SERVICE',heartbeat_at=time.time());atomic(root/'status.json',state)
    finally:
        try:obs._write('PROCESS_STOP',process_key,dict(at=time.time(),cursor=poller.cursor,scheduled_end=end))
        finally:obs.close()


def create_app(config,*,start=False):
    cfg=json.loads(Path(config).read_text());root=Path(cfg['root']);runtime=Path(cfg['runtime'])
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=1024
    @app.before_request
    def read_only():
        if request.path=='/health' and request.method=='GET':return None
        if request.method not in ('GET','HEAD'):abort(405)
        if not scoped_auth(os.environ.get('ICT_SHADOW_VIEW_TOKEN')):abort(401)
        if not request.path.startswith('/ict-shadow/shadow-mvp/'):abort(404)
    @app.get('/health')
    def health():return jsonify(service='agent50k-shadow-internal',orders=False,llm='PENDING_LLM')
    from services.tanja.ict_agent.shadow_views import register
    register(app,cfg['replay_db'],runtime)
    @app.after_request
    def redact(response):
        if response.is_json:
            def clean(x):
                if isinstance(x,dict):return {k:clean(v) for k,v in x.items() if not any(s in k.lower() for s in ('password','secret','api_key','directory','file_path','source_path','evidence_file')) and k not in ('pid','original_directory')}
                if isinstance(x,list):return [clean(v) for v in x]
                if isinstance(x,str) and x.startswith(('/private/','/Users/','/data/','/app/')):return '[internal location omitted]'
                return x
            response.set_data(json.dumps(clean(response.get_json())))
        response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff'
        return response
    if start:
        check_environment(os.environ);verify_release(cfg['release_manifest']);verify_release('/app/code-manifest.json')
        shutdown=threading.Event();app.extensions['shadow_shutdown']=shutdown
        def runner():
            try:run_observer(cfg,shutdown)
            except Exception as e:
                # Codes/types only; never exception messages containing credentials/paths.
                prior=json.loads((root/'status.json').read_text()) if (root/'status.json').exists() else {}
                code=str(e) if isinstance(e,ValueError) and str(e) in ('STORAGE_LOW_NO_DELETION','ACTIVATION_OR_CALENDAR_EXPIRED','SERVER_RUN_CONFIG_CHANGED','EXPLICIT_CURRENT_PASSIVE_ACTIVATION_REQUIRED','ACTIVATION_CONFIG_MISMATCH','ACTIVATION_RELEASE_MISMATCH','SOURCE_BINDING_CHANGED','SOURCE_REVISION_REQUIRES_REVIEW') else 'SAFE_LOCAL_GATE_FAILURE'
                prior.update(operational_state='BLOCKED_WORKER_FAILURE',error_code=code,error_category=type(e).__name__,heartbeat_at=time.time(),stop_at=0,feed_classification='FEED_UNVERIFIED',llm_status='PENDING_LLM',orders_enabled=False,paid_calls_enabled=False)
                atomic(root/'status.json',prior)
        thread=threading.Thread(target=runner,name='passive-observer',daemon=True);app.extensions['shadow_thread']=thread;thread.start()
    return app


def main():
    from werkzeug.serving import make_server,ThreadedWSGIServer
    # Bound concurrent dashboard clients; one observer, no per-HTTP-worker scans.
    class BoundedServer(ThreadedWSGIServer):
        slots=threading.BoundedSemaphore(4)
        def process_request(self,request,address):
            if not self.slots.acquire(False):request.close();return
            try:super().process_request(request,address)
            except BaseException:self.slots.release();raise
        def process_request_thread(self,request,address):
            try:super().process_request_thread(request,address)
            finally:self.slots.release()
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--activate',action='store_true');a=p.parse_args()
    check_environment(os.environ)
    app=create_app(a.config,start=a.activate)
    # Bounded subclass; no Flask debug/reloader, no multiprocess fork.
    server=BoundedServer('::',int(os.environ.get('PORT','8090')),app)
    def stop(*_):
        if 'shadow_shutdown' in app.extensions:app.extensions['shadow_shutdown'].set()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    server.serve_forever()
    if 'shadow_shutdown' in app.extensions:app.extensions['shadow_shutdown'].set()

if __name__=='__main__':main()
