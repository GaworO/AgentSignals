'use strict';
const titles={overview:'Trading overview',candidates:'Trade candidates',context:'AI context',automatic:'Automatic plans',plans:'AI plan readiness',executions:'50K Builder executions',feed:'Market data'};
if(new URLSearchParams(location.search).get('embed')==='1')document.documentElement.classList.add('embedded');
function route(){const key=location.hash.slice(1) in titles?location.hash.slice(1):'overview';document.querySelectorAll('.view').forEach(e=>e.hidden=e.id!==key);document.querySelectorAll('nav a').forEach(e=>e.classList.toggle('active',e.hash==='#'+key));document.getElementById('title').textContent=titles[key];document.getElementById('section-label').textContent=key.toUpperCase();}
window.addEventListener('hashchange',route);route();
const text=(id,value)=>{const node=document.getElementById(id);if(node)node.textContent=value;};
const at=t=>t?new Date(t*1000).toLocaleString('en-GB',{timeZone:'America/New_York',hour12:false}):'—';
function rows(id,items){const target=document.getElementById(id);target.replaceChildren();for(const cells of items){const tr=document.createElement('tr');for(const value of cells){const td=document.createElement('td');td.textContent=String(value);tr.appendChild(td);}target.appendChild(tr);}}
function show(d){
 showDataHealth(d.data_health);
 showAccount(d.account_snapshot);
 for(const s of ['ES','MNQ','NQ']){const f=d.feeds[s]||{state:'NO_DATA',count:0,latest_close:null},p=s.toLowerCase();text(p+'-state',f.state==='NO_DATA'?'No data':f.state==='CURRENT'?'Receiving':d.data_health?.scheduled_pause?'Scheduled pause':'Data delayed');text(p+'-info',f.count+' bars · '+(f.latest_close?at(f.latest_close)+' NY':'Waiting for TradingView'));}
 text('market-roles','AI reviews still use ES/MNQ. Automatic plans use ES/NQ with independent MNQ pricing; they appear in Automatic plans.');
 const paired=d.feeds.ES.count>0&&d.feeds.MNQ.count>0;text('progress-feed',paired?'Both markets have stored candles':'Waiting for both one-minute feeds');
 rows('candidate-rows',d.candidates.map(c=>[at(c.as_of),c.direction.toUpperCase(),c.timeframe+'m',c.lower+' – '+c.upper,'Needs context']));document.getElementById('candidate-empty').hidden=d.candidates.length>0;
 showAI(d.ai);
 showPlans(d.plan_observer);
 showAutomatic(d.automatic_selection);
 showConnectionTest(d.connection_test,d.test_csrf);
 const c=d.latest_context;document.getElementById('context-empty').hidden=!!c;const dl=document.getElementById('context-summary');dl.replaceChildren();
 if(c){const p=c.packet;for(const [k,v] of [['Market cutoff',at(p.as_of)+' NY'],['Inputs frozen',at(c.frozen_at)+' NY'],['Packet ID',p.packet_id],['Evidence items',Object.keys(p.evidence).length],['Processing lag',c.processing_delay_seconds+' seconds']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;dl.append(dt,dd);}rows('coverage',['ES','MNQ'].map(s=>[s,c.coverage[s]['1'],c.coverage[s]['5'],c.coverage[s]['60'],c.coverage[s]['240']]));text('packet',JSON.stringify(c,null,2));text('warmup',c.warmup==='PARTIAL_HISTORY'?'Partial history: fewer than three complete 4h bars in at least one market. No context approval.':'Higher-timeframe bars are present. News, contract alignment and strategy judgment still need validation.');text('progress-context','Latest snapshot: '+at(p.as_of)+' NY');}
 rows('job-rows',d.jobs.map(j=>[at(j.cutoff),j.status+(j.error?' · '+j.error:''),j.processed_at?Math.round(j.processed_at-j.cutoff)+'s':'—']));
 const ul=document.getElementById('diagnostics');ul.replaceChildren();for(const item of d.diagnostics){const li=document.createElement('li');li.textContent=at(item.at)+' · '+item.kind+' · '+item.message;ul.appendChild(li);}if(!d.diagnostics.length){const li=document.createElement('li');li.textContent='No recent intake errors recorded.';ul.appendChild(li);}
}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error('HTTP '+r.status);show(await r.json());text('connection','Service connected · refreshed '+new Date().toLocaleTimeString()+' · market times shown in New York');document.getElementById('connection').className='';}catch(e){showDataHealth({level:'error',messages:['Cannot reach the service. Displayed prices and decisions may be stale.']});text('connection','Cannot refresh the service. Displayed data may be old. '+e.message);document.getElementById('connection').className='error';}finally{setTimeout(refresh,10000);}}
refresh();

function showAI(a){
 if(!a)return;
 const labels={DISABLED:'Disabled',MISSING_API_KEY:'API key needed',MISSING_MODEL:'Model needed',CONFIG_ERROR:'Check configuration',WAITING_FOR_PAIRED_DATA:'Waiting for feeds',WAITING_FOR_FRESH_DATA:'Waiting for fresh data',WAITING_FOR_HISTORY:'Warming up history',WAITING_FOR_CONTIGUOUS_DATA:'Waiting for complete data',OUTSIDE_REVIEW_WINDOW:'Outside review window',READY:'Ready for review',REVIEW_IN_PROGRESS:'Review in progress',PAUSED_AFTER_ERROR:'Paused after error',DAILY_LIMIT_REACHED:'Daily limit reached',WAITING_FOR_INTERVAL:'Waiting for next review',ALREADY_REVIEWED:'Snapshot reviewed',INPUT_TOO_LARGE:'Context too large'};
 const label=labels[a.status]||a.status;
 text('ai-state',label);text('ai-context-state',label);text('ai-info',(a.model||'No model configured')+' · '+a.calls_today+'/'+a.daily_limit+' calls today');text('progress-ai',label+' · observation only');
 text('ai-budget',a.review_window+' · at least '+a.min_interval_minutes+' minutes between reviews · '+a.calls_today+'/'+a.daily_limit+' daily attempts · maximum '+a.max_output_tokens+' output tokens per call.');
 text('ai-help',a.status==='PAUSED_AFTER_ERROR'?'Inspect the latest audit and fix the cause. Then increase TANJA_AI_REVISION in Railway to resume. Failed attempts still count toward the daily limit.':a.status==='WAITING_FOR_HISTORY'?'Collect at least three complete 4h bars and three 1h bars for BOTH markets. This normally requires overnight collection; missing earlier history remains a limitation.':'Scheduled context reviews · no broker orders.');
 const target=document.getElementById('ai-rows');target.replaceChildren();
 for(const r of a.records){const tr=document.createElement('tr');for(const v of [at(r.cutoff),at(r.finished),r.status+(r.error?' · '+r.error:''),r.usage?((r.usage.input_tokens??'?')+' in / '+(r.usage.output_tokens??'?')+' out'):'—']){const td=document.createElement('td');td.textContent=v;tr.appendChild(td);}const td=document.createElement('td'),link=document.createElement('a');link.href='/api/ai/audit/'+encodeURIComponent(r.id);link.target='_blank';link.rel='noopener';link.textContent='Open JSON';td.appendChild(link);tr.appendChild(td);target.appendChild(tr);}
 document.getElementById('ai-empty').hidden=!!a.records.length;
 const latest=a.records[0];
 text('ai-rationale',latest?latest.status==='validated'?latest.decision.rationale:'Latest attempt: '+latest.status+' · '+(latest.error||'waiting for response'):'No model answer yet.');
 text('ai-decision',latest?JSON.stringify({market_cutoff:latest.cutoff,available_at:latest.available_at,executable:false,decision:latest.decision,checks:latest.review},null,2):'No response yet.');
}

let testCSRF='',testBusy=false;
const testButton=document.getElementById('run-test');
function showConnectionTest(c,csrf){
 if(!c)return;
 testCSRF=csrf||'';
 const labels={NOT_CONFIGURED:'Not connected',INVALID_WEBHOOK_URL:'Check webhook URL',EXPLICIT_MNQ_CONTRACT_REQUIRED:'Set MNQ contract',TEST_READY:'Ready for connection test',TEST_RECEIVED:'Test signal received',TEST_REJECTED:'Test signal rejected',SENDING:'Sending test',UNKNOWN:'Test outcome unknown'};
 const label=labels[c.status]||c.status;
 text('execution-state',label);text('execution-info','Test mode only · broker orders disabled');
 text('test-state',label);text('test-config',c.configured?'Dedicated test webhook configured · '+c.contract+' · verify account mapping in TradersPost.':'Set TANJA_TRADERSPOST_TEST_WEBHOOK_URL and TANJA_TEST_CONTRACT in Railway.');
 testButton.disabled=testBusy||!c.configured||c.status==='SENDING';
 rows('test-rows',c.records.map(r=>[at(r.started),r.status+(r.error?' · '+r.error:''),r.receipt?.id||'—','None — test mode']));
 document.getElementById('test-empty').hidden=!!c.records.length;
 text('test-audit',c.records.length?JSON.stringify(c.records[0],null,2):'No test yet.');
}
testButton.addEventListener('click',async()=>{
 if(testBusy||testButton.disabled)return;
 testBusy=true;testButton.disabled=true;text('test-result','Sending a test signal to TradersPost. No broker order will be sent.');
 try{
  const request_id=crypto.randomUUID().replaceAll('-','');
  const response=await fetch('/api/connection/test',{method:'POST',headers:{'Content-Type':'application/json','X-Tanja-CSRF':testCSRF},body:JSON.stringify({request_id})});
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'Test request failed');
  text('test-result',result.status==='test_received'?'TradersPost acknowledged the test signal. Check its Signals log and account subscription. No broker order was sent.':'Test result: '+result.status+' · '+(result.error||result.receipt?.messageCode||'Inspect test history'));
 }catch(e){text('test-result',e.message+' — inspect test history before trying again.');}
 finally{testBusy=false;try{const response=await fetch('/api/state',{cache:'no-store'});if(response.ok)show(await response.json());}catch(e){text('test-result','Cannot refresh test status. Reload and inspect history before retrying.');}}
});

function showPlans(p){
 if(!p)return;
 const names={AI_NOT_VALIDATED:'AI review not validated',ABSTAINED:'No trade proposed',CONTEXT_NOT_READY:'Context incomplete',UNSUPPORTED_VARIANT:'Unsupported setup',UNSUPPORTED_ENTRY_MODE:'Entry method unsupported',PLAN_INCOMPLETE:'Plan incomplete',RULE_REVIEW_REQUIRED:'Rules need review',AUDIT_REJECTED:'Evidence check failed'};
 rows('plan-rows',p.records.map(r=>[at(r.market_cutoff),at(r.available_at),names[r.state]||r.state,[...(r.reasons||[]),...(r.missing_fields||[]).map(s=>'Missing: '+s.replaceAll('_',' '))].join(' ')]));
 document.getElementById('plan-empty').hidden=p.records.length>0;
 for(const [id,list] of [['strategy-gaps',p.strategy_gaps],['automation-gaps',p.automation_gaps]]){const node=document.getElementById(id);node.replaceChildren();for(const message of list){const li=document.createElement('li');li.textContent=message;node.appendChild(li);}}
}

function showAutomatic(a){
 if(!a)return;
 const labels={PLAN_READY:'Research plan selected',ABSTAIN:'No plan selected',COMPILER_BLOCKED:'Plan checks blocked',INPUT_REJECTED:'Input rejected'};
 const explain=r=>(r.reasons||[]).map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ');
 const latest=a.records[0];
 text('auto-state',!a.enabled?'Disabled':latest?(labels[latest.state]||latest.state):'Waiting for three feeds');
 text('auto-help',latest?at(latest.cutoff)+' NY · '+(explain(latest)||'A complete research plan passed the mechanical checks.'):'All three feeds must reach a closed minute before selection runs.');
 text('auto-risk','Hypothetical plan limits: $'+a.configuration.paper_risk_usd+' price risk and '+a.configuration.paper_max_contracts+' MNQ contract(s). These are research settings, not your Builder account limits.');
 const choice=document.getElementById('auto-choice');choice.replaceChildren();
 const selected=latest?.selected,plan=latest?.compiled?.price_review;
 document.getElementById('auto-choice-empty').hidden=!!selected;
 if(selected){const v=selected.audit;for(const [k,val] of [['Direction',(selected.selection.direction||'unknown').toUpperCase()],['Setup',v.recipe.replaceAll('_',' ')],['Point of interest',v.poi.replaceAll('_',' ')],['Confirmation',v.timeframe+'m closed inversion and '+(selected.selection.direction==='short'?'low':'high')+' break'],['Initial stop',v.stop],['Target',v.target.price+' · '+v.target.reason],['Quantity',v.quantity+' MNQ — hypothetical'],['Why this ranked first','Higher-timeframe point of interest, then timeframe priority, then latest gap']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=String(val);choice.append(dt,dd);}}
 text('auto-comparison',latest?JSON.stringify({version:latest.version,policy:latest.policy,direction_checks:latest.direction_checks,candidates:latest.candidates,checks:plan},null,2):'No review yet.');
 const target=document.getElementById('auto-rows');target.replaceChildren();
 for(const r of a.records){const tr=document.createElement('tr');for(const val of [at(r.cutoff),(labels[r.state]||r.state)+(r.selected?' · '+r.selected.selection.direction.toUpperCase():''),explain(r)||r.selected?.audit?.target?.reason||'Inspect audit']){const td=document.createElement('td');td.textContent=val;tr.appendChild(td);}const td=document.createElement('td'),link=document.createElement('a');link.href='/api/automatic-selection/audit/'+encodeURIComponent(r.revision)+'/'+r.cutoff;link.target='_blank';link.rel='noopener';link.textContent='Open audit';td.append(link);
 if(r.state==='PLAN_READY'){const pine=document.createElement('a');pine.href='/api/automatic-selection/pine/'+encodeURIComponent(r.revision)+'/'+r.cutoff;pine.textContent='Download Pine';pine.className='pine-download';td.append(document.createElement('br'),pine);}
 tr.append(td);target.append(tr);}
 document.getElementById('auto-empty').hidden=!!a.records.length;
}

function showDataHealth(h){
 const node=document.getElementById('data-alert');
 if(!h){node.hidden=false;node.className='data-alert warning';node.textContent='Data health unavailable.';return;}
 node.hidden=h.level==='ok';node.className='data-alert '+h.level;
 const message=(h.messages||[]).join(' ');if(node.textContent!==message)node.textContent=message;
}

function showAccount(a){
 const money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n);
 text('account-asof',a?'MANUAL SNAPSHOT · '+a.as_of:'No account snapshot supplied');
 for(const [id,val] of [['account-balance',a?.balance],['account-floor',a?.minimum_balance],['account-buffer',a?a.balance-a.minimum_balance:null],['account-target',a?Math.max(0,a.target_balance-a.balance):null]])text(id,val==null?'—':money(val));
}
