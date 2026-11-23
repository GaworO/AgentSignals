"""Agent50k mount: existing user auth + narrow read-only internal candle export.
No worker, Knowledge, trading configuration, account DB, order or LLM import.
"""
import hmac,json,re,sqlite3,time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from flask import Response,request,jsonify,abort
from .shadow_transport import PrivateReader,ReadFailure

FIELDS=('symbol','ticker','confirmed','standard','bar_open_ms','bar_close_ms','open','high','low','close')
PATH=re.compile(r'(?:status|feed-status|prospective-status|candidates|candidates/[a-f0-9]{24}|events|events/[a-f0-9]{24}|evidence/[a-f0-9]{64}|research/(?:summary|sources|m1-review|source/[A-Za-z0-9_-]+|models|candidates|candidates/[a-f0-9]{24}|prospective-report|prospective-current|occurrences|assets/app\.js)|episode6/(?:summary|model|cases|cases/[a-f0-9]{24}|assets/app\.js)|assets/app\.js)\Z')
ARGS={'run','offset','symbol','session','at','family','kind','model','status','limit','classification','condition'}

def scoped_auth(token):
    return isinstance(token,str) and len(token)>=32 and hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token)

def export_candles(database,after,now,limit=300):
    rows=[];bindings={};health={}
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for key,raw in db.execute("SELECT key,value FROM metadata WHERE key IN ('tickers','collection_tickers')"):
            bindings.update({k:v for k,v in json.loads(raw).items() if k in ('NQ','MNQ','ES')})
        for symbol,table in [('NQ','collection_bars'),('MNQ','bars'),('ES','bars')]:
            # Fixed common time-page prevents advancing past a missing counterpart.
            found=db.execute('SELECT symbol,start,end,received,payload FROM '+table+' WHERE symbol=? AND start>? AND start<=? AND received<=? AND end<=? ORDER BY start LIMIT ?', (symbol,after,after+limit*60,now,now,limit)).fetchall()
            for s,start,end,received,raw in found:
                p=json.loads(raw);clean={k:p[k] for k in FIELDS if k in p}
                rows.append([s,start,end,received,json.dumps(clean,sort_keys=True,separators=(',',':'))])
            newest=db.execute('SELECT end,received FROM '+table+' WHERE symbol=? AND end<=? AND received<=? ORDER BY start DESC LIMIT 1',(symbol,now,now)).fetchone()
            health[symbol]={'latest_close':newest[0],'latest_received':newest[1]} if newest else None
    return {'captured_at':now,'read_only':True,'bindings':bindings,'rows':rows,'feed_health':health,'page_end':min(after+limit*60,int(now//60)*60-60)}

def register(app,cfg,authorize,*,reader=None):
    if app.extensions.get('ict_shadow_gateway'):return
    app.extensions['ict_shadow_gateway']=True
    enabled=cfg.get('ICT_SHADOW_UI_ENABLED','false').lower()=='true'
    source=Path(cfg.get('DATA_DIR','/data/builder50'))/'market_context/tanja.sqlite3'
    @app.get('/internal/ict-shadow/candles')
    def shadow_intake_readonly():
        if cfg.get('ICT_SHADOW_EXPORT_ENABLED','false').lower()!='true':abort(404)
        if not scoped_auth(cfg.get('ICT_SHADOW_FEED_TOKEN')):abort(401)
        if set(request.args)!={'after'}:abort(400)
        try:after=int(request.args['after'])
        except ValueError:abort(400)
        now=time.time()
        if after<0 or after>now or after%60:abort(400)
        try:r=jsonify(export_candles(source,after,now))
        except (OSError,sqlite3.Error,ValueError,KeyError):return jsonify(error='INTAKE_READ_UNAVAILABLE'),503
        r.headers['Cache-Control']='no-store';return r
    @app.get('/ict-shadow/shadow-assets/<name>')
    def shadow_native_asset(name):
        if not authorize():return Response('Authentication required',401,{'WWW-Authenticate':'Basic realm="ICT Shadow"'})
        if name!='shadow-native.css':abort(404)
        return Response(Path(__file__).with_name('static').joinpath(name).read_text(),mimetype='text/css')
    @app.get('/ict-shadow/shadow-mvp/<path:tail>')
    def shadow_read_proxy(tail):
        if not authorize():return Response('Authentication required',401,{'WWW-Authenticate':'Basic realm="ICT Shadow"'})
        if not enabled:return jsonify(error='SHADOW_NOT_ACTIVATED',llm_status='PENDING_LLM'),503
        if not PATH.fullmatch(tail) or set(request.args)-ARGS or any(len(v)>100 or re.search(r'[^A-Za-z0-9_.:-]',v) for v in request.args.values()):abort(400)
        try:
            client=reader or PrivateReader(cfg['ICT_SHADOW_WORKER_ORIGIN'],cfg['ICT_SHADOW_VIEW_TOKEN'])
            data,content=client.get('/ict-shadow/shadow-mvp/'+tail+('?' + urlencode(request.args) if request.args else ''))
            r=Response(data,mimetype='text/javascript' if tail.endswith('.js') else 'application/json')
        except (ReadFailure,KeyError,ValueError):r=jsonify(error='SHADOW_WORKER_UNAVAILABLE',llm_status='PENDING_LLM');r.status_code=503
        r.headers['Cache-Control']='no-store';r.headers['X-Content-Type-Options']='nosniff';return r
