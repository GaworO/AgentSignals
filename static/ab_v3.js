"use strict";
let snapshot=null;
const el=id=>document.getElementById(id);
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':Number(v).toFixed(3);
const utc=v=>v?new Date(Number(v)).toISOString():'—';
const card=(label,value,note='')=>`<div class="card"><small>${esc(label)}</small><strong>${esc(value)}</strong><p>${esc(note)}</p></div>`;
const percent=v=>v==null?'—':(100*Number(v)).toFixed(1)+'%';
function exitLabel(x){const s=x.status;if(s==='OPEN'||s==='MONITORING')return x.research?.ne2||x.research?.opposing_displacement||x.research?.mss?'WARNING':'WAIT';if(s==='EXIT_SIGNALLED')return 'EXIT SIGNAL';if(s==='EXIT_REQUESTED'||s==='EXIT_ACKNOWLEDGED')return 'EXIT REQUESTED';if(s==='EXIT_FILLED'||s==='CLOSED')return 'EXIT FILLED';if(s==='EXIT_BLOCKED'||s==='EXIT_REJECTED'||s==='BROKER_MISMATCH'||s==='STALE_DATA')return 'BLOCKED';return s;}
function renderExits(ex){
 if(!ex)return;
 el('exit-title').textContent=ex.title;
 el('exit-mode').textContent=`Exit mode: ${ex.mode?.toUpperCase()} · real execution flag: ${ex.real_execution} · historical parity: ${ex.historical_parity?'PASS':'FAIL'} · Model 0: ${ex.runner_status||'UNKNOWN'} · managers: ${Object.entries(ex.enabled||{}).filter(([,v])=>v).map(([k])=>k).join(', ')||'NONE'}${ex.blockers?.length?' · Blockers: '+ex.blockers.join(', '):''}`;
 const m=ex.metrics||{};
 el('exit-metrics').innerHTML=[['TRADES',m.trades],['WR',percent(m.win_rate)],['PF',num(m.pf)],['NET',m.net_pnl==null?'—':'$'+num(m.net_pnl)],['EXPECTANCY',m.expectancy==null?'—':'$'+num(m.expectancy)],['DD',m.max_dd==null?'—':'$'+num(m.max_dd)],['EARLY EXIT RATE',percent(m.exit_rate)],['AVG EXIT R',num(m.avg_exit_r)]].map(([a,b])=>card(a,b)).join('');
 el('exit-open').innerHTML=table(['TRADE','DIR','ENTRY','CURRENT R','MFE','ACTIVE FVG','NE2','OPP DISP','MSS','M1','M2','M3','RUNNER','P(3R)','BROKER TP','EXIT STATUS','EVIDENCE'],(ex.open||[]).map(x=>{const s=x.research||{},t=s.signals||{};return [x.trade_id,x.direction,num(x.entry),num(s.current_r),num(s.mfe_r),s.active_fvg?`${num(s.active_fvg.lower)}–${num(s.active_fvg.upper)}`:'—',s.ne2,s.opposing_displacement,s.mss,t.manager_status?.M1,t.manager_status?.M2,t.manager_status?.M3,x.runner?.decision_kind,num(x.runner?.probability),num(x.broker_tp),exitLabel(x),x.error_reason||JSON.stringify(s.first_events||{})];}));
 el('exit-completed').innerHTML=table(['DATE','TRADE ID','DIR','SESSION','ENTRY','ORIGINAL SL','ORIGINAL TP','BROKER TP','RUNNER','REAL EXIT TIME','REAL EXIT PRICE','REAL EXIT R','REAL P&L','TRIGGERED BY','PRIMARY REASON','ALL MANAGERS','ALL REASONS','M1','M2','M3','BASELINE TP2R/SL','DELTA VS BASELINE','STATUS'],(ex.completed||[]).map(x=>[utc(x.fill_time),x.trade_id,x.direction,x.session,num(x.entry),num(x.original_sl),num(x.original_tp),num(x.broker_tp),x.runner?.decision_kind,utc(x.fill_time),num(x.fill_price),num(x.exit_r),x.exit_pnl==null?'—':'$'+num(x.exit_pnl),x.primary_manager,x.primary_reason,(x.triggered_managers||[]).join('+'),(x.triggered_reasons||[]).join('+'),(x.triggered_managers||[]).includes('M1'),(x.triggered_managers||[]).includes('M2'),(x.triggered_managers||[]).includes('M3'),x.baseline_outcome,num(x.delta_vs_baseline),x.status]));
 el('exit-reasons').innerHTML=table(['REASON','EXITS','WINNERS','LOSERS','AVG EXIT R','AVG PNL','PF CONTRIBUTION','EXPLANATION'],(ex.reason_stats||[]).map(x=>[x.reason,x.exits,x.winners,x.losers,num(x.avg_exit_r),num(x.avg_pnl),num(x.pf_contribution),x.explanation]));
 el('exit-contribution').innerHTML=Object.entries(ex.manager_contribution||{}).map(([k,v])=>card(k+' involved',v)).join('')+Object.entries(ex.overlap||{}).map(([k,v])=>card(k,v,'same-bar participation')).join('');
 const cuts=[];for(const [kind,rows] of Object.entries(ex.cuts||{}))for(const x of rows)cuts.push([kind,x.bucket,x.trades,percent(x.win_rate),num(x.pf),num(x.net_pnl),num(x.expectancy),num(x.max_dd),percent(x.exit_rate)]);
 el('exit-cuts').innerHTML=table(['CUT','BUCKET','TRADES','WR','PF','NET','EXPECTANCY','DD','EARLY EXIT RATE'],cuts);
}
function table(headers,rows){return '<table><thead><tr>'+headers.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+(rows.length?rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join(''):'<tr><td colspan="'+headers.length+'">Brak rekordów — to nie oznacza braku feedu.</td></tr>')+'</tbody></table>';}
function nearest(tag){return (tag?.open_directional_liquidity_pools||[]).filter(x=>x.status==='OPEN').sort((a,b)=>a.distance_points-b.distance_points)[0];}
function poolLabel(pool){return pool?(pool.constituent_levels||[]).map(x=>x.kind+' '+x.price).join(' · '):'Brak znanego OPEN poziomu';}
function renderTrade(){
 const o=(snapshot?.orders||[]).find(x=>x.order_id===el('position').value);
 if(!o){el('trade').innerHTML=card('Manager','WAITING_BROKER_FILL','Brak potwierdzonej pozycji. Nie używamy modelowego fillu.');el('layers').innerHTML='';el('decision').textContent='Nie wykonano decyzji dla potwierdzonej pozycji.';return;}
 const d=o.last_decision,m=d?.measurements||{},s=o.snapshot||{},dol=d?.frozen_dol||s.frozen_dol||{},p=o.broker_position;
 el('trade').innerHTML=card('Broker state',o.state,o.order_id)+card('Entry / SL / TP',`${o.entry_price} / ${o.stop_price} / ${o.target_price}`,`${o.direction} · TP 2R · Qty ${o.quantity??'—'}`)+card('Źródło '+(s.source_side||'BSL/SSL'),s.source_level,s.source_name)+card('DOL FROZEN',dol.price,dol.state+' · '+(dol.id||'UNKNOWN'))+card('Broker net $',p?.realized_net_usd,'Tylko potwierdzony wynik brokera, nie model.')+card('Fill brokera',p?.fill_ms?utc(p.fill_ms):'Niepotwierdzony',p?.position_id);
 const rows=[['1s → M1','speed / ATR20',num(m.speed),'ER60 '+num(m.efficiency)+' · RV '+num(m.rv),'Nie wyznacza sam wejścia ani zamknięcia'],['M1','progress / MFE',num(m.progress)+' / '+num(m.mfe),'weak '+(m.weak_streak??'—')+' / wymagane '+(d?.required_streak??'—'),'BE return: '+(m.be_returned??'—')+' · source failed: '+(m.source_failed??'—')]];
 for(const tf of ['M5','M15','H1']){const c=d?.context?.[tf]||{};rows.push([tf,'Kontekst + potwierdzony pivot',c.vote==null?'UNKNOWN':c.vote===1?'SUPPORT':c.vote===-1?'OPPOSING':'NEUTRAL',`Protected ${c.protected??'—'} · broken ${c.broken??'UNKNOWN'}`,c.available_at||'Brak pełnej zamkniętej świecy']);}
 el('layers').innerHTML=table(['Warstwa','Pomiar','Stan','Szczegóły','Dostępność / znaczenie'],rows);
 el('decision').textContent=d?`${utc(d.decision_ms)} · ${d.cause} · ${d.action_state}. Przeciwne TF: ${d.opposition??'—'}, zgodne: ${d.support??'—'}. DOL dostarczony: ${m.dol_delivered??'UNKNOWN'}.`:'Oczekiwanie na potwierdzenie fillu i pełne dane do decyzji M1.';
}
function renderCandidates(){
 const all=snapshot?.candidates||[];
 el('candidate-metrics').innerHTML=card('Ostatnie rekordy',all.length,'Maksymalnie 120 potwierdzonych kandydatur V3')+card('Forward eligible',all.filter(c=>c.forward_eligible).length,'Sygnały po rozgrzewce detektora')+card('Eligible',all.filter(c=>c.eligible).length,'Poprawna geometria wejścia i ryzyka')+card('Rejected',all.filter(c=>!c.eligible).length,'Odrzucone przez detektor');
 const side=el('candidate-side').value,status=el('candidate-status').value;
 const rows=all.filter(c=>(!side||(c.direction||c.dir)===side)&&(!status||(status==='forward'?c.forward_eligible:status==='ready'?c.eligible:!c.eligible)));
 el('candidates').innerHTML=table(['Czas UTC','Candidate ID','Kierunek','Status','Etap','Forward','Powód','BSL / SSL','Entry','SL · STRUCT','TP · 2R','DOL FROZEN'],rows.map(c=>{const s=c.v3_snapshot||{},src=c.source_event||{};return [utc(c.decision_ms||c.entry_ms||c.trigger_ms||c.bos_ms),c.candidate_id,c.direction||c.dir,c.status,c.stage,c.forward_eligible?'YES':'NO',c.rejection_reason,s.source_level??src.bsl_price??src.ssl_price,c.final_entry,c.final_structural_sl,c.policy_B_target,s.frozen_dol?.price];}));
}
function render(x){
 snapshot=x;
 renderExits(x.exits);
 el('health').innerHTML=card('Konto / route',x.account_label,x.route_id)+card('V3 mode',x.mode,x.policy)+card('Kontrakt',x.contract,'Jawny kontrakt feedu i brokera')+(x.feeds||[]).map(f=>card('Feed '+f.tf,f.state,`Age ${num(f.age_seconds)}s · ${f.rows} bars`)).join('');
 el('blockers').textContent=(x.blockers||[]).length?'LIVE blokery: '+x.blockers.join(' · '):'Konfiguracja LIVE gotowa. Zamykanie nadal wymaga świeżej, przypisanej pozycji brokera.';
 const market=x.market,L=market?.LONG,S=market?.SHORT,bsl=nearest(L),ssl=nearest(S);
 el('market').innerHTML=card('Aktualna OPEN BSL',bsl?.pool_price,poolLabel(bsl))+card('Aktualna OPEN SSL',ssl?.pool_price,poolLabel(ssl))+card('DOL rynku LONG',L?.current_dol?.pool_price,L?.dol_status+' · '+poolLabel(L?.current_dol))+card('DOL rynku SHORT',S?.current_dol?.pool_price,S?.dol_status+' · '+poolLabel(S?.current_dol));
 el('market-age').textContent=market?`Skan dostępny na ${utc(market.evaluated_at_ms)} · to nie jest DOL zamrożony dla pozycji · kontrakt archiwum: ${market.contract_identity}`:'Oczekiwanie na skan detektora. DOL nie jest jeszcze znany.';
 const selected=el('position').value;
 el('position').innerHTML=(x.orders||[]).map(o=>`<option value="${esc(o.order_id)}">${esc(o.direction+' · '+o.state+' · '+o.order_id)}</option>`).join('');
 if((x.orders||[]).some(o=>o.order_id===selected))el('position').value=selected;
 renderTrade();
 renderCandidates();
 el('logs').innerHTML=table(['Decyzja UTC','Order','Decyzja','Akcja','Speed ATR','ER60','Progress R','MFE R','DOL delivered','M5 / M15 / H1'],(x.decisions||[]).map(d=>[utc(d.decision_ms),d.order_id,d.cause,d.action_state,num(d.measurements?.speed),num(d.measurements?.efficiency),num(d.measurements?.progress),num(d.measurements?.mfe),d.measurements?.dol_delivered,['M5','M15','H1'].map(tf=>d.context?.[tf]?.vote??'UNKNOWN').join(' / ')]));
 el('actions').innerHTML=table(['Decyzja UTC','Order','Action ID','Stan'],(x.actions||[]).map(a=>[utc(a.decision_ms),a.order_id,a.action_id,a.state]));
 el('refresh').textContent='Aktualizacja '+new Date().toLocaleTimeString();
}
el('position').addEventListener('change',renderTrade);
el('candidate-side').addEventListener('change',renderCandidates);
el('candidate-status').addEventListener('change',renderCandidates);
function showTab(which,updateUrl=true){
 const tab=['main','candidates','exits'].includes(which)?which:'main';
 for(const name of ['main','candidates','exits']){el(name+'-view').hidden=name!==tab;el('tab-'+name).classList.toggle('active',name===tab);}
 if(updateUrl){const url=new URL(location.href);if(tab==='main')url.searchParams.delete('tab');else url.searchParams.set('tab',tab);history.replaceState(null,'',url);}
}
el('tab-main').addEventListener('click',()=>showTab('main'));
el('tab-candidates').addEventListener('click',()=>showTab('candidates'));
el('tab-exits').addEventListener('click',()=>showTab('exits'));
showTab(new URLSearchParams(location.search).get('tab'),false);
async function load(){try{const r=await fetch('/ab/v3/data',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json());el('candidate-warning').hidden=true;}catch(e){el('refresh').textContent='Dane niedostępne: '+e.message;el('blockers').textContent='Brak świeżego odczytu panelu — nie traktuj poprzedniego stanu jako aktualnego.';el('candidate-warning').textContent='Brak świeżego odczytu kandydatów — poprzedni stan może być nieaktualny.';el('candidate-warning').hidden=false;}}
load();setInterval(load,5000);
