/* Read-only source qualification. Saved real OHLC; no fill or AI inference. */
(()=>{
if(new URLSearchParams(location.search).get('shadow_run')!=='episode6')return;
const $=s=>document.querySelector(s),base='/ict-shadow/shadow-mvp/episode6';
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=x=>x==null?'—':Number(x).toLocaleString('pl-PL');
const ny=t=>t==null?'—':new Date(t*1000).toLocaleString('pl-PL',{timeZone:'America/New_York'});
const json=x=>`<pre>${esc(JSON.stringify(x,null,2))}</pre>`;
const statuses=['VERIFIED','FAILED','NOT_YET_OBSERVED','DATA_UNAVAILABLE','SOURCE_AMBIGUOUS','CONTEXT_UNRESOLVED'];
let model,summary,offset=0,requestSerial=0;
async function get(path){const r=await fetch(base+path);if(!r.ok)throw Error('HTTP '+r.status+' — dane kwalifikacji niedostępne');return r.json()}
function fail(e){$('#episode6-state').textContent='READ / VALIDATION FAILURE: '+e.message}
function sourceLinks(refs){return(refs||[]).map(r=>{try{const u=new URL(r.url||'');if(u.protocol==='https:'&&['youtube.com','www.youtube.com'].includes(u.hostname)){const stamp=s=>Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');return `<a target="_blank" rel="noopener" href="${esc(u.href)}">${esc(r.title||r.video_id||'Episode 6')}${r.start!=null?' · '+stamp(r.start)+(r.end!=null?'–'+stamp(r.end):''):''}</a>`}}catch{}return ''}).join(' · ')}
function sourceCard(def){
 if(!def)return '';
 const refs=def.sources||def.source_references||def.references||[];
 return `<details><summary>Warunek źródłowy: ${esc(def.id)} · ${esc(def.title||def.label)}</summary><p>${esc(def.requirement||def.role)} · ${esc(def.scope)}</p><p>Wymaga: ${esc((def.evidence_requirements||[]).join('; '))}</p><p>${esc((def.notes||[]).join(' '))}</p><p>${sourceLinks(refs)}</p>${refs.map(r=>(r.extracted_text||r.excerpt)?`<blockquote>${esc(r.extracted_text||r.excerpt)}${r.excerpt_is_truncated_display?' […]':''}</blockquote>`:'').join('')}<p>${esc((def.source_rule_ids||[]).join(' · '))}</p></details>`;
}
// Same OHLC/annotation presentation as the existing shadow chart, scoped to this saved case.
function chart(c){
 if(!c)return '<p>DATA_UNAVAILABLE — brak zapisanego wykresu.</p>';
 const bars=(c.frames?.['1']||c.bars||[]).slice(-120);
 if(!bars.length)return '<p>DATA_UNAVAILABLE — brak świec M1.</p>';
 if(bars.some(b=>b.available_at>c.as_of||b.symbol!==c.symbol))throw Error('NONCAUSAL_CHART_EVIDENCE');
 const W=1100,H=400,L=40,R=80,T=30,B=40,x0=bars[0].ts,x1=bars.at(-1).end;
 const min=Math.min(...bars.map(b=>b.low)),max=Math.max(...bars.map(b=>b.high)),pad=Math.max((max-min)*.12,1),lo=min-pad,hi=max+pad;
 const x=t=>L+(t-x0)/(x1-x0||60)*(W-L-R),y=p=>T+(hi-p)/(hi-lo)*(H-T-B),fmt=p=>Number(p).toFixed(2);
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(c.symbol)} rzeczywiste świece M1 do ${esc(ny(c.as_of))}" style="width:100%;background:#10192b;border:1px solid #344154;border-radius:12px">`;
 for(let i=0;i<=5;i++){const p=lo+(hi-lo)*i/5;svg+=`<line x1="${L}" y1="${y(p)}" x2="${W-R}" y2="${y(p)}" stroke="#273449"/><text x="${W-R+8}" y="${y(p)+4}" fill="#cbd5e1" font-size="12">${fmt(p)}</text>`}
 const width=Math.min(12,Math.max(1,(W-L-R)/bars.length*.65));
 for(const b of bars){const xx=x((b.ts+b.end)/2),color=b.close>=b.open?'#30d5a1':'#fb7185';svg+=`<g><title>${esc(b.id)} | ${esc(ny(b.ts))} NY | dostępna ${esc(ny(b.available_at))} | O ${b.open} H ${b.high} L ${b.low} C ${b.close}</title><line x1="${xx}" y1="${y(b.high)}" x2="${xx}" y2="${y(b.low)}" stroke="${color}"/><rect x="${xx-width/2}" y="${y(Math.max(b.open,b.close))}" width="${width}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/></g>`}

 const colors={SWEEP:'#ffaa00',LIQUIDITY_REFERENCE:'#ab9dff',STRUCTURE_BREAK:'#60a5fa',PRE_RAID_CROSS_WITNESS:'#60a5fa',ORIGINAL_CROSS_REFERENCE:'#a1a1aa',DISPLACEMENT:'#f5c86a',FVG:'#20cba4',RETEST:'#f3f4f6',TARGET_CANDIDATE:'#38bdf8',INVALIDATION:'#fb7185'};
 for(const a of c.annotations||[]){if(a.available_at>c.as_of)throw Error('FUTURE_CHART_ANNOTATION');const color=colors[a.kind]||'#e879f9',title=esc(`${a.kind} ${a.observation_id} | ${ny(a.available_at)} | ${a.status}`);
  if(a.lower!=null&&a.upper!=null&&a.upper>=lo&&a.lower<=hi){const xx=x(Math.max(a.available_at,x0));svg+=`<g><title>${title}</title><rect x="${xx}" y="${y(Math.min(hi,a.upper))}" width="${Math.max(0,W-R-xx)}" height="${Math.max(2,y(Math.max(lo,a.lower))-y(Math.min(hi,a.upper)))}" fill="${color}" fill-opacity=".12" stroke="${color}" stroke-dasharray="4 3"/></g>`}
  const touchBar=a.kind==='RETEST'?bars.find(b=>b.id===a.observation_id):null;
  const p=a.price??touchBar?.close;if(p==null||p<lo||p>hi)continue;
  if(['TARGET_CANDIDATE','LIQUIDITY_REFERENCE'].includes(a.kind)){svg+=`<g><title>${title}</title><line x1="${x(Math.max(a.available_at,x0))}" y1="${y(p)}" x2="${W-R}" y2="${y(p)}" stroke="${color}" stroke-dasharray="5 4"/></g>`}
  else if(a.ts>=x0&&a.ts<=x1){const xx=x(a.ts);svg+=`<g><title>${title}</title><circle cx="${xx}" cy="${y(p)}" r="4" fill="${color}"/></g>`}}
 for(let i=0;i<6;i++){const t=x0+(x1-x0)*i/5;svg+=`<text x="${x(t)}" y="${H-15}" text-anchor="middle" fill="#9aaec7" font-size="12">${esc(new Date(t*1000).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}))}</text>`}
 const legend=(c.annotations||[]).filter(a=>!['TARGET_CANDIDATE','LIQUIDITY_REFERENCE'].includes(a.kind)).map(a=>`<span style="color:${colors[a.kind]||'#e879f9'}" title="${esc(a.observation_id)}">${esc(a.kind)} · ${esc(new Date(a.ts*1000).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}))}${a.lower!=null?' · '+a.lower+'–'+a.upper:a.price!=null?' · '+a.price:''}</span>`).join('');
 return svg+'</svg>'+`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin-top:12px;font-size:12px">${legend}</div><p>${bars.length} rzeczywistych świec M1 · dostępne do ${esc(ny(c.as_of))} NY. Najedź na świecę/znacznik, aby sprawdzić ID. Biały znacznik retestu pokazuje zamknięcie jego świecy, nie cenę fillu. Późniejsze wyniki nie wybierają tego przypadku.</p>`;
}
async function detail(id){
 const d=await get('/cases/'+encodeURIComponent(id));
 const defs=new Map((model.conditions||[]).map(c=>[c.id,c]));
 const rows=Array.isArray(d.conditions)?d.conditions:Object.entries(d.conditions||{}).map(([condition_id,c])=>({condition_id,...c}));
 const coords=d.conditional_coordinates||{},targets=d.target_inventory_at_retest||d.target_inventory||[];
 $('#episode6-detail').innerHTML=`<h3>${esc(d.symbol)} SHORT · ${esc(d.status||d.classification)}</h3><p><code>${esc(d.candidate_id||d.id)}</code></p><p>Powstanie FVG / zamrożony kontekst: <b>${esc(ny(d.formation_decision_at||d.context_target_cutoff))} NY</b><br>Koniec pokazanej obserwacji: <b>${esc(ny(d.decision_at||d.as_of))} NY</b>. Późniejszy retest nie jest dowodem dostępnym przy powstaniu FVG.</p>
 <p><b>HISTORICAL_RESEARCH · PENDING_LLM · EXECUTION_BLOCKED</b></p>${chart(d.chart)}
 ${d.structural_review?`<h3>Referencje sprzed raidu — bez wyboru preferowanego dołka</h3><p>Oryginalny wynik: ${esc(d.original_classification)}. Dostępne dowody geometrii: ${d.structural_review.witnesses.length}. Niebieskie znaczniki oznaczają możliwe przełamania, nie potwierdzone MSS.</p><div class="shadow-scroll"><table><thead><tr><th>Referencja / TF</th><th>Cena</th><th>Uformowana / dostępna (NY)</th><th>Pierwsze dostępne przełamanie (NY)</th><th>Status</th></tr></thead><tbody>${d.structural_review.references.map(r=>`<tr><td>${esc(r.id)} · M${r.timeframe}${r.original_selected?' · oryginalna':''}</td><td>${r.price}</td><td>${esc(ny(r.pivot_ts))}<br>${esc(ny(r.available_at))}</td><td>${esc(ny(r.first_cross?.available_at))}</td><td>${esc(r.status)}</td></tr>`).join('')}</tbody></table></div><details><summary>Pełna polityka, powiązania i dowody</summary>${json(d.structural_review)}</details>`:''}
 <p><b>FVG:</b> ${esc((coords.FVG_bounds||[]).join(' – '))}. <b>Odniesienie stopu C2:</b> ${esc(coords.stop_C2?.reference)}; <b>C1 / swing:</b> ${esc(coords.stop_C1?.reference)}. Są to poziomy odniesienia, nie wybrane stopy z buforem. Wejście i preferowany target pozostają nierozstrzygnięte.</p>
 <details><summary>Najbliższe poziomy — ranking liczbowy, nie preferencja ICT (${targets.length} odniesień)</summary><p>Dystans liczony od ${esc(d.target_distance_reference?.price)} — dolnej granicy FVG, nie certyfikowanego wejścia. Dostępność przy powstaniu FVG jest oddzielona od późniejszej konsumpcji; brak wybranego celu transakcji.</p><div class="shadow-scroll"><table><thead><tr><th>Ranking</th><th>Rodzaj / cena</th><th>Dystans</th><th>Dostępny od (NY)</th><th>Stan w końcu obserwacji</th><th>ID</th></tr></thead><tbody>${targets.slice(0,12).map(t=>`<tr><td>${esc(t.numerical_rank)}</td><td>${esc(t.kind)}<br>${esc(t.price)}</td><td>${esc(t.distance_points)}</td><td>${esc(ny(t.available_at))}</td><td>${esc(t.later_availability_status||t.mitigation_status||'SOURCE_PREFERENCE_UNRESOLVED')}</td><td>${esc(t.id)}</td></tr>`).join('')}</tbody></table></div><p>Pokazano pierwsze 12 odniesień; pełny zachowany inwentarz znajduje się poniżej.</p></details>
 <div class="shadow-scroll"><table><thead><tr><th>Warunek / źródło</th><th>Ocena</th><th>Dokładny powód i dowody</th></tr></thead><tbody>${rows.map(c=>{const id=c.condition_id||c.id,def=defs.get(id);return `<tr><td>${esc(id)} · ${esc(def?.title||def?.label||c.title)}${sourceCard(def)}</td><td class="condition-${esc(c.status)}">${esc(c.status)}</td><td>${esc(c.reason||c.explanation||'')}<p>${esc((c.evidence_ids||[]).join(' · '))}</p>${c.measurements?json(c.measurements):''}</td></tr>`}).join('')}</tbody></table></div>
 <details><summary>Warianty stopu, wejście i wszystkie cele — bez domyślnej preferencji</summary>${json({conditional_coordinates:coords,source_qualified_entry:d.entry,source_qualified_stop:d.stop,preferred_target:d.preferred_target,target_distance_reference:d.target_distance_reference,targets_at_formation:d.target_inventory,targets_at_observation_cutoff:d.target_inventory_at_retest,execution_eligible:d.execution_eligible,execution_blockers:d.execution_blockers})}</details>
 <details><summary>Konkurujące zakresy HTF i granice obserwacji</summary>${json({ranges:d.competing_htf_ranges,retest:d.retest_observation,initial_assessment:d.initial_assessment,branches:d.branch_decisions,failed_mandatory_conditions:d.failed_mandatory_conditions,missing_mandatory_data:d.missing_mandatory_data,unresolved_mandatory_conditions:d.unresolved_mandatory_conditions})}</details>
 <details><summary>Adnotacje i oryginalne ID obserwacji</summary>${json(d.chart?.annotations||d.evidence_ids)}</details>`;
 $('#episode6-detail').scrollIntoView({block:'start'});
 const u=new URL(location.href);u.searchParams.set('episode6_case',id);history.replaceState(null,'',u);
}
async function cases(){
 const serial=++requestSerial;
 const q=new URLSearchParams({offset,symbol:$('#episode6-symbol').value,condition:$('#episode6-condition').value,status:$('#episode6-status').value});
 const data=await get('/cases?'+q);if(serial!==requestSerial)return;
 $('#episode6-cases').innerHTML=data.items.map(r=>`<tr><td>${esc(r.symbol)} SHORT<br>${esc(ny(r.decision_at))}</td><td>${esc(r.status)}<br><small>${esc(r.id)}</small></td><td><button data-case="${esc(r.id)}">Warunki i wykres</button></td></tr>`).join('');
 $('#episode6-page').textContent=data.total?`${offset+1}–${offset+data.items.length} z ${data.total}`:'Brak przypadków dla wybranego filtra';$('#episode6-more').disabled=!data.has_more;
 $('#episode6-cases').querySelectorAll('[data-case]').forEach(b=>b.onclick=()=>detail(b.dataset.case).catch(fail));
}
async function load(){
 [summary,model]=await Promise.all([get('/summary'),get('/model')]);
 $('#episode6-state').textContent=`${summary.version||summary.schema||'Episode 6'} · manifest ${summary.manifest_sha256||'—'}`;
 $('#episode6-provenance').innerHTML=`<p class="notice"><b>${esc(model.classification)}</b><br>${esc(model.identity_warning||model.provenance?.warning||'')}</p><p>Źródło: ${sourceLinks([{url:model.source?.url,title:model.source?.title}])} · ${esc(model.source?.actual_modality_reviewed||'Zachowane dowody źródłowe')}.</p>`;
 const counts=summary.population_counts||summary.funnel||{};
 const labels={short_selector_candidates:'Hipotezy selektora SHORT',complete_engineering_chains:'Łańcuchy z FVG',zone_retests_observed:'Zaobserwowane dotknięcia FVG',c3_and_contiguous_retest:'C3 low < C2 low + ciągły retest',decidable_geometry_and_contiguous_retest:'Sprawdzalne warunki geometrii + retest',context_resolved:'Rozstrzygnięty kontekst',fully_defined_source_entry_sl_tp:'Pełny plan wejście / SL / TP',source_qualified:'Kwalifikacja źródłowa',simulated_trades:'Defensywne symulacje'};
 $('#episode6-funnel').innerHTML=Object.entries(labels).map(([k,label])=>`<div><strong>${num(counts[k])}</strong><span>${esc(label)}</span></div>`).join('');
 const raw=summary.condition_funnel||summary.condition_counts||{};
 const byId=Array.isArray(raw)?Object.fromEntries(raw.map(c=>[c.condition_id||c.id,c.counts||c.status_counts||c])):raw;
 $('#episode6-conditions').innerHTML=`<table><thead><tr><th>Warunek i rola</th>${statuses.map(s=>`<th>${esc(s)}</th>`).join('')}</tr></thead><tbody>${(model.conditions||[]).map(c=>{const counts=byId[c.id];return `<tr><td>${esc(c.id)} · ${esc(c.title||c.label)}<br><small>${esc(c.requirement||c.role)} · ${esc(c.scope)}</small></td>${statuses.map(s=>`<td class="condition-${s}">${num(counts?(counts[s]??0):null)}</td>`).join('')}</tr>`}).join('')}</tbody></table>`;
 $('#episode6-condition').innerHTML='<option value="">Wszystkie</option>'+(model.conditions||[]).map(c=>`<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.title||c.label)}</option>`).join('');
 $('#episode6-metrics').innerHTML=`<h3>${esc(summary.pf_status||'PF_NOT_ESTIMABLE')}</h3><p><b>${num(summary.measured_context_differences?.selected_structural_low_failed)} wybranych referencji struktury nie spełnia chronologii źródłowej.</b> Dołek powstały po raidzie nie zastępuje wymaganego dołka sprzed raidu. <b>${num(counts.c3_low_condition_failed)} FVG nie spełnia C3.low &lt; C2.low.</b> Te grupy częściowo się pokrywają.</p><p>Nie wybrano źródłowo uzasadnionego wejścia, wariantu stopu z buforem i preferowanego celu dla kompletnego setupu. Brak kwalifikacji nie dowodzi braku historycznych okazji. Nieoceniona interpretacja nie jest niespełnionym warunkiem rynkowym.</p><p>PF, win rate, średnie R i drawdown pozostają nieobliczone; brak symulowanych transakcji nie oznacza PF = 0.</p><p><b>Uwaga do ciągłości:</b> sześć oznaczonych w przebiegu v1 przerw przypada na 17:00–18:00 NY. Historyczny kalendarz zamknięcia nie jest przypięty do tych dat; to nie jest dowód sześciu awarii feedu. „Ciągły retest” oznacza tu obserwację bez przerwy w zegarowym ciągu M1. Zachowano wynik v1 i niezależne zastrzeżenie do jego interpretacji.</p>`;
 if(summary.structural_comparison){const a=summary.structural_comparison.after,b=summary.original_population_counts;
 $('#episode6-metrics').insertAdjacentHTML('afterbegin',`<h3>Oryginalny wynik v1 → ponowna ocena v2</h3><p><b>${num(a.reference_defect_corrected)} z 309:</b> znaleziono alternatywne referencje sprzed raidu. <b>${num(a.original_failure_still_unresolved)}:</b> brak zgodnego łańcucha w zamrożonym zakresie. <b>${num(a.multiple_witnesses)}:</b> więcej niż jedna referencja; żadnej nie wybrano automatycznie.</p><table><thead><tr><th>Etap</th><th>v1</th><th>v2</th></tr></thead><tbody><tr><td>Referencja sprzed raidu / dostępny dowód alternatywny</td><td>229</td><td>${num(a.with_pre_raid_witness)}</td></tr><tr><td>Sprawdzalna geometria</td><td>${num(b.decidable_mandatory_geometry_verified)}</td><td>${num(a.decidable_mandatory_geometry_verified)}</td></tr><tr><td>Geometria + ciągły retest</td><td>${num(b.decidable_geometry_and_contiguous_retest)}</td><td>${num(a.decidable_geometry_and_contiguous_retest)}</td></tr><tr><td>Kwalifikacja źródłowa / kompletne plany</td><td>0 / 0</td><td>0 / 0</td></tr></tbody></table>`);}
 $('#episode6-limitations').innerHTML=json({limitations:summary.limitations,population_counts:summary.population_counts,intersection_definitions:summary.intersection_definitions,measured_context_differences:summary.measured_context_differences,branches:summary.variant_results||summary.classification_counts,source_branches:model.variants,deduplication:summary.deduplication,chronological_split:summary.chronological_split,by_instrument:summary.by_instrument});
 await cases();const initial=new URLSearchParams(location.search).get('episode6_case');if(initial)await detail(initial);
}
for(const id of ['episode6-symbol','episode6-condition','episode6-status'])$('#'+id).onchange=()=>{offset=0;cases().catch(fail)};
$('#episode6-first').onclick=()=>{offset=0;cases().catch(fail)};$('#episode6-more').onclick=()=>{offset+=25;cases().catch(fail)};
load().catch(fail);
})();
