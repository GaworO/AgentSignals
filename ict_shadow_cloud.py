"""Cloud-only research worker and private read-only dashboard. No broker imports.
Automatic ICT submission uses a separate default-off permission and shared guarded broker lifecycle.
Dollar budget applies only to this scanner; production's ledger is unchanged.
"""
import base64, collections, fcntl, hashlib, hmac, json, math, os, sqlite3, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
NY=ZoneInfo('America/New_York')
DAILY_USD=10.0
DAILY_CALLS=16

from ict_sessions import daily_call_limit, session, session_call_limit, session_budgets, review_pacing, SCHEDULE

MODEL='gpt-6.1-sol'

def cost(usage):
    """Official standard rates, cache writes replace base input rate; no tools."""
    if not isinstance(usage,dict):return None
    i,o=usage.get('input_tokens'),usage.get('output_tokens')
    detail=usage.get('input_tokens_details',{});c=detail.get('cached_tokens',0);w=detail.get('cache_write_tokens',0)
    if not all(type(v) is int and v>=0 for v in (i,o,c,w)) or c+w>i:return None
    return ((i-c-w)*2+c*.1+w*2.5)*(2 if i>272000 else 1)/1e6+o*10*(1.5 if i>272000 else 1)/1e6

def compact(packet):
    p=dict(packet);all_e=p['evidence'];keep={};groups=collections.defaultdict(list)
    for k,e in all_e.items():groups[(e.get('symbol'),e.get('kind'),e.get('timeframe'))].append((k,e))
    for (sym,kind,tf),items in groups.items():
        n=(32 if sym=='MNQ' or tf in (60,240) else 16) if kind=='bar' else 4
        if sym in ('NQ','ES') and kind=='bar' and tf in (2,3):n=0
        for k,e in sorted(items,key=lambda x:x[1].get('available_at',0))[-n:] if n else []:keep[k]=e
    # Retain all referenced evidence recursively, not orphaned ICT assertions.
    while True:
        refs={r for e in keep.values() for r in e.get('evidence_ids',[]) if r in all_e and r not in keep}
        if not refs:break
        for r in refs:keep[r]=all_e[r]
    p['evidence']=keep
    p['coverage']={'scope':'COMPACT_SUBSET_NOT_COMPLETE_MARKET_HISTORY','counts':dict(collections.Counter(str((e.get('symbol'),e.get('kind'),e.get('timeframe'))) for e in keep.values())),'omitted_items':len(all_e)-len(keep)}
    p['prior_shadow_ideas']=[{k:r.get(k) for k in ('id','requested_at')}|{'decision':{k:(r.get('decision') or {}).get(k) for k in ('decision','summary','watch_condition','plan')},'broker_fill':'NONE_SHADOW_ONLY'} for r in p.get('prior_shadow_ideas',[])[-3:]]
    p['trigger_events']=[e for e in p.get('trigger_events',[]) if all(r in keep for r in e.get('evidence_ids',[]))][-8:]
    p['missing_context']=list(p.get('missing_context',[]))+['Older low-timeframe candles omitted to bound cost; absence is UNKNOWN, not absence of a pattern.']
    return p


def dashboard_analytics(costs, reviews, now):
    """Read-only NY-calendar aggregates. Fixed one-MNQ gross simulations, not fills."""
    day=datetime.fromtimestamp(now,NY).date()
    starts={'today':day,'week':day-timedelta(days=day.weekday()),'month':day.replace(day=1),'all':None}
    result={}
    for period,start in starts.items():
        def included(d):return (start is None or d>=start.isoformat()) and d<=day.isoformat()
        rows=[c for c in costs if included(c['day'])]
        selected=[r for r in reviews if r.get('requested_at') and included(datetime.fromtimestamp(r['requested_at'],NY).date().isoformat())]
        proposals=[r for r in selected if (r.get('decision') or {}).get('plan')]
        valid=[r for r in proposals if (r.get('validation') or {}).get('state')=='VALIDATED_SHADOW_PROPOSAL']
        counts=collections.Counter((r.get('outcome') or {}).get('state','UNKNOWN') for r in valid)
        wins=[r for r in valid if (r.get('outcome') or {}).get('state')=='SIMULATED_TARGET']
        losses=[r for r in valid if (r.get('outcome') or {}).get('state')=='SIMULATED_STOP']
        def pnl(r):
            value=r['outcome'].get('gross_usd_per_mnq')
            return value if isinstance(value,(int,float)) and math.isfinite(value) else None
        values=[pnl(r) for r in wins+losses];complete=all(v is not None for v in values)
        profit=sum(v for v in values if v is not None and v>0);loss=-sum(v for v in values if v is not None and v<0)
        known={'SIMULATED_TARGET','SIMULATED_STOP','SIMULATED_NO_FILL','SIMULATED_OPEN','SIMULATED_WAITING','SIMULATED_INVALIDATED_BEFORE_FILL'}
        result[period]={
            'from':start.isoformat() if start else None,'through':day.isoformat(),
            'calls':len(rows),'estimated_usd':sum(c['amount'] for c in rows if c['amount'] is not None),
            'reserved_usd':sum(c['reserve'] for c in rows if c['amount'] is None),
            'unknown_cost_attempts':sum(c['amount'] is None for c in rows),
            'reviews':len(selected),'proposals':len(proposals),'validated':len(valid),'rejected':len(proposals)-len(valid),
            'wins':len(wins),'losses':len(losses),'no_fill':counts['SIMULATED_NO_FILL'],
            'open':counts['SIMULATED_OPEN'],'pending':counts['SIMULATED_WAITING'],
            'invalidated':counts['SIMULATED_INVALIDATED_BEFORE_FILL'],
            'uncertain':sum(n for k,n in counts.items() if k not in known),
            'gross_pnl_per_mnq':profit-loss if values and complete else None,
            'profit_factor':profit/loss if loss and complete else None,
            'pf_state':'AVAILABLE' if loss and complete else 'INCOMPLETE_PNL' if not complete else 'NO_LOSSES' if wins else 'NO_CLOSED_TRADES',
            'win_rate':len(wins)/len(values) if values else None,
        }
    return {'periods':result,'basis':'SIMULATED_GROSS_USD_PER_1_MNQ','period_basis':'PROPOSAL_DATE_AMERICA_NEW_YORK','net_pnl':'UNKNOWN_FEES_AND_SLIPPAGE','independent_proposals':True}

class Cloud:
    def __init__(self,config):
        self._wake=threading.Event();self._worker_lock=threading.Lock();self._worker_thread=None
        self.config=config;self.root=Path(config.get('DATA_DIR','/data/builder50'))/'research';self.root.mkdir(parents=True,exist_ok=True)
        self.dbpath=self.root/'ict_cloud.sqlite3'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS costs(id TEXT PRIMARY KEY, day TEXT, at REAL, reserve REAL, amount REAL, state TEXT);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS candles(symbol TEXT,time INTEGER,body TEXT,PRIMARY KEY(symbol,time));''')
        from ict_order_handoff import Handoff
        self.handoff=Handoff(self.root,self.config)
        self.import_costs()
        history=Path(__file__).with_name('ict_shadow_history_20261030.json')
        if history.exists():
            with self.db() as db:
                for b in json.loads(history.read_text())['candles']:
                    db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)',(b['symbol'],b['time'],json.dumps(b)))
    def db(self):
        d=sqlite3.connect(self.dbpath,timeout=20);d.row_factory=sqlite3.Row;return d
    def put(self,key,value):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)))
    def import_costs(self):
        # Import saved trial usage once; never reset or forgive uncertain attempts.
        for path in self.root.glob('ict_2022_nyam_*/ICT_INDEPENDENT_NYAM_*.json'):
            r=json.loads(path.read_text());raw=r.get('response') or {};amount=cost(raw.get('usage'));day=r['id'].split('_')[-2]
            day=f'{day[:4]}-{day[4:6]}-{day[6:]}'
            with self.db() as db:db.execute('INSERT OR IGNORE INTO costs VALUES(?,?,?,?,?,?)',(r['id'],day,r.get('requested_at',0),3.2,amount,'ESTIMATED' if amount is not None else 'UNKNOWN_RESERVED'))
    def reserve(self,rid,payload,now):
        if payload['model']!=MODEL or payload.get('service_tier')!='default':raise ValueError('UNPRICED_MODEL_OR_TIER')
        # UTF-8 byte count plus conservative schema/message overhead bounds tokens.
        upper=len(json.dumps(payload,ensure_ascii=True).encode())+2048
        if upper>272000:raise ValueError('PACKET_COST_BOUND_EXCEEDED')
        reserve=(upper*2.5+payload['max_output_tokens']*10)/1e6*1.1
        day=datetime.fromtimestamp(now,NY).date().isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            total=db.execute('SELECT COALESCE(SUM(COALESCE(amount,reserve)),0) FROM costs WHERE day=?',(day,)).fetchone()[0]
            if total+reserve>DAILY_USD:raise ValueError('DAILY_DOLLAR_CAP')
            if db.execute('SELECT 1 FROM costs WHERE id=?',(rid,)).fetchone():raise ValueError('COST_RESERVED_NO_RETRY')
            window=session(now)
            if window is None:raise ValueError('SESSION_ENDED')
            if db.execute('SELECT COUNT(*) FROM costs WHERE day=?',(day,)).fetchone()[0]>=daily_call_limit(day):raise ValueError('DAILY_CALL_CAP')
            if db.execute('SELECT COUNT(*) FROM costs WHERE day=? AND at>=? AND at<?',(day,window[2],window[1])).fetchone()[0]>=session_call_limit(window[0]):raise ValueError('SESSION_CALL_CAP')
            db.execute('INSERT INTO costs VALUES(?,?,?,?,NULL,?)',(rid,day,now,reserve,'RESERVED'))
    def finish(self,rid,raw,error):
        amount=cost((raw or {}).get('usage'))
        # 10% pricing cushion; displayed estimate includes it, not invoice actual.
        if amount is not None:amount*=1.1
        with self.db() as db:
            row=db.execute('SELECT reserve FROM costs WHERE id=?',(rid,)).fetchone()
            if row is None:raise ValueError('COST_RESERVATION_MISSING')
            if amount is not None and amount>row['reserve']:db.execute("INSERT OR REPLACE INTO state VALUES('budget_anomaly','true')")
            db.execute('UPDATE costs SET amount=?,state=? WHERE id=?',(amount,'ESTIMATED_WITH_CUSHION' if amount is not None else 'UNKNOWN_RESERVED',rid))
    def prepare(self,p,now):
        s=session(now)
        if s is None:raise ValueError('SESSION_ENDED')
        from ict_execution import VERSION,IFVG_VERSION,ifvg_permission
        import ict_news_context
        p=compact(p);p.update(cloud_shadow=True,review_families=['ICT2022','IFVG'],session_name=s[0],session_end=s[1],execution_contract=VERSION)
        p['scheduled_news']=ict_news_context.build(self.config,now)
        self.put('scheduled_news',p['scheduled_news'])
        if ifvg_permission(now=now) is None:p['ifvg_execution_contract']=IFVG_VERSION
        return p
    def entry_events(self,p):
        # Wake AI when an entry gap completes, without selecting a trade.
        events=[]
        for tf in (1,2,3,5,15):
            bars=sorted([(k,e) for k,e in p['evidence'].items() if e.get('kind')=='bar' and e.get('symbol')=='MNQ' and e.get('timeframe')==tf],key=lambda x:x[1]['time'])
            for i in range(2,len(bars)):
                trio=bars[i-2:i+1];a,b,c=[v for _,v in trio]
                if b['time']-a['time']!=tf*60 or c['time']-b['time']!=tf*60:continue
                direction='LONG' if c['low']>a['high'] else 'SHORT' if c['high']<a['low'] else None
                if direction:events.append(dict(kind='OBSERVED_FVG_FORMATION',symbol='MNQ',timeframe=tf,direction=direction,available_at=c['available_at'],evidence_ids=[k for k,_ in trio],interpretation='Candidate observation only; AI must assess its significance.'))
                if direction:
                    # Candidate inversion observations only. Same-timeframe close,
                    # never a wick or a future/incomplete candle.
                    low,high=(a['high'],c['low']) if direction=='LONG' else (c['high'],a['low'])
                    for key,inv in bars[i+1:]:
                        if inv.get('available_at',math.inf)>p['as_of']:continue
                        crossed=inv['close']<low if direction=='LONG' else inv['close']>high
                        if crossed:
                            events.append(dict(kind='OBSERVED_IFVG_INVERSION',symbol='MNQ',timeframe=tf,direction='SHORT' if direction=='LONG' else 'LONG',available_at=inv['available_at'],evidence_ids=[k for k,_ in trio]+[key],interpretation='Inversion close observed; retest/ICT validity requires AI review. IFVG review-only.'))
                            break
        return events
    def select(self,rows,p,now):
        s=session(now)
        if not s:return None,'OUTSIDE_SESSION',None
        if any(r['status'] in ('research_inflight','research_unknown') for r in rows):return None,'UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION',None
        limit=daily_call_limit(datetime.fromtimestamp(now,NY).date().isoformat())
        if len(rows)>=limit:return None,'DAILY_CALL_CAP',None
        import ict_news_context
        news=ict_news_context.build(self.config,now)
        self.put('scheduled_news',news)
        if news['status']!='CLEAR':return None,'ICT_NEWS_'+news['status'],None
        # Include reservations burned before the review row was persisted.
        # IDs remain stable for existing journals and execution bindings.
        with self.db() as db:
            reserved_rows=[dict(r) for r in db.execute('SELECT id,at FROM costs WHERE day=?',(datetime.fromtimestamp(now,NY).date().isoformat(),))]
        attempts={r['id']:dict(r) for r in rows}
        for r in reserved_rows:
            attempts.setdefault(r['id'],dict(id=r['id'],requested_at=r['at']))
        in_session=[r for r in attempts.values() if s[2]<=r['requested_at']<s[1]]
        if len(in_session)>=session_call_limit(s[0]):return None,'SESSION_CALL_CAP',None
        if sum(r['requested_at']>now-1800 for r in in_session)>=3:return None,'THREE_REVIEWS_PER_30_MINUTES',None
        last=max((r['requested_at'] or 0 for r in attempts.values()),default=0)
        if now-last<90:return None,'90_SECOND_COOLDOWN',None
        # Never reuse a burned cost reservation after a crash before research INSERT.
        with self.db() as db:
            reserved=[r['id'] for r in db.execute('SELECT id FROM costs WHERE day=?',(datetime.fromtimestamp(now,NY).date().isoformat(),))]
        next_slot=max([int(r['id'].rsplit('_',1)[1]) for r in rows]+[int(r.rsplit('_',1)[1]) for r in reserved]+[-1])+1
        if next_slot>=limit:return None,'DAILY_CALL_CAP_INCLUDING_BURNED',None
        if not review_pacing(now, list(attempts.values()))['ready']:
            return None,'SESSION_REVIEW_PACING',None
        if p['as_of']<s[2]+60:return None,'WAIT_FIRST_CLOSED_CANDLE',None
        if not any(r['requested_at']>=s[2] for r in rows):return next_slot,'CLOUD_'+s[0]+'_OPENING',None
        fresh=[e for e in p.get('trigger_events',[]) if last<e['available_at']<=p['as_of'] and p['as_of']-e['available_at']<=120]
        if fresh:return next_slot,'CLOUD_STRUCTURE_EVENT',max(fresh,key=lambda x:x['available_at'])
        if now-last>=1200:return next_slot,'CLOUD_20_MINUTE_REASSESSMENT',None
        return None,'WATCHING_CANDLES_NO_NEW_EVENT',None
    def observe(self,p):
        with self.db() as db:
            for e in p['evidence'].values():
                if e.get('kind')=='bar' and e.get('timeframe')==1:
                    db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)',(e['symbol'],e['time'],json.dumps(e)))
        self.put('market',{'as_of':p['as_of'],'observed_at':time.time(),'evidence_count':len(p['evidence']),'contract':p.get('contract_configured'),'quote':'CLOSED_CANDLES_NOT_BBO'})
    def tick(self):
        import ict_shadow_research as research
        now=time.time();day=datetime.fromtimestamp(now,NY).date().isoformat()
        research.DAY=day;research.PREFIX='ICT_INDEPENDENT_NYAM_'+day.replace('-','')+'_'
        self.put('worker',{'at':now,'host':'Railway','commit':self.config.get('RAILWAY_GIT_COMMIT_SHA'),'enabled':self.config.get('ICT_SHADOW_ENABLED')=='true','session':session(now)})
        if self.config.get('ICT_SHADOW_ENABLED')!='true':self.put('last_check',{'at':now,'state':'SCANNER_DISABLED'});return
        if not session(now):
            self.handoff.refresh(self.records(),{},now)
            self.put('last_check',{'at':now,'state':'OUTSIDE_SESSION'});return
        with self.db() as db:
            if db.execute("SELECT 1 FROM state WHERE key='budget_anomaly'").fetchone():raise ValueError('COST_RECONCILIATION_REQUIRED')
        # Source validation (calendar/freshness/closed bars) is shared with production.
        obs=research.remote_run(0,True);self.observe(obs['packet'])
        self.handoff.refresh(self.records(),obs['packet'],time.time())
        import ict_news_context
        news=ict_news_context.build(self.config,time.time());self.put('scheduled_news',news)
        if news['status']!='CLEAR':
            self.put('last_check',{'at':time.time(),'state':'ICT_NEWS_'+news['status'],'reason':news.get('reason')});return
        # Production receives priority; no production changes or setup queue writes.
        with sqlite3.connect('file:'+str(self.root.parent/'ab_ai_context.sqlite3')+'?mode=ro',uri=True) as db:
            pending=db.execute("SELECT COUNT(*) FROM setup_requests WHERE state IN ('queued','running') AND expires>?",(now,)).fetchone()[0]
        if pending:self.put('last_check',{'at':now,'state':'DEFER_PRODUCTION_SETUP_QUEUE'});return
        result=research.remote_run(None,cloud=self)
        self.handoff.refresh(self.records(),obs['packet'],time.time())
        self.put('last_check',{'at':time.time(),'state':result['state'],'review_id':result.get('id')})
    def records(self):
        records=[]
        for p in sorted(self.root.glob('ict_2022_nyam_*/ICT_INDEPENDENT_NYAM_*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            try:records.append(json.loads(p.read_text()))
            except (OSError,ValueError):continue
        return sorted(records,key=lambda r:r.get('requested_at',0),reverse=True)
    def state(self):
        now=time.time();day=datetime.fromtimestamp(now,NY).date().isoformat()
        with self.db() as db:
            state={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM state')}
            all_costs=[dict(r) for r in db.execute('SELECT * FROM costs')]
            costs=[r for r in all_costs if r['day']==day]
        with self.db() as db:state['candles']=[json.loads(r[0]) for r in db.execute("SELECT body FROM candles WHERE symbol='MNQ' ORDER BY time DESC LIMIT 90")][::-1]
        state.update(now=now,day_ny=day,execution_authorized=False,daily_usd_cap=DAILY_USD,estimated_or_reserved_usd=sum(r['amount'] if r['amount'] is not None else r['reserve'] for r in costs),calls_today=len(costs),daily_call_cap=daily_call_limit(day),costs=costs,budget_scope='THIS_SHADOW_SCANNER_ONLY; production billed separately. USD before tax; not a guaranteed EUR invoice cap.',sessions=SCHEDULE,session_budgets=session_budgets(day,costs),reviews=[])
        state['review_pacing'] = review_pacing(now, costs)
        for r in self.records():
            state['reviews'].append({k:r.get(k) for k in ('id','state','decision','validation','requested_at','received_at','finished_at','latency_seconds','error')}|{'purpose':r.get('packet',{}).get('assessment_kind'),'market_as_of':r.get('packet',{}).get('as_of'),'budget':r.get('budget_before',{}),'outcome':self.outcome(r)})
        companions=[]
        for r in state['reviews']:
            r['family']='ICT2022'
            d=(r.get('decision') or {}).get('ifvg_review')
            if d is None:continue
            iv=(r.get('validation') or {}).get('ifvg_validation',{'state':'BLOCKED','reason':'IFVG_VALIDATION_MISSING'})
            child=dict(r,id=r['id']+':IFVG',parent_review_id=r['id'],family='IFVG',decision=d,validation=iv,execution_route='SHADOW_ONLY')
            child['outcome']=self.outcome(child)
            companions.append(child)
        state['reviews']+=companions
        state['reviews'].sort(key=lambda r:r.get('requested_at') or 0,reverse=True)
        state['review_families']=['ICT2022','IFVG']
        import ict_execution
        state['ifvg_execution']='GUARDED_AUTOMATIC' if ict_execution.ifvg_permission(now=now) is None else 'SHADOW_ONLY'
        state['ifvg_runtime']=ict_execution.status()['ifvg']
        for child in companions:child['execution_route']=state['ifvg_execution']
        state['analytics']=dashboard_analytics(all_costs,state['reviews'],now)
        from ict_order_handoff import broker_status
        state['order_handoff']=self.handoff.state()
        state['order_handoff']['broker']=broker_status()
        # Display-only: evaluate the saved calendar at request time, including
        # outside sessions when the worker's last news snapshot may be old.
        import ict_news_context, ict_ai_call_status
        state['scheduled_news']=ict_news_context.build(self.config,now)
        state['ai_call_status']=ict_ai_call_status.describe(state,state['scheduled_news'],self.config.get('ICT_SHADOW_ENABLED')=='true')
        return state
    def outcome(self,r):
        v=r.get('validation') or {}
        if v.get('state')!='VALIDATED_SHADOW_PROPOSAL':return {'state':'NO_VALIDATED_PROPOSAL'}
        start=math.ceil(v['activation_at']/60)*60;expiry=v['expires_at'];long=(r['decision']['plan']['direction']=='LONG');entry,sl,tp=v['entry'],v['stop'],v['target']
        with self.db() as db:bars=[json.loads(x[0]) for x in db.execute('SELECT body FROM candles WHERE symbol=? AND time>=? ORDER BY time',('MNQ',start))]
        filled=False;previous=start-60
        for b in bars:
            if not filled and b['time']>=expiry:return {'state':'SIMULATED_NO_FILL','net_pnl':'UNKNOWN'}
            if b['time']!=previous+60:return {'state':'UNKNOWN_CANDLE_GAP'}
            previous=b['time'];hit=(b['low']<=entry-.25 if long else b['high']>=entry+.25)
            stopped=b['low']<=sl if long else b['high']>=sl;target=b['high']>=tp if long else b['low']<=tp
            if not filled:
                if stopped or target:return {'state':'AMBIGUOUS_ENTRY_BAR' if hit else 'SIMULATED_INVALIDATED_BEFORE_FILL'}
                if hit:filled=True
                continue
            if stopped and target:return {'state':'AMBIGUOUS_SL_TP_SAME_BAR'}
            if stopped or target:return {'state':'SIMULATED_STOP' if stopped else 'SIMULATED_TARGET','gross_usd_per_mnq':(-1 if stopped else v['gross_rr'])*v['risk_usd_per_mnq'],'net_pnl':'UNKNOWN_FEES_SLIPPAGE','freeform_invalidation':'NOT_SIMULATED'}
        return {'state':'SIMULATED_OPEN' if filled else 'UNKNOWN_INCOMPLETE_COVERAGE' if time.time()>expiry else 'SIMULATED_WAITING','freeform_invalidation':'NOT_SIMULATED'}
    def start_worker(self):
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._wake.set();return 'CHECK_REQUESTED'
            self._worker_thread=threading.Thread(target=self.run,daemon=True,name='ict-shadow-cloud')
            self._worker_thread.start()
            return 'WORKER_STARTED'
    def pause(self):
        self._wake.wait(30);self._wake.clear()
    def run(self):
        # Shared volume flock prevents multiple Gunicorn/deployment workers calling AI.
        while True:
            with (self.root/'ict_cloud.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:self.pause();continue
                try:self.tick()
                except Exception as e:self.put('last_check',{'at':time.time(),'state':'BLOCKED','reason':type(e).__name__+':'+str(e)[:120]})
                self.pause()

def register(app,config=None,start=True):
    from flask import Response, jsonify, request
    cloud=Cloud(config or os.environ)
    def authorized():
        password=cloud.config.get('AB_CONTEXT_SOURCE_PASSWORD') or cloud.config.get('TANJA_DASHBOARD_PASSWORD','')
        expected='Basic '+base64.b64encode(('tanja:'+password).encode()).decode()
        return bool(password) and hmac.compare_digest(request.headers.get('Authorization',''),expected)
    def private(fn):
        def wrapped(*args,**kwargs):
            if not authorized():return Response('Sign in with your existing Tanja dashboard login.',401,{'WWW-Authenticate':'Basic realm="ICT Shadow"'})
            r=fn(*args,**kwargs);r.headers['Cache-Control']='no-store';r.headers['X-Content-Type-Options']='nosniff';r.headers['X-Frame-Options']='DENY';return r
        return wrapped
    app.add_url_rule('/ict-shadow','ict_shadow_view',private(lambda:Response(Path(__file__).with_name('ict_shadow.html').read_text(),mimetype='text/html')))
    app.add_url_rule('/ict-shadow/state','ict_shadow_state',private(lambda:jsonify(cloud.state())))
    app.add_url_rule('/ict-shadow/ifvg-examples','ict_ifvg_examples',private(lambda:Response(Path(__file__).with_name('ict_ifvg_examples.html').read_text(),mimetype='text/html')))
    def detail(rid):
        r=next((r for r in cloud.records() if r['id']==rid),None)
        return jsonify({k:v for k,v in r.items() if k not in ('flags','original_sse')} if r else {'error':'not_found'})
    app.add_url_rule('/ict-shadow/reviews/<rid>','ict_shadow_detail',private(detail))
    def history():
        import ict_history
        try:return jsonify(ict_history.collect(cloud,request.args.get('period','day'),request.args.get('date',datetime.now(NY).date().isoformat()),request.args.get('session','ALL')))
        except ValueError as e:
            response=jsonify(error=str(e));response.status_code=400;return response
    app.add_url_rule('/ict-shadow/history','ict_shadow_history',private(history))
    import ict_scanner
    ict_scanner.register(app,private,cloud)
    import ict_risk_renewal_web
    ict_risk_renewal_web.register(app,private,cloud.config)
    import ict_ai_recovery_web
    ict_ai_recovery_web.register(app,private,cloud,allow_worker_start=start)
    if start:cloud.start_worker()
    return cloud
