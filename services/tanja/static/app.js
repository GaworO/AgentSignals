'use strict';
const titles={overview:'A clear view of every decision.',candidates:'Trade candidates',context:'AI context',executions:'50K Builder executions',feed:'Market data'};
if(new URLSearchParams(location.search).get('embed')==='1')document.documentElement.classList.add('embedded');
function route(){const key=location.hash.slice(1) in titles?location.hash.slice(1):'overview';document.querySelectorAll('.view').forEach(e=>e.hidden=e.id!==key);document.querySelectorAll('nav a').forEach(e=>e.classList.toggle('active',e.hash==='#'+key));document.getElementById('title').textContent=titles[key];document.getElementById('section-label').textContent=key.toUpperCase();}
window.addEventListener('hashchange',route);route();
const text=(id,value)=>{document.getElementById(id).textContent=value;};
const at=t=>t?new Date(t*1000).toLocaleString('en-GB',{timeZone:'America/New_York',hour12:false}):'—';
function rows(id,items){const target=document.getElementById(id);target.replaceChildren();for(const cells of items){const tr=document.createElement('tr');for(const value of cells){const td=document.createElement('td');td.textContent=String(value);tr.appendChild(td);}target.appendChild(tr);}}
function show(d){
 for(const s of ['ES','MNQ']){const f=d.feeds[s],p=s.toLowerCase();text(p+'-state',f.state==='NO_DATA'?'No data':f.state==='CURRENT'?'Receiving':'Stale / closed');text(p+'-info',f.count+' bars · '+(f.latest_close?at(f.latest_close)+' NY':'Waiting for TradingView'));}
 const paired=d.feeds.ES.count>0&&d.feeds.MNQ.count>0;text('progress-feed',paired?'Both markets have stored candles':'Waiting for both one-minute feeds');
 rows('candidate-rows',d.candidates.map(c=>[at(c.as_of),c.direction.toUpperCase(),c.timeframe+'m',c.lower+' – '+c.upper,'Needs context']));document.getElementById('candidate-empty').hidden=d.candidates.length>0;
 showAI(d.ai);
 showConnectionTest(d.connection_test,d.test_csrf);
 const c=d.latest_context;document.getElementById('context-empty').hidden=!!c;const dl=document.getElementById('context-summary');dl.replaceChildren();
 if(c){const p=c.packet;for(const [k,v] of [['Market cutoff',at(p.as_of)+' NY'],['Inputs frozen',at(c.frozen_at)+' NY'],['Packet ID',p.packet_id],['Evidence items',Object.keys(p.evidence).length],['Processing lag',c.processing_delay_seconds+' seconds']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;dl.append(dt,dd);}rows('coverage',['ES','MNQ'].map(s=>[s,c.coverage[s]['1'],c.coverage[s]['5'],c.coverage[s]['60'],c.coverage[s]['240']]));text('packet',JSON.stringify(c,null,2));text('warmup',c.warmup==='PARTIAL_HISTORY'?'Partial history: fewer than three complete 4h bars in at least one market. No context approval.':'Higher-timeframe bars are present. News, contract alignment and strategy judgment still need validation.');text('progress-context','Latest snapshot: '+at(p.as_of)+' NY');}
 rows('job-rows',d.jobs.map(j=>[at(j.cutoff),j.status+(j.error?' · '+j.error:''),j.processed_at?Math.round(j.processed_at-j.cutoff)+'s':'—']));
 const ul=document.getElementById('diagnostics');ul.replaceChildren();for(const item of d.diagnostics){const li=document.createElement('li');li.textContent=at(item.at)+' · '+item.kind+' · '+item.message;ul.appendChild(li);}if(!d.diagnostics.length){const li=document.createElement('li');li.textContent='No recent intake errors recorded.';ul.appendChild(li);}
}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);show(await r.json());text('connection','Service connected · refreshed '+new Date().toLocaleTimeString()+' · market times shown in New York');document.getElementById('connection').className='';}catch(e){text('connection','Cannot refresh the service. Displayed data may be old. '+e.message);document.getElementById('connection').className='error';}finally{setTimeout(refresh,10000);}}
refresh();

function showAI(a){
 if(!a)return;
 const labels={DISABLED:'Disabled',MISSING_API_KEY:'API key needed',MISSING_MODEL:'Model needed',CONFIG_ERROR:'Check configuration',WAITING_FOR_PAIRED_DATA:'Waiting for feeds',WAITING_FOR_FRESH_DATA:'Waiting for fresh data',WAITING_FOR_HISTORY:'Warming up history',WAITING_FOR_CONTIGUOUS_DATA:'Waiting for complete data',OUTSIDE_REVIEW_WINDOW:'Outside review window',READY:'Ready for review',REVIEW_IN_PROGRESS:'Review in progress',PAUSED_AFTER_ERROR:'Paused after error',DAILY_LIMIT_REACHED:'Daily limit reached',WAITING_FOR_INTERVAL:'Waiting for next review',ALREADY_REVIEWED:'Snapshot reviewed',INPUT_TOO_LARGE:'Context too large'};
 const label=labels[a.status]||a.status;
 text('ai-state',label);text('ai-context-state',label);text('ai-info',(a.model||'No model configured')+' · '+a.calls_today+'/'+a.daily_limit+' calls today');text('progress-ai',label+' · observation only');
 text('ai-budget',a.review_window+' · at least '+a.min_interval_minutes+' minutes between reviews · '+a.calls_today+'/'+a.daily_limit+' daily attempts · maximum '+a.max_output_tokens+' output tokens per call.');
 text('ai-help',a.status==='PAUSED_AFTER_ERROR'?'Inspect the latest audit and fix the cause. Then increase TANJA_AI_REVISION in Railway to resume. Failed attempts still count toward the daily limit.':a.status==='WAITING_FOR_HISTORY'?'Collect at least three complete 4h bars and three 1h bars for BOTH markets. This normally requires overnight collection; missing earlier history remains a limitation.':'The API key stays on Railway. Review times are a sampling schedule for this pilot, not Tanja entry rules.');
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
