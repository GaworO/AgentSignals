/* Read-only measured research; null statistics are never rendered as zero. */
(()=>{
if(new URLSearchParams(location.search).get('shadow_run')!=='research')return;
const $=x=>document.querySelector(x), base='/ict-shadow/shadow-mvp/research';
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=x=>x==null?'—':Number(x).toLocaleString('pl-PL');
const ny=t=>t==null?'—':new Date(t*1000).toLocaleString('pl-PL',{timeZone:'America/New_York'});
let data,selected,offset=0;
async function get(p){const r=await fetch(base+p);if(!r.ok)throw Error('HTTP '+r.status+' — pomiar niedostępny');return r.json()}
function failure(e){$('#strategy-research-state').textContent=e.message}
async function source(id){
 const r=await get('/source/'+encodeURIComponent(id));
 $('#research-source-detail').innerHTML=`<h4>${esc(r.model)} · ${esc(r.id)}</h4><p>${esc(r.description)}</p><pre>${esc(JSON.stringify(r.fields,null,2))}</pre><p>Niejasności: ${esc(JSON.stringify(r.ambiguities))}</p>${r.references.map(ref=>{
 let link='';try{const u=new URL(ref.url);if(u.protocol==='https:'&&['youtube.com','www.youtube.com'].includes(u.hostname))link=`<a href="${esc(u.href)}" target="_blank" rel="noopener">${esc(ref.title)} · ${Math.floor(ref.start/60)}:${String(Math.floor(ref.start%60)).padStart(2,'0')}</a>`}catch{}
 return `<p>${link}</p><blockquote>${esc(ref.extracted_text)}</blockquote><small>${esc(ref.source_modality)}</small>`}).join('')}`;
}
function drawRows(){
const family=$('#research-family').value,kind=$('#research-kind').value;
if(selected&&!data.models.some(m=>m.id===selected&&(!family||m.family===family)&&(!kind||m.kind===kind))){selected=null;$('#research-model-detail').innerHTML='';}
$('#research-models').innerHTML=data.models.filter(m=>(!family||m.family===family)&&(!kind||m.kind===kind)).map(m=>`<tr>
<td><button data-model="${esc(m.id)}">${esc(m.name)}</button><br><small>${esc(m.kind)}</small></td>
<td>${number(m.geometry_count)}</td><td>${number(m.selector_candidates)}</td><td>${number(m.complete_geometry_candidates)}</td>
<td>${number(m.source_qualified_count)}</td><td>${number(m.simulation_eligible_count)}<br><small>kwalifikujących się</small></td>
<td><b>${esc(m.pf_status)}</b><br>${number(m.pf)} / ${number(m.win_rate)} / ${number(m.average_r)} / ${number(m.max_drawdown)}</td></tr>`).join('');
$('#research-models').querySelectorAll('[data-model]').forEach(b=>b.onclick=()=>detail(b.dataset.model).catch(failure));
}
function breakdown(title,rows){
if(!rows?.length)return '';
const keys=Object.keys(rows[0]);
return `<details><summary>${esc(title)}</summary><div class="shadow-scroll"><table><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${keys.map(k=>`<td>${esc(r[k]==null?'—':typeof r[k]==='object'?JSON.stringify(r[k]):r[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`;
}
async function occurrences(){
const p=await get('/occurrences?model='+encodeURIComponent(selected)+'&symbol='+encodeURIComponent($('#research-occurrence-symbol').value)+'&offset='+offset);
$('#research-occurrences').innerHTML=p.items.map(r=>`<tr><td>${esc(ny(r.at))}<br>${esc(r.symbol)} ${esc(r.direction)} · ${esc(r.session)}</td><td>${esc(r.entity_kind)}<br><code>${esc(r.id)}</code></td><td>${esc(r.status)}<br>${esc(r.reason)}</td></tr>`).join('');
$('#research-more').disabled=!p.has_more;$('#research-page').textContent=`Rekordy ${offset+1}–${offset+p.items.length}; czas NY`;
}
async function detail(id){
selected=id;offset=0;const m=data.models.find(m=>m.id===id);
const fields=[['Składowe zdarzenia detektora',m.geometry_count],['Projekcje kandydatów selektora',m.selector_candidates],['Sweep → displacement → cross → FVG (retest nieoceniony)',m.complete_geometry_candidates],['Zweryfikowane pełne wymagania źródłowe',m.source_prerequisites_verified_count],['Nierozstrzygnięty kontekst',m.context_unresolved_count],['Wykazane niepowodzenia warunku obowiązkowego',m.mandatory_failed_count],['W pełni określone źródłowe entry/SL/TP',m.fully_source_defined_count],['Wykazane kwalifikacje źródłowe',m.source_qualified_count],['Współczynnik kwalifikacji źródłowej',m.source_qualification_rate],['Dopuszczone do symulacji',m.simulation_eligible_count]];
$('#research-model-detail').innerHTML=`<h3>${esc(m.name)}</h3><p>${esc(m.family)} · ${esc(m.kind)}</p>
<p>To projekcja na zachowane zdarzenia i selektor badawczy. Brak niezależnego detektora danego modelu oznacza brak pomiaru jego pełnej częstotliwości.</p>
<table><tbody>${fields.map(([k,v])=>`<tr><th>${esc(k)}</th><td>${number(v)}</td></tr>`).join('')}</tbody></table>
<h4>Dlaczego nie ma PF / czego brakuje</h4><ul>${(m.limitations||[]).map(x=>`<li>${esc(typeof x==='object'?JSON.stringify(x):x)}</li>`).join('')}</ul>
<p>PF ${number(m.pf)} · wynik netto ${number(m.net_pnl)} · win rate ${number(m.win_rate)} · średnie R ${number(m.average_r)} · expectancy ${number(m.expectancy)} · max drawdown ${number(m.max_drawdown)}. Brak dopuszczonej symulacji oznacza brak krzywej kapitału, nie płaski wynik.</p>
<p><b>Źródłowe ID reguł:</b> ${(m.source_rule_ids||[]).map(x=>`<button data-source="${esc(x)}">${esc(x)}</button>`).join(' · ')||'Nie przypisano zweryfikowanej reguły.'}</p><div id="research-source-detail"></div>
${Object.entries(m.breakdowns||{}).map(([k,v])=>breakdown(k,v)).join('')}
<h4>Rzeczywiste rekordy pomiaru</h4><label>Instrument <select id="research-occurrence-symbol"><option value="">Wszystkie</option><option>NQ</option><option>MNQ</option><option>ES</option></select></label>
<span id="research-page"></span> <button id="research-more">Następne 50</button>
<div class="shadow-scroll"><table><thead><tr><th>Czas / instrument</th><th>Typ / ID obserwacji</th><th>Stan / powód</th></tr></thead><tbody id="research-occurrences"></tbody></table></div>`;
$('#research-more').onclick=()=>{offset+=50;occurrences().catch(failure)};
$('#research-model-detail').querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>source(b.dataset.source).catch(failure));
$('#research-occurrence-symbol').onchange=()=>{offset=0;occurrences().catch(failure)};
await occurrences();$('#research-model-detail').scrollIntoView({block:'start'});
}
async function load(){
data=await get('/summary');
const s=data.scope,t=data.totals;
$('#strategy-research-state').textContent=`${data.version} · ${s.first_ny} → ${s.last_ny} · manifest ${data.manifest_sha256}`;
const cards=[['Świece historyczne',Object.values(s.bars_by_symbol||{}).reduce((a,b)=>a+b,0)],['Zdarzenia detektora',t.events],['Hipotezy selektora',t.candidates],['Łańcuchy do FVG',t.complete_engineering_sequences],['Wykazane kwalifikacje',t.source_qualified],['Symulowane transakcje',t.simulated_trades]];
$('#strategy-research-totals').innerHTML=cards.map(([label,v])=>`<div><strong>${number(v)}</strong><span>${esc(label)}</span></div>`).join('');
$('#strategy-research-coverage').innerHTML=`<p><b>Zakres:</b> ${Object.entries(s.bars_by_symbol||{}).map(([k,v])=>esc(k)+' '+number(v)).join(' · ')}. Dane historyczne, nie bieżące prognozy.</p>
<p><b>Ekonomia kontraktów:</b> ${s.contracts_verified?'zweryfikowana dla wskazanej serii':'mapowanie kontraktu i korekt serii niezweryfikowane; ceny nie wystarczają do wiarygodnego PF po kosztach'}.</p>
<p>Po grupowaniu skorelowanej ekspozycji: <b>${number(t.candidate_exposure_clusters)}</b> grup kandydatów. Zachowano osobne obserwacje i ceny NQ/MNQ; nie są to niezależne okazje.</p>
<details><summary>Chronologiczny podział i deduplikacja</summary><pre>${esc(JSON.stringify(data.chronological_split,null,2))}</pre><pre>${esc(JSON.stringify(data.deduplication||data.market_frequency||{},null,2))}</pre><p>Podział danych nie jest dowodem wykonania walidacji OOS, gdy brak dopuszczonej strategii.</p></details>`;
for(const [sel,key]of [['#research-family','family'],['#research-kind','kind']]){const old=$(sel).value;$(sel).innerHTML='<option value="">Wszystkie</option>'+[...new Set(data.models.map(m=>m[key]))].map(k=>`<option value="${esc(k)}">${esc(k)}</option>`).join('');$(sel).value=old;$(sel).onchange=drawRows}
$('#research-limitations').innerHTML=`<ul>${(data.limitations||[]).map(x=>`<li>${esc(typeof x==='object'?JSON.stringify(x):x)}</li>`).join('')}</ul><p>Świadectwa źródłowe są osobnym zbiorem; ich obecność nie dowodzi spełnienia warunków na rynku.</p><a href="${base}/sources" target="_blank">Taksonomia z dokładnymi odnośnikami źródłowymi</a> · <a href="${base}/m1-review" target="_blank">Ocena pierwszych 11 sweepów M1</a>`;
drawRows();
const tax=await get('/sources');
$('#research-source-programs').innerHTML=`<p>${tax.library_summary.sources} transkrypcji · ${tax.library_summary.navigation_families} grup wiedzy · ${tax.source_local_assemblies.length} źródłowych programów badawczych. To nie dodatkowe, niezależnie policzone strategie. Częstotliwość i PF pozostałych programów: <b>NOT_MEASURED / PF_NOT_ESTIMABLE</b>.</p>${tax.source_local_assemblies.map(p=>`<details><summary>${esc(p.id)} · ${esc(p.readiness)}</summary><p>${esc(p.sequence)}</p><p>${esc(p.branch_limitations)}</p><p>Nieustalone: ${esc(p.unresolved.join('; '))}</p><p>Brakujące etapy w tej definicji: ${esc(p.missing_stages.join(', ')||'Brak pustych etapów nie rozstrzyga niejasności semantycznych.')}.</p><p>Nie jest to stwierdzenie braku reguły w całym korpusie. ${p.source_video_ids.map(id=>`<a target="_blank" rel="noopener" href="https://www.youtube.com/watch?v=${encodeURIComponent(id)}">${esc(id)}</a>`).join(' · ')}</p></details>`).join('')}`;
}
$('#research-refresh').onclick=()=>load().catch(failure);
load().catch(failure);
})();
