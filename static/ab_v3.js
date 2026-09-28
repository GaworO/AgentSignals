"use strict";
let snapshot=null;
const el=id=>document.getElementById(id);
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':Number(v).toFixed(3);
const utc=v=>v?new Date(Number(v)).toISOString():'—';
const card=(label,value,note='')=>`<div class="card"><small>${esc(label)}</small><strong>${esc(value)}</strong><p>${esc(note)}</p></div>`;
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
function render(x){
 snapshot=x;
 const micro=x.micro||{},tv=x.tv_feed||{};
 el('micro').innerHTML=card('TV paczki',tv.state,tv.symbol)+card('Most danych V3',tv.v3_bridge?.v3_data_bridge||'Brak połączenia',tv.v3_bridge?.v3_contract)+card('Ostatnie 1s',micro.observed_seconds,`Do ${utc(micro.end_ms)} · ciągłość ${micro.complete??'UNKNOWN'}`)+card('Displacement / ER',`${num(micro.displacement_points)} pts / ${num(micro.efficiency)}`,`Volume ${micro.volume??'—'}`);
 el('health').innerHTML=card('Konto / route',x.account_label,x.route_id)+card('V3 mode',x.mode,x.policy)+card('Kontrakt',x.contract,'Jawny kontrakt feedu i brokera')+(x.feeds||[]).map(f=>card('Feed '+f.tf,f.state,`Age ${num(f.age_seconds)}s · ${f.rows} bars`)).join('');
 el('blockers').textContent=(x.blockers||[]).length?'LIVE blokery: '+x.blockers.join(' · '):'Brak wykrytych blokad konfiguracji; nie jest to potwierdzenie gotowości LIVE.';
 const market=x.market,L=market?.LONG,S=market?.SHORT,bsl=nearest(L),ssl=nearest(S);
 el('market').innerHTML=card('Aktualna OPEN BSL',bsl?.pool_price,poolLabel(bsl))+card('Aktualna OPEN SSL',ssl?.pool_price,poolLabel(ssl))+card('DOL rynku LONG',L?.current_dol?.pool_price,L?.dol_status+' · '+poolLabel(L?.current_dol))+card('DOL rynku SHORT',S?.current_dol?.pool_price,S?.dol_status+' · '+poolLabel(S?.current_dol));
 el('market-age').textContent=market?`Skan dostępny na ${utc(market.evaluated_at_ms)} · to nie jest DOL zamrożony dla pozycji · kontrakt archiwum: ${market.contract_identity}`:'Oczekiwanie na skan detektora. DOL nie jest jeszcze znany.';
 const selected=el('position').value;
 el('position').innerHTML=(x.orders||[]).map(o=>`<option value="${esc(o.order_id)}">${esc(o.direction+' · '+o.state+' · '+o.order_id)}</option>`).join('');
 if((x.orders||[]).some(o=>o.order_id===selected))el('position').value=selected;
 renderTrade();
 el('candidates').innerHTML=table(['Czas UTC','Klasa','Kierunek','Etap / status','BSL / SSL','Entry','SL · STRUCT','TP · 2R','DOL FROZEN'],(x.candidates||[]).map(c=>{const s=c.v3_snapshot||{},src=c.source_event||{};return [utc(c.entry_ms||c.trigger_ms||c.bos_ms),c.strategy||'Source trigger',c.dir||c.direction,c.stage||c.status||(c.eligible?'LIMIT READY':c.rejection_reason),s.source_level??src.bsl_price??src.ssl_price,c.final_entry,c.final_structural_sl,c.policy_B_target,s.frozen_dol?.price];}));
 el('logs').innerHTML=table(['Decyzja UTC','Order','Decyzja','Akcja','Speed ATR','ER60','Progress R','MFE R','DOL delivered','M5 / M15 / H1'],(x.decisions||[]).map(d=>[utc(d.decision_ms),d.order_id,d.cause,d.action_state,num(d.measurements?.speed),num(d.measurements?.efficiency),num(d.measurements?.progress),num(d.measurements?.mfe),d.measurements?.dol_delivered,['M5','M15','H1'].map(tf=>d.context?.[tf]?.vote??'UNKNOWN').join(' / ')]));
 el('actions').innerHTML=table(['Decyzja UTC','Order','Action ID','Stan'],(x.actions||[]).map(a=>[utc(a.decision_ms),a.order_id,a.action_id,a.state]));
 el('refresh').textContent='Aktualizacja '+new Date().toLocaleTimeString();
}
el('position').addEventListener('change',renderTrade);
async function load(){try{const r=await fetch('/ab/v3/data',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json());}catch(e){el('refresh').textContent='Dane niedostępne: '+e.message;el('blockers').textContent='Brak świeżego odczytu panelu — nie traktuj poprzedniego stanu jako aktualnego.';}}
load();setInterval(load,5000);
