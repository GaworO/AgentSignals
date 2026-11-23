"""Local native dashboard extension; GET-only and no trading workers."""
import json,time
from pathlib import Path
from flask import Flask, abort, jsonify, request, Response
from .store import Store
from .shadow_mvp import summary

PANEL='''<section class="panel" id="shadow-mvp-panel"><h2>ICT Shadow · dziennik obserwacji</h2>
<p class="notice" id="shadow-mode-notice"><b>SIMULATED_REPLAY · PENDING_LLM · ZLECENIA WYŁĄCZONE</b><br>
Rzeczywiste archiwalne świece odtwarzane chronologicznie. Detektor i pobrane źródła nie oznaczają kwalifikacji setupu.
Brak zweryfikowanego live feedu, brak wywołań AI i brak symulowanych realizacji.</p>
<p><a href="/ict-shadow?view=ictai&shadow_run=prospective">Obserwacja prospektywna</a> · <a href="/ict-shadow?view=ictai&ai_mode=replay">Archiwalny replay</a> · <a href="/ict-shadow?view=ictai&shadow_run=research">Częstotliwość strategii i PF</a> · <a href="/ict-shadow?view=ictai&shadow_run=episode6">Episode 6 — kwalifikacja</a></p>
<p id="shadow-feed"></p><div id="shadow-prospective"></div><p id="shadow-mvp-status">Odczyt dziennika…</p>
<p>Wersja wykonawcza: odzyskany commit fafc627 + lokalny łącznik shadow. Późniejsze niezacommitowane badania nie zostały odtworzone.</p>
<p><label>Instrument <select id="shadow-symbol"><option value="">Wszystkie</option><option>NQ</option><option>MNQ</option><option>ES</option></select></label> <label>Sesja <select id="shadow-session"><option value="">Wszystkie</option><option>LO</option><option>NYAM</option><option>NYPM</option><option>BETWEEN_WINDOWS</option></select></label> <button id="shadow-mvp-refresh">Odśwież</button> <button id="shadow-mvp-more">Następna strona</button></p>
<div style="overflow:auto;max-height:350px"><table><thead><tr><th>Czas NY</th><th>Instrument</th><th>Kierunek</th><th>Wykryta geometria</th><th>Decyzja shadow</th><th>Interpretacja</th></tr></thead><tbody id="shadow-mvp-candidates"></tbody></table></div>
<div id="shadow-mvp-detail"></div>
<p><b>Widok techniczny poniżej:</b> zachowany dziennik i wykresy poprzedniego silnika.
Jego etykiety TRADE_CANDIDATE / NO_TRADE są heurystykami deterministycznymi, nie decyzjami LLM ani kwalifikacją źródłową.
Obowiązują stany shadow z powyższej tabeli. Nie symulowano realizacji zleceń.</p></section>'''

JS=r'''(()=>{
const base='/ict-shadow/shadow-mvp';let offset=0;
const $=s=>document.querySelector(s), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time=t=>new Date(t*1000).toLocaleString('en-GB',{timeZone:'America/New_York'});
async function get(p){let r=await fetch(base+p);if(!r.ok)throw Error('HTTP '+r.status);return r.json()}
function sourceLinks(refs){return refs.map(r=>{try{const u=new URL(r.url);if(u.protocol!=='https:'||!['www.youtube.com','youtube.com'].includes(u.hostname))return '';return `<a target="_blank" rel="noopener" href="${esc(u.href)}">${esc(r.video_id)} · ${esc(u.searchParams.get('t'))}</a>`}catch{return ''}}).join(' · ')}
async function detail(id){const d=await get('/candidates/'+encodeURIComponent(id));const a=d.latest;
$('#shadow-mvp-detail').innerHTML=`<h3>${esc(a.symbol)} ${esc(a.direction)} · ${esc(a.decision)}</h3><p>${esc(id)} · ${esc(time(a.as_of))} NY</p>
<p><b>Model:</b> ${esc(a.model)} — hipoteza badawcza, nie kompletna strategia źródłowa.</p>
<p><b>Brakuje:</b> ${esc(a.missing_requirements.join(', '))}</p><p><b>Następne potwierdzenie:</b> ${esc(a.next_confirmation.join(', ')||'Interpretacja kontekstu i warunków źródłowych')}</p>
<p><b>Warunek podważenia odwrócenia:</b> zamknięcie późniejszej świecy M1 poza ekstremum ${esc(a.invalidation.level)}. To warunek hipotezy badawczej.</p>
<p>Źródłowo zatwierdzone wejście / SL / TP: <b>nieustalone</b>. Wykonanie: BLOCKED_RESEARCH_ONLY. LLM: PENDING_LLM.</p>
<h4>Przebieg i późniejsze obserwacje</h4><table><thead><tr><th>NY</th><th>Etap</th><th>Stan / przyczyna</th></tr></thead><tbody>${d.records.map(r=>`<tr><td>${esc(time(r.as_of))}</td><td>${esc(r.stage)}</td><td>${esc(r.decision||r.status||r.context_status||'')}${r.added_confirmations?.length?' · '+esc(r.added_confirmations.join(', ')):''}${r.reason?' · '+esc(r.reason):''}</td></tr>`).join('')}</tbody></table>
<h4>Dowody dostępne przy ostatniej ocenie</h4><p>${d.evidence_count} rekordów ze zweryfikowanymi czasami dostępności. ${a.evidence_ids.map(esc).join(' · ')}</p>
<details><summary>Kontekst TF i konkurujące zakresy — bez wyboru biasu</summary><pre>${esc(JSON.stringify(d.context,null,2))}</pre></details>
<h4>Przywołane reguły</h4><p>RELEVANT_ONLY — poprawność zastosowania do rynku pozostaje nierozstrzygnięta.</p>
${d.rules.map(r=>`<details><summary>${esc(r.id)} · ${esc(r.model)}</summary><p>${esc(r.description||'')}</p><pre>${esc(JSON.stringify(r.fields,null,2))}</pre><p>Wyjątki: ${esc(JSON.stringify(r.exceptions))}</p>${sourceLinks(r.references||[])}</details>`).join('')}
<p><a target="_blank" href="${base}/evidence/${esc(a.evidence_packet_id)}">Dokładny pakiet dowodów dostępny dla LLM — bez przyszłych wyników</a></p>`;
}
async function load(){try{const [s,c]=await Promise.all([get('/status'),get('/candidates?offset='+offset)]);
$('#shadow-mvp-status').textContent=`${s.session} · zegar replay ${time(s.current_as_of)} NY · kandydaci ${s.candidates} · decyzje ${JSON.stringify(s.decisions)} · zapisy wyników obserwacji ${s.outcome_records} · kwalifikacje źródłowe ${s.source_qualified} · LLM ${s.openai_calls} · zlecenia ${s.broker_orders}`;
$('#shadow-mvp-candidates').innerHTML=c.items.map(a=>`<tr><td>${esc(time(a.created_at))}</td><td>${esc(a.symbol)}</td><td>${esc(a.direction)}</td><td><button data-candidate="${esc(a.candidate_id)}">${esc(a.observed_sequence.join(' → ')||'SWEEP')}</button></td><td>${esc(a.decision)}</td><td>PENDING_LLM</td></tr>`).join('');
$('#shadow-mvp-candidates').querySelectorAll('button').forEach(b=>b.onclick=()=>detail(b.dataset.candidate).catch(fail));
$('#shadow-mvp-more').disabled=!c.has_more;
}catch(e){fail(e)}}
function fail(e){$('#shadow-mvp-status').textContent='VALIDATION / READ FAILURE: '+e.message}
$('#shadow-mvp-refresh').onclick=()=>{offset=0;load()};$('#shadow-mvp-more').onclick=()=>{offset+=50;load()};load();
})();'''

def create_app(database, library, runtime):
    import base64
    from .strategy_research_web import register as register_research, PANEL as RESEARCH_PANEL
    from .episode6_web import register as register_episode6, PANEL as EPISODE6_PANEL
    import dashboard,ict_ai_trader,ict_shadow_cloud
    app=Flask(__name__);runtime=Path(runtime);runtime.mkdir(parents=True,exist_ok=True)
    store=object.__new__(Store);store.path=Path(database).resolve()
    if not store.path.is_file():raise ValueError('SHADOW_SESSION_REQUIRED')
    def prospective_paths():
        config=runtime/'prospective-run.json'
        if not config.is_file():return None
        root=Path(json.loads(config.read_text())['directory']).resolve()
        if not root.is_relative_to(runtime.resolve().parent):raise ValueError('LOCAL_RUN_OUTSIDE_WORKSPACE')
        return root
    def selected_store():
        if request.args.get('run')!='prospective':return store
        root=prospective_paths()
        if root is None or not (root/'observer.sqlite3').is_file():abort(503)
        s=object.__new__(Store);s.path=root/'observer.sqlite3';return s
    def operational_status():
        root=prospective_paths()
        if root and (root/'status.json').exists():
            p=json.loads((root/'status.json').read_text());p.pop('pid',None)
            p['enabled']=p['operational_state']=='PROSPECTIVE_SHADOW_RUNNING' and time.time()-p['heartbeat_at']<75 and time.time()<p['stop_at']
            if p['operational_state']=='PROSPECTIVE_SHADOW_RUNNING' and not p['enabled']:p['operational_state']='BLOCKED_STALE_HEARTBEAT_OR_EXPIRED'
            p['classification']=p['feed_classification'];p['blockers']=[] if p['enabled'] else [p['operational_state']]
            p['explanation']='Nowe decyzje zapisane przed następną świecą. Catch-up wykluczony z liczby kandydatów prospektywnych. CME entitlement niezweryfikowane; badanie pasywne.'
            incidents=runtime/'observer-incidents.json'
            if incidents.is_file() and incidents.stat().st_size<100000:
                p['observer_incidents']=json.loads(incidents.read_text())
            return p
        return None
    cfg={'DATA_DIR':str(runtime),'TANJA_DASHBOARD_PASSWORD':'local-shadow-preview',
         'ICT_AI_REPLAY_DB':str(store.path),'ICT_AI_LIBRARY_DB':str(Path(library).resolve()),
         'ICT_AI_MODE':'OFF','ICT_AI_PAID_APPROVED':'false','AUTO_SUBMIT':'0','EXEC_MODE':'off',
         'ICT_AI_CONTEXT_REPORT_DIR':str(runtime/'unavailable-research-reports')}
    @app.before_request
    def safety():
        if request.remote_addr not in ('127.0.0.1','::1'):abort(403)
        if request.method not in ('GET','HEAD','OPTIONS'):abort(405)
        request.environ['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:local-shadow-preview').decode()
        if request.path.endswith('/api/budget'):
            return jsonify(calls=0,accounted_usd=0,reserved_unknown_usd=0,cap_usd=0,
                status='LOCAL_SHADOW_ONLY_NO_API_AUTHORIZATION; historical financial ledger not connected')
    dashboard.register(app);ict_shadow_cloud.register(app,cfg,start=False);ict_ai_trader.register(app,cfg,start=False)
    register_research(app, runtime)
    register_episode6(app, runtime)
    @app.after_request
    def native_panel(response):
        if request.path=='/ict-shadow' and response.status_code==200 and 'text/html' in response.content_type:
            text=response.get_data(as_text=True)
            marker='<section class="hero"><div><div class="eyebrow" id="ictai-dataOrigin">'
            if marker not in text:raise ValueError('NATIVE_AI_VIEW_NOT_FOUND')
            text=text.replace('</head>','<style>.shadow-scroll{overflow:auto} #shadow-mvp-panel td{vertical-align:top;padding:8px} #shadow-mvp-panel code{font-size:11px;overflow-wrap:anywhere} #shadow-mvp-panel pre{white-space:pre-wrap} #shadow-mvp-panel select{margin:6px;padding:6px} #shadow-mvp-panel h4{margin-top:24px}</style></head>').replace(marker,PANEL+marker,1).replace('</body>','<script src="/ict-shadow/shadow-mvp/assets/app.js"></script></body>')
            response.set_data(text)
            text=response.get_data(as_text=True).replace(PANEL,PANEL+RESEARCH_PANEL+EPISODE6_PANEL,1)
            research=request.args.get('shadow_run')=='research'
            episode6=request.args.get('shadow_run')=='episode6'
            extra='.research-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px;margin:20px 0}.research-cards>div{border:1px solid #32415d;border-radius:8px;padding:16px;background:#122035}.research-cards strong{display:block;font-size:26px;color:#9ee4da}.research-cards span{display:block;font-size:12px;margin-top:8px} #strategy-research-panel td,#strategy-research-panel th{padding:10px;text-align:left;vertical-align:top} #strategy-research-panel pre{white-space:pre-wrap;overflow-wrap:anywhere} #strategy-research-panel code{overflow-wrap:anywhere;font-size:11px}'
            if research:
                extra+='#shadow-mvp-panel{display:none} #strategy-research-panel[hidden]{display:block} #ictTraderView #strategy-research-panel~*{display:none!important}'
                text=text.replace('</body>','<script src="/ict-shadow/shadow-mvp/research/assets/app.js"></script></body>')
            if episode6:
                extra+='#shadow-mvp-panel,#strategy-research-panel{display:none!important} #episode6-panel[hidden]{display:block} #ictTraderView #episode6-panel~*{display:none!important}'
                text=text.replace('</body>','<script src="/ict-shadow/shadow-mvp/episode6/assets/app.js"></script></body>')
            extra+=' #episode6-panel td,#episode6-panel th{padding:9px;text-align:left;vertical-align:top} #episode6-panel pre{white-space:pre-wrap;overflow-wrap:anywhere} #episode6-panel code{overflow-wrap:anywhere;font-size:11px} #episode6-panel select{margin:5px;padding:6px} #episode6-panel h3{margin-top:25px} #episode6-panel .condition-FAILED{color:#fb7185} #episode6-panel .condition-VERIFIED{color:#30d5a1} #episode6-panel .condition-CONTEXT_UNRESOLVED,#episode6-panel .condition-SOURCE_AMBIGUOUS{color:#f5c86a}'
            response.set_data(text.replace('</head>','<style>'+extra+'</style></head>'))
        response.headers['Cache-Control']='no-store'
        return response
    @app.get('/ict-shadow/shadow-mvp/assets/app.js')
    def script():return Response(Path(__file__).with_name('static').joinpath('shadow-evidence.js').read_text(),mimetype='text/javascript')
    @app.get('/ict-shadow/shadow-mvp/feed-status')
    def feed_status():
        file=runtime/'feed-status.json'
        return jsonify(json.loads(file.read_text()) if file.exists() else {'status':'LIVE_NOT_VERIFIED'})
    @app.get('/ict-shadow/shadow-mvp/prospective-status')
    def prospective_status():
        current=operational_status()
        if current:return jsonify(current)
        file=runtime/'prospective-status.json'
        return jsonify(json.loads(file.read_text()) if file.exists() else {
            'enabled':False,'classification':'UNVERIFIED','prospective_candidates':0,
            'blockers':['FEED_ENTITLEMENT_NOT_VERIFIED'],'llm_status':'PENDING_LLM'})
    @app.get('/ict-shadow/shadow-mvp/status')
    def status():
        if request.args.get('run')=='prospective':
            p=operational_status()
            if not p:abort(503)
            return jsonify(mode='PROSPECTIVE_SHADOW',session=p['operational_state'],current_as_of=(p['last_bar_start'] or 0)+60,candidates=p['prospective_candidates'],candidates_by_instrument=p['candidate_counts'],decisions=p['decisions'],outcome_records=p['prospective_outcomes'],source_qualified=0,openai_calls=0,broker_orders=0,catchup_candidates=p['catchup_candidates'])
        return jsonify(summary(store))
    @app.get('/ict-shadow/shadow-mvp/candidates')
    def candidates():
        selected=selected_store();prospective=request.args.get('run')=='prospective'
        try:offset=int(request.args.get('offset',0))
        except ValueError:abort(400)
        if not 0<=offset<=10000:abort(400)
        symbol=request.args.get('symbol','');session=request.args.get('session','')
        if symbol not in ('','NQ','MNQ','ES') or session not in ('','LO','NYAM','NYPM','BETWEEN_WINDOWS'):abort(400)
        with selected.readonly() as db:
            clause=" AND candidate_id IN (SELECT d.candidate_id FROM shadow_records d JOIN passive_log p ON p.key=json_extract(d.payload,'$.persistence_batch') AND p.kind='DURABLE' WHERE d.stage='DETECTED' AND json_extract(p.payload,'$.prospective_timing_verified')=1)" if prospective else ''
            rows=db.execute("SELECT candidate_id,min(as_of),max(as_of) FROM shadow_records WHERE stage='ASSESSMENT' AND (?='' OR json_extract(payload,'$.symbol')=?) AND (?='' OR json_extract(payload,'$.session')=?)"+clause+" GROUP BY candidate_id ORDER BY min(as_of),candidate_id LIMIT 51 OFFSET ?",(symbol,symbol,session,session,offset)).fetchall()
            items=[]
            for ident,created,latest in rows[:50]:
                value=json.loads(db.execute("SELECT payload FROM shadow_records WHERE candidate_id=? AND as_of=? AND stage='ASSESSMENT'",(ident,latest)).fetchone()[0]);value['created_at']=created;items.append(value)
        return jsonify(items=items,has_more=len(rows)>50)
    @app.get('/ict-shadow/shadow-mvp/events')
    def observed_events():
        with selected_store().readonly() as db:
            manifest=json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
            start=manifest.get('prospective_after_bar_start',0)+60
            rows=[json.loads(p) for p, in db.execute("SELECT payload FROM events WHERE as_of>=? AND kind IN ('SWEEP','FVG','IFVG','STRUCTURE_BREAK','DISPLACEMENT') ORDER BY as_of DESC LIMIT 30",(start,))]
        for e in rows:
            e['selection_status']='M1_REFERENCE_BELOW_RESEARCH_SELECTOR_MINIMUM_M5' if e['kind']=='SWEEP' and max(e['timeframe'],e.get('level_timeframe',1))<5 else 'OBSERVATION_NOT_SOURCE_QUALIFICATION'
        return jsonify(items=rows)
    @app.get('/ict-shadow/shadow-mvp/events/<ident>')
    def observed_event(ident):
        from .ai import packet as make_packet
        selected=selected_store()
        with selected.readonly() as db:
            row=db.execute('SELECT payload FROM events WHERE id=?',(ident,)).fetchone()
            if row is None:abort(404)
            e=json.loads(row[0])
        snap={'as_of':e['available_at'],'events':[e],'setups':[],'retrieval_ids':[],'context':{e['symbol']:{'contract':e['contract'],'active_levels':[],'active_zones':[],'ranges':{}}}}
        p=make_packet(selected,snap)
        annotations=[]
        for item in p['evidence'].values():
            if item['id']==ident or item.get('kind')=='PIVOT':
                annotations.append(dict(observation_id=item['id'],kind=e['kind'] if item['id']==ident else 'LIQUIDITY_REFERENCE',ts=item['ts'],available_at=item['available_at'],price=item.get('price'),lower=item.get('lower'),upper=item.get('upper'),direction=item.get('direction'),evidence_ids=item.get('evidence_ids',[]),status='DETECTED_GEOMETRY_ONLY_NOT_A_QUALIFIED_SETUP'))
        chart={'symbol':e['symbol'],'as_of':e['available_at'],'frames':{'1':selected.chart(snap,e['symbol'],1,150)},'annotations':annotations}
        return jsonify(event=e,evidence=p['evidence'],chart=chart,qualification=False,interpretation='PENDING_LLM')
    @app.get('/ict-shadow/shadow-mvp/candidates/<ident>')
    def detail(ident):
        with selected_store().readonly() as db:
            rows=db.execute('SELECT payload FROM shadow_records WHERE candidate_id=? ORDER BY as_of,rowid',(ident,)).fetchall()
            if not rows:abort(404)
            records=[json.loads(r[0]) for r in rows];latest=[r for r in records if r['stage']=='ASSESSMENT'][-1]
            at=request.args.get('at')
            if at is not None:
                try:wanted=int(at)
                except ValueError:abort(400)
                matching=[r for r in records if r['stage']=='ASSESSMENT' and r['as_of']==wanted]
                if not matching:abort(404)
                latest=matching[0]
            packet=json.loads(db.execute('SELECT payload FROM shadow_evidence WHERE id=?',(latest['evidence_packet_id'],)).fetchone()[0])
            durability={}
            if request.args.get('run')=='prospective':
                for r in records:
                    key=r.get('persistence_batch')
                    found=db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE' AND key=?",(key,)).fetchone()
                    if found:durability[key]=json.loads(found[0])
        # Only annotation ancestors, loaded for one selected saved assessment.
        refs=set()
        for item in packet.get('chart',{}).get('annotations',[]):
            refs.update(item.get('evidence_ids',[]));refs.update(item.get('source_candles',[]))
            refs.update(x for x in (item.get('source_candle'),item.get('reference_id')) if x)
        return jsonify(records=records,latest=latest,durability=durability,chart=packet.get('chart'),rules=packet['source_records'],context=packet['snapshot']['context'],evidence_count=len(packet['evidence']),annotation_evidence={key:packet['evidence'][key] for key in refs if key in packet['evidence']})
    @app.get('/ict-shadow/shadow-mvp/evidence/<ident>')
    def evidence(ident):
        with selected_store().readonly() as db:row=db.execute('SELECT payload FROM shadow_evidence WHERE id=?',(ident,)).fetchone()
        if not row:abort(404)
        return jsonify(json.loads(row[0]))
    return app
