/* Real saved OHLC only. No synthetic path, price inference or provider calls. */
(()=>{
if(['research','episode6'].includes(new URLSearchParams(location.search).get('shadow_run')))return;
const prospective=new URLSearchParams(location.search).get('shadow_run')==='prospective';
const runQuery=prospective?'run=prospective':'';
const base='/ict-shadow/shadow-mvp';let offset=0,current=null,tf='1',windowBars=300;
const $=s=>document.querySelector(s),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time=t=>new Date(t*1000).toLocaleString('en-GB',{timeZone:'America/New_York'});
const hh=t=>new Date(t*1000).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'});
async function get(path){const r=await fetch(base+path+(runQuery?(path.includes('?')?'&':'?')+runQuery:''));if(!r.ok)throw Error('HTTP '+r.status);return r.json()}
function fail(e){$('#shadow-mvp-status').textContent='VALIDATION / READ FAILURE: '+e.message}
function links(refs){return (refs||[]).map(r=>{try{const u=new URL(r.url);return u.protocol==='https:'&&['www.youtube.com','youtube.com'].includes(u.hostname)?`<a target="_blank" rel="noopener" href="${esc(u.href)}">${esc(r.video_id)} · ${esc(r.start)}–${esc(r.end)} s</a>`:''}catch{return ''}}).join(' · ')}
const colors={SWEEP:'#ffaa00',LIQUIDITY_REFERENCE:'#ab9dff',STRUCTURE_BREAK:'#60a5fa',DISPLACEMENT:'#f5c86a',FVG:'#20cba4',IFVG:'#e879f9',RETEST:'#f3f4f6',TARGET_CANDIDATE:'#38bdf8',INVALIDATION:'#fb7185'};
function refs(e,d){return [...new Set([...(e.source_candles||[]),e.source_candle,e.reference_id,...(e.evidence_ids||[])].filter(Boolean))].map(id=>{const r=d.annotation_evidence?.[id];return `<p><code>${esc(id)}</code>${r?`<br>${esc(time(r.ts))} NY · dostępne ${esc(time(r.available_at))}${r.close!=null?` · O ${r.open} H ${r.high} L ${r.low} C ${r.close}`:''}`:''}</p>`}).join('')}
function draw(){
 const c=current?.chart,host=$('#shadow-chart');if(!c){host.textContent='Brak zapisanego wykresu dowodowego w tym starszym pakiecie.';return}
 const bars=(c.frames[tf]||[]).slice(-windowBars);if(!bars.length){host.textContent='DATA_UNAVAILABLE — brak pełnych świec tego TF.';return}
 const W=Math.max(560,Math.min(1200,host.clientWidth||1100)),H=400,L=44,R=80,T=28,B=40,x0=bars[0].ts,x1=bars[bars.length-1].end,span=x1-x0||60;
 const min=Math.min(...bars.map(b=>b.low)),max=Math.max(...bars.map(b=>b.high)),pad=Math.max((max-min)*.12,1),lo=min-pad,hi=max+pad;
 const x=t=>L+(t-x0)/span*(W-L-R), y=p=>T+(hi-p)/(hi-lo)*(H-T-B),fmt=p=>Number(p).toFixed(2);
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(c.symbol)} ${esc(tf)} minut, rzeczywiste świece do ${esc(time(c.as_of))}" style="width:100%;background:#10192b;border:1px solid #344154;border-radius:12px">`;
 for(let i=0;i<=5;i++){const p=lo+(hi-lo)*i/5;svg+=`<line x1="${L}" y1="${y(p)}" x2="${W-R}" y2="${y(p)}" stroke="#273449"/><text x="${W-R+8}" y="${y(p)+4}" fill="#cbd5e1" font-size="12">${fmt(p)}</text>`}
 const width=Math.min(12,Math.max(1,(W-L-R)/bars.length*.65));
 for(const b of bars){const xx=x((b.ts+b.end)/2),color=b.close>=b.open?'#30d5a1':'#fb7185';svg+=`<g><title>${esc(b.id)} | ${esc(time(b.ts))} NY | dostępna ${esc(time(b.available_at))} | O ${b.open} H ${b.high} L ${b.low} C ${b.close}</title><line x1="${xx}" y1="${y(b.high)}" x2="${xx}" y2="${y(b.low)}" stroke="${color}"/><rect x="${xx-width/2}" y="${y(Math.max(b.open,b.close))}" width="${width}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/></g>`}
 let lane=0;
 for(const a of c.annotations){const color=colors[a.kind]||'#fff',title=esc(`${a.kind} ${a.observation_id} | ${time(a.available_at)} NY | ${a.status}`);
  if(a.lower!=null&&a.upper!=null&&a.upper>=lo&&a.lower<=hi&&a.available_at<=x1){const xx=x(Math.max(a.available_at,x0));svg+=`<g><title>${title} | ${a.lower}–${a.upper}</title><rect x="${xx}" y="${y(Math.min(hi,a.upper))}" width="${Math.max(0,W-R-xx)}" height="${Math.max(2,y(Math.max(lo,a.lower))-y(Math.min(hi,a.upper)))}" fill="${color}" fill-opacity=".12" stroke="${color}" stroke-dasharray="4 3"/></g>`}
  const p=a.price;if(p==null||p<lo||p>hi)continue;
  if(['TARGET_CANDIDATE','LIQUIDITY_REFERENCE'].includes(a.kind)&&a.available_at<=x1){const knownX=x(Math.max(a.available_at,x0));svg+=`<g><title>${title} | ${p}</title><line x1="${knownX}" y1="${y(p)}" x2="${W-R}" y2="${y(p)}" stroke="${color}" stroke-dasharray="5 4"/><circle cx="${knownX}" cy="${y(p)}" r="2" fill="${color}"/><text x="${L+5}" y="${y(p)-4}" fill="${color}" font-size="10">${esc(a.kind)} ${fmt(p)}</text></g>`}
  else if(a.ts>=x0&&a.ts<=x1){const xx=x(a.ts),ly=T+16+(lane++%4)*17;svg+=`<g><title>${title} | ${p}</title><line x1="${xx}" y1="${ly+4}" x2="${xx}" y2="${y(p)}" stroke="${color}" stroke-opacity=".5"/><circle cx="${xx}" cy="${y(p)}" r="4" fill="${color}"/><text x="${Math.min(xx+5,W-R-145)}" y="${ly}" fill="${color}" font-size="11">${esc(a.kind)} ${hh(a.available_at)}</text></g>`}
 }
 for(let i=0;i<6;i++){const t=x0+span*i/5,label=Number(tf)>=60?new Date(t*1000).toLocaleDateString('en-GB',{timeZone:'America/New_York',day:'2-digit',month:'2-digit'})+' '+hh(t):hh(t);svg+=`<text x="${x(t)}" y="${H-15}" text-anchor="middle" fill="#9aaec7" font-size="12">${label}</text>`}
 svg+='</svg>';host.innerHTML=svg+`<p>${c.annotations.filter(a=>['FVG','IFVG'].includes(a.kind)).map(a=>`<span style="color:${colors[a.kind]}">${a.kind} ${esc(a.direction||'')} ${a.lower}–${a.upper} · ${hh(a.available_at)} NY</span>`).join(' · ')}</p><p>${bars.length} rzeczywistych świec · ${esc(time(bars[0].ts))} → ${esc(time(bars.at(-1).available_at))} NY. Poziomy poza skalą i kotwice spoza widoku są w tabeli dowodów. Najedź na świecę lub znacznik, aby zobaczyć ID.</p>`;
}
async function detail(id,at){
 const d=await get('/candidates/'+encodeURIComponent(id)+(at?'?at='+at:''));current=d;const a=d.latest,c=d.chart;
 const url=new URL(location.href);url.searchParams.delete('shadow_event');url.searchParams.set('shadow_candidate',id);url.searchParams.set('shadow_at',a.as_of);history.replaceState(null,'',url);
 $('#shadow-mvp-detail').innerHTML=`<h3>${esc(a.symbol)} ${esc(a.direction)} · ${esc(a.decision)} <small>LLM: PENDING_LLM</small></h3>
 <p>${esc(a.observation_mode||'SIMULATED_REPLAY')} · ${esc(id)} · grupa ekspozycji ${esc(a.correlation?.exposure_family||a.symbol)} — NQ/MNQ nie są niezależnymi głosami.</p>
 <label>Stan wiedzy z chwili <select id="shadow-asof">${d.records.filter(r=>r.stage==='ASSESSMENT').map(r=>`<option value="${r.as_of}" ${r.as_of===a.as_of?'selected':''}>${esc(time(r.as_of))} NY · ${esc(r.decision)}</option>`).join('')}</select></label>
 <label>TF <select id="shadow-tf">${[['1','M1'],['5','M5'],['15','M15'],['60','H1'],['240','H4'],['1440','D1']].map(([v,t])=>`<option value="${v}" ${tf===v?'selected':''}>${t}</option>`).join('')}</select></label>
 <label>Zakres <select id="shadow-window">${[30,60,150,300].map(n=>`<option value="${n}" ${windowBars===n?'selected':''}>${n} świec</option>`).join('')}</select></label>
 ${d.durability?.[a.persistence_batch]?`<p><b>Dowód zapisu:</b> ${esc(new Date(d.durability[a.persistence_batch].commit_observed_at_ns/1e6).toISOString())} UTC · ${d.durability[a.persistence_batch].prospective_timing_verified?'zapis przed zamknięciem następnej M1':'catch-up lub niepotwierdzony termin zapisu'}. ${esc(d.durability[a.persistence_batch].effective_decision_mode)}</p>`:''}
 <p>Wykres i poziomy wyłącznie do wybranej chwili. Późniejsze zdarzenia pozostają osobno w dzienniku.</p><div id="shadow-chart"></div>
 <p><b>Model:</b> ${esc(a.model)} · geometria badawcza. Entry / SL / preferowany TP: <b>niezakwalifikowane źródłowo</b>. Wykonanie wyłączone.</p>
 <p>Stan detektora: <b>${esc(a.detector_state)}</b> — etykieta badawcza, niezależna od kwalifikacji źródłowej i LLM.</p>
 <p><b>Brakujące warunki:</b> ${esc(a.missing_requirements.join(', '))}</p>
 <p><b>Dowody przeciwne:</b> ${c?.annotations.filter(e=>e.direction&&e.direction!==a.direction).map(e=>esc(e.kind+' '+e.direction+' '+hh(e.available_at)+' NY — kierunek przeciwny do hipotezy; nie jest jej potwierdzeniem.')).join(' ')||'Brak dodatkowego przeciwnego zdarzenia w zachowanym łańcuchu; nie dowodzi to braku wszystkich przeciwnych scenariuszy.'}</p>
 <p><b>Następne potwierdzenie:</b> ${esc(a.next_confirmation.join(', ')||'Rozstrzygnięcie kontekstu i warunków źródłowych')} · badawcze podważenie odwrócenia: zamknięcie M1 poza ${esc(a.invalidation.level)}.</p>
 ${c?`<p>Wykryto w ${esc(c.origin_session)} o ${hh(c.detected_at)} NY; obserwacja ${esc(c.observation_session)}. Limit selektora ${hh(c.research_expires_at)} jest konwencją badawczą; nie definiuje okna wejścia.</p>
 <h4>Adnotacje z rzeczywistych obserwacji</h4><div class="shadow-scroll"><table><thead><tr><th>Zdarzenie / status</th><th>Dostępne NY</th><th>Cena / pomiar</th><th>ID i źródłowe świece</th></tr></thead><tbody>${c.annotations.map(e=>`<tr><td>${esc(e.kind)} ${esc(e.direction||'')}<br>${esc(e.status)}</td><td>${esc(time(e.available_at))}</td><td>${esc(e.lower!=null?e.lower+'–'+e.upper:e.price)}${e.reclaim!=null?' · reclaim '+esc(e.reclaim):''}${e.measurement?'<br>'+esc(JSON.stringify(e.measurement)):''}${e.kind==='RETEST'?'<br>'+esc(e.entry_windows.map(w=>w.model+': '+w.status).join('; ')):''}</td><td><code>${esc(e.observation_id)}</code><details><summary>Referencje</summary>${refs(e,d)}</details></td></tr>`).join('')}</tbody></table></div>
 <h4>Numeryczna lista celów — bez preferencji ICT</h4><p>Odległość od ostatniego close ${c.distance_reference.price}, nie od domniemanego wejścia. Zakres: dostępne, zachowane poziomy detektora.</p><table><thead><tr><th>Ranga</th><th>Poziom / TF</th><th>Odległość</th><th>Dostępność / ID</th></tr></thead><tbody>${c.targets.map(t=>`<tr><td>${t.numerical_rank}</td><td>${t.price} / M${t.timeframe}</td><td>${t.distance.toFixed(2)}</td><td>${esc(time(t.available_at))}<br><code>${esc(t.observation_id)}</code></td></tr>`).join('')||'<tr><td colspan="4">Brak wspieranych kierunkowych celów w zachowanych poziomach.</td></tr>'}</tbody></table>
 <h4>Okna źródłowe w wybranej chwili</h4><ul>${c.source_windows.map(w=>`<li>${esc(w.model)}: ${esc(w.status)} · ${esc(w.rule_id)}</li>`).join('')}</ul>`:''}
 <h4>Dziennik, łącznie z późniejszymi obserwacjami</h4><table><thead><tr><th>NY</th><th>Etap</th><th>Wynik / przyczyna</th></tr></thead><tbody>${d.records.map(r=>`<tr><td>${esc(time(r.as_of))}</td><td>${esc(r.stage)}</td><td>${esc(r.decision||r.status||r.context_status||'')}${r.added_confirmations?.length?' · '+esc(r.added_confirmations.join(', ')):''}${r.newly_observed_retests?.length?' · późniejszy RETEST':''}${r.newly_observed_inversions?.length?' · późniejszy IFVG':''}${r.reason?' · '+esc(r.reason):''}</td></tr>`).join('')}</tbody></table>
 <h4>Reguły źródłowe — zastosowanie nierozstrzygnięte</h4>${d.rules.map(r=>`<details><summary>${esc(r.model)} · ${esc(r.id)}</summary><p>${esc(r.instrument_scope_assessment||'RELEVANT_ONLY')}</p><p>${esc(r.description)}</p><pre>${esc(JSON.stringify(r.fields,null,2))}</pre><p>Wyjątki: ${esc(JSON.stringify(r.exceptions))}</p>${links(r.references)}</details>`).join('')}
 <p><a href="${base}/evidence/${esc(a.evidence_packet_id)}${runQuery?'?'+runQuery:''}" target="_blank">Zapisany pakiet dowodów z tej chwili</a></p>`;
 $('#shadow-asof').onchange=e=>detail(id,e.target.value).catch(fail);$('#shadow-tf').onchange=e=>{tf=e.target.value;draw()};$('#shadow-window').onchange=e=>{windowBars=Number(e.target.value);draw()};draw();
 $('#shadow-mvp-detail').scrollIntoView({block:'start'});
}
async function eventDetail(id){
 const d=await get('/events/'+encodeURIComponent(id));current=d;tf='1';windowBars=150;const url=new URL(location.href);url.searchParams.delete('shadow_candidate');url.searchParams.delete('shadow_at');url.searchParams.set('shadow_event',id);history.replaceState(null,'',url);
 const exclusion=d.event.kind==='SWEEP'&&Math.max(d.event.timeframe,d.event.level_timeframe||1)<5?'Sweep M1 z odniesieniem M1 nie spełnia minimum M5 obecnego selektora badawczego. To konwencja selektora, nie uniwersalna reguła ICT.':'Znaczenie zdarzenia wymaga oceny w ramach konkretnego modelu i kontekstu; sam detektor nie kwalifikuje wejścia.';
 $('#shadow-mvp-detail').innerHTML=`<h3>Obserwacja ${esc(d.event.symbol)} ${esc(d.event.kind)} ${esc(d.event.direction)} · ${esc(time(d.event.available_at))} NY</h3><p><b>Geometria detektora, nie kandydat źródłowy ani prognoza LLM.</b> ${esc(exclusion)}</p><div id="shadow-chart"></div><h4>Oryginalne obserwacje i źródłowe świece</h4>${Object.values(d.evidence).map(e=>`<details><summary>${esc(e.kind||'CANDLE')} · ${esc(e.id)} · dostępne ${esc(time(e.available_at))} NY</summary><pre>${esc(JSON.stringify(e,null,2))}</pre></details>`).join('')}`;draw();$('#shadow-mvp-detail').scrollIntoView({block:'start'});
}
async function measuredReport(){
 if(!prospective)return;
 let host=$('#shadow-measured-report');if(!host){host=document.createElement('div');host.id='shadow-measured-report';$('#shadow-prospective').after(host)}
 try{
 const r=await get('/research/prospective-current');
 host.innerHTML=`<p><b>${esc(r.report_status)}</b> · pomiar do ${esc(r.cutoff.new_york)} · nowe świece ${r.bars.newly_received_total} · zapisane terminowo ${r.bars.timely_prospective_total} · wszystkie zdarzenia ${r.events.total} · M1 sweeps poza selektorem ${r.events.m1_sweeps_excluded_by_selector??'—'} · kwalifikacje ${r.candidates.source_qualified}.</p>
 <details><summary>Pełny bilans zdarzeń i wyników (osobno od replayu)</summary><pre>${esc(JSON.stringify({events:r.events.by_kind,candidates:r.candidates.by_instrument,models:r.candidates.by_model,sessions:r.candidates.by_origin_session,decisions:r.candidates.latest_decisions,outcomes:r.outcomes,gaps:r.gaps},null,2))}</pre><a href="${base}/research/prospective-current" target="_blank">Aktualny odczyt dziennika</a></details>
 <details id="shadow-m1-source"><summary>Co źródła mówią o pierwszych 11 sweepach M1?</summary><p>Wszystkie wystąpiły 12:10–12:16 NY. M1 jest dopuszczony jako TF wykonawczy; nie dowodzi to ważności dowolnego lokalnego pivotu jako płynności dla konkretnego modelu. Źródłowy przykład Episode 6 rozróżnia lokalny scalp i plan dziennego zakresu oparty na M15. Te zdarzenia były poza oknami Charter AM i Silver Bullet; późniejsze potwierdzenia wymagają osobnej oceny.</p><p>Nie zmieniono minimum M5 selektora. Nie uznano sweepów za nieistniejące ani źródłowo zakwalifikowane setupy.</p><a href="${base}/research/m1-review" target="_blank">11 ID obserwacji, dokładne reguły i brakujące warunki</a></details>`;
 }catch(e){host.textContent='BILANS SESJI NIEDOSTĘPNY: '+e.message}
}
async function load(){
 try{const symbol=$('#shadow-symbol').value,session=$('#shadow-session').value;
 const [s,c,f,p]=await Promise.all([get('/status'),get('/candidates?offset='+offset+'&symbol='+symbol+'&session='+session),get('/feed-status'),get('/prospective-status')]);
 $('#shadow-mvp-status').textContent=`${s.session} · ${time(s.current_as_of)} NY · kandydaci ${s.candidates} ${JSON.stringify(s.candidates_by_instrument||{})} · decyzje ${JSON.stringify(s.decisions)} · LLM 0 · zlecenia 0`;
 if(prospective){
 $('#shadow-mode-notice').innerHTML='<b>PROSPECTIVE_SHADOW · FEED_UNVERIFIED · PENDING_LLM · ZLECENIA WYŁĄCZONE</b><br>Nowe świece TradingView → Railway. Brak potwierdzenia uprawnień CME. Oceny deterministyczne; brak decyzji LLM i kwalifikacji źródłowej.';
 $('#shadow-feed').textContent=`Ostatni zapis odczytu ${time(p.heartbeat_at)} NY. Stan: ${p.operational_state}. ${p.hosting==='RAILWAY_INTERNAL'?'Obserwator wewnętrzny Railway':'Zapis obecnego obserwatora lokalnego'} · koniec zatwierdzonego okna ${time(p.stop_at)} NY.`;
 if(p.observer_incidents?.length)$('#shadow-feed').textContent+=' '+p.observer_incidents.map(i=>`Zachowana luka obserwacji: ${time(i.last_persisted_close)}–${time(i.first_resumed_prospective_close)} NY; ${i.catchup_batches} minut (${i.catchup_bars} świec) odczytano później jako catch-up, nie prognozy. ${i.reason}`).join(' ');
 $('#shadow-prospective').innerHTML=`<p><b>${esc(p.classification)} · ${esc(p.operational_state)}</b></p><p>${esc(p.explanation)} Kandydaci prospektywni: ${p.prospective_candidates}; catch-up: ${p.catchup_candidates}. Późniejsze obserwacje: ${p.prospective_outcomes}; braki po 180 s tolerancji: ${p.source_gaps}. Kwalifikacje źródłowe: 0.</p><table><thead><tr><th>Instrument</th><th>Close NY</th><th>Odbiór Railway UTC</th><th>Close → odbiór</th></tr></thead><tbody>${Object.entries(p.feeds||{}).map(([symbol,v])=>`<tr><td>${esc(symbol)}</td><td>${esc(time(v.latest_close))}</td><td>${esc(new Date(v.latest_received*1000).toISOString())}</td><td>${Number(v.close_to_receipt_seconds).toFixed(3)} s</td></tr>`).join('')}</tbody></table><p>Godziny badawcze: LO 02–05, NYAM 08:30–12, NYPM 13–16 NY. Kalendarz giełdy jest niezależny od okien wejścia. ${esc(p.exchange_status||'Status giełdy: patrz przypięty kalendarz sesji.')} ${p.calendar_valid_until?'Kalendarz ważny do '+esc(time(p.calendar_valid_until))+' NY.':''}</p>`;
 }else{
 $('#shadow-feed').textContent=f.checked_at?`Archiwalna kontrola Railway ${time(f.checked_at)} NY. Ten widok jest historycznym replayem.`:'SIMULATED_REPLAY';
 $('#shadow-prospective').textContent=`Obserwator prospektywny: ${p.operational_state||'NIEAKTYWNY'} · ${p.prospective_candidates||0} kandydatów. Otwórz „Obserwacja prospektywna”, aby zobaczyć osobny dziennik.`;
 }
 if(prospective){
 const ev=await get('/events');let host=$('#shadow-event-list');if(!host){host=document.createElement('div');host.id='shadow-event-list';$('#shadow-prospective').after(host)}
 const wasOpen=host.querySelector('details')?.open;host.innerHTML=`<details ${wasOpen?'open':''}><summary>Nowe zdarzenia detektora — ${ev.items.length} ostatnich, nie wszystkie są kandydatami</summary><table><thead><tr><th>NY / instrument</th><th>Zdarzenie / TF</th><th>Selekcja</th></tr></thead><tbody>${ev.items.map(e=>`<tr><td>${esc(time(e.available_at))} · ${esc(e.symbol)}</td><td><button data-event="${esc(e.id)}">${esc(e.kind)} ${esc(e.direction)} · M${e.timeframe}</button></td><td>${esc(e.selection_status)}</td></tr>`).join('')}</tbody></table></details>`;
 host.querySelectorAll('[data-event]').forEach(b=>b.onclick=()=>eventDetail(b.dataset.event).catch(fail));
 }
 $('#shadow-mvp-candidates').innerHTML=c.items.map(a=>`<tr><td>${esc(time(a.created_at))}</td><td>${esc(a.symbol)}<br>${esc(a.session||'')}</td><td>${esc(a.direction)}</td><td><button data-candidate="${esc(a.candidate_id)}">${esc(a.observed_sequence.join(' → ')||'SWEEP')}</button></td><td>${esc(a.decision)}</td><td>PENDING_LLM</td></tr>`).join('');
 $('#shadow-mvp-candidates').querySelectorAll('button').forEach(b=>b.onclick=()=>detail(b.dataset.candidate).catch(fail));$('#shadow-mvp-more').disabled=!c.has_more;
 }catch(e){fail(e)}
}
$('#shadow-mvp-refresh').onclick=()=>{offset=0;load()};$('#shadow-mvp-more').onclick=()=>{offset+=50;load()};
for(const id of ['#shadow-symbol','#shadow-session'])$(id).onchange=()=>{offset=0;load()};
if(prospective){setInterval(load,15000);setInterval(measuredReport,60000);measuredReport()}
load().then(()=>{const p=new URLSearchParams(location.search),id=p.get('shadow_candidate');if(p.get('shadow_event'))eventDetail(p.get('shadow_event')).catch(fail);else if(id)detail(id,p.get('shadow_at')).catch(fail)});
})();
