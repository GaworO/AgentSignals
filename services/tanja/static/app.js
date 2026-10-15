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
 showAccount(d.account_snapshot,d.account_snapshot_current);
 for(const s of ['ES','MNQ','NQ']){const f=d.feeds[s]||{state:'NO_DATA',count:0,latest_close:null},p=s.toLowerCase();text(p+'-state',f.state==='NO_DATA'?'No data':f.state==='CURRENT'?'Receiving':d.data_health?.scheduled_pause?'Scheduled pause':'Data delayed');text(p+'-info',f.count+' bars · '+(f.latest_close?at(f.latest_close)+' NY':'Waiting for TradingView'));}
 const analysis=d.market_roles?.analysis_inputs?.[1]||'MNQ';
 text('market-roles','ES/'+analysis+' AI reviews select the setup and confirmation. MNQ evidence supplies execution entry, stop and target prices. Inspect Automatic plans for the selected evidence.');
 const readiness=d.market_readiness;
 text('market-readiness-state',readiness?.bars_ready?'Three analysis/execution feeds pass the candle checks':'Market history needs attention');
 text('market-readiness-reasons',readiness?(readiness.entry_reasons.length?readiness.entry_reasons.map(s=>s.toLowerCase().replaceAll('_',' ')).join('; '):'Feeds and dated contract mapping pass. Fresh broker quotes and account feedback are still required.'):'Readiness check unavailable on this service version.');
 text('market-readiness-atr',readiness?.daily_atr?'Five-session MNQ ATR: '+readiness.daily_atr.state.toLowerCase().replaceAll('_',' ')+(readiness.daily_atr.complete?' · '+readiness.daily_atr.atr.toFixed(2)+' points':' · '+(readiness.daily_atr.missing_sessions||[]).map(s=>s.trade_date+': '+s.missing_minutes+' missing minutes').join('; ')):'Five-session MNQ ATR awaits matching feeds.');
 text('market-readiness-contract',readiness?.execution_prices_attested?'Dated, unadjusted chart/contract mapping supplied by the operator.':'Execution contract alignment has not been established. Continuous chart symbols alone do not prove it.');
 text('candidate-lead','Latest 100 '+analysis+' inversion observations from ES/'+analysis+' analysis, with separate MNQ execution prices. Each observation still needs its context and plan checks.');
 const paired=['ES',analysis,'MNQ'].every(s=>d.feeds[s]?.count>0);text('progress-feed',paired?'Analysis and execution feeds have stored candles':'Waiting for analysis and execution feeds');
 rows('candidate-rows',d.candidates.map(c=>[at(c.as_of),c.direction.toUpperCase(),c.timeframe+'m',c.lower+' – '+c.upper,candidateStatus(c,d)]));document.getElementById('candidate-empty').hidden=d.candidates.length>0;
 showAI(d.ai);
 showPlans(d.plan_observer);
 showAutomatic(d.automatic_selection);
 showConnectionTest(d.connection_test,d.test_csrf);
 const loop=d.trading_loop;
 text('loop-state',loop?loop.status.toLowerCase().replaceAll('_',' '):'Loop not supplied by this service version');
 text('loop-reason',loop?.reason||'');
 text('loop-feedback',loop?.feedback?.account_verified?'Broker account feedback is current.':'Actual broker account feedback is missing or stale; a signal receipt is not a fill.');
 text('loop-losses',loop?.feedback?.loss_count!=null?'Today: '+loop.feedback.loss_count+' net losing completed trades across the account. Two block new entries; break-even trades do not count.':'Account-wide loss count unavailable until complete broker history arrives.');
 const q=loop?.feedback?.quote_feed,a=loop?.feedback?.accounting;
 text('loop-quotes',q?(q.quotes_current?'Separate live quotes are current for ES, NQ and MNQ.':'Separate quote source selected; fresh ES, NQ and MNQ component prices are required.'):'Separate live quotes are not configured. CrossTrade’s direct Tradovate API supplies no market data.');
 text('loop-accounting',a?(a.accounting_ready?'Account-wide fill coverage and costs reconciled for the current window.':a.fill_count+' locally recorded fills · '+a.provisional_loss_count+' provisional losing completed trades today. New entries blocked: '+a.reasons.map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ')):'CrossTrade accounting capture is not configured. A complete account-wide history and confirmed fees are required.');
 text('loop-audit',loop?JSON.stringify(loop,null,2):'No loop audit.');
 const c=d.latest_context;document.getElementById('context-empty').hidden=!!c;const dl=document.getElementById('context-summary');dl.replaceChildren();
 if(c){const p=c.packet;for(const [k,v] of [['Market cutoff',at(p.as_of)+' NY'],['Inputs frozen',at(c.frozen_at)+' NY'],['Packet ID',p.packet_id],['Evidence items',Object.keys(p.evidence).length],['Processing lag',c.processing_delay_seconds+' seconds']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;dl.append(dt,dd);}rows('coverage',['ES',analysis].map(s=>[s,c.coverage[s]['1'],c.coverage[s]['5'],c.coverage[s]['60'],c.coverage[s]['240']]));text('packet',JSON.stringify(c,null,2));text('warmup',c.warmup==='PARTIAL_HISTORY'?'Partial history: fewer than three complete 4h bars in at least one analysis market. No context approval.':'Higher-timeframe bars are present. News, contract alignment and strategy judgment still need validation.');text('progress-context','Latest snapshot: '+at(p.as_of)+' NY');}
 rows('job-rows',d.jobs.map(j=>[at(j.cutoff),j.status+(j.error?' · '+j.error:''),j.processed_at?Math.round(j.processed_at-j.cutoff)+'s':'—']));
 const ul=document.getElementById('diagnostics');ul.replaceChildren();for(const item of d.diagnostics){const li=document.createElement('li');li.textContent=at(item.at)+' · '+item.kind+' · '+item.message;ul.appendChild(li);}if(!d.diagnostics.length){const li=document.createElement('li');li.textContent='No recent intake errors recorded.';ul.appendChild(li);}
}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error('HTTP '+r.status);show(await r.json());text('connection','Service connected · refreshed '+new Date().toLocaleTimeString()+' · market times shown in New York');document.getElementById('connection').className='';}catch(e){showDataHealth({level:'error',messages:['Cannot reach the service. Displayed prices and decisions may be stale.']});text('connection','Cannot refresh the service. Displayed data may be old. '+e.message);document.getElementById('connection').className='error';}finally{setTimeout(refresh,10000);}}
refresh();

function candidateStatus(candidate,state){
 const plans=state.automatic_selection?.records||[];
 for(const record of plans){
  const checked=record.candidate_evaluations?.find(c=>c.candidate_id===candidate.id&&c.cutoff===candidate.as_of&&c.direction===candidate.direction&&c.lower===candidate.lower&&c.upper===candidate.upper);
  if(checked){
   if(checked.state==='PLAN_READY')return 'Saved context · '+(checked.entry_mode==='retracement'?'retracement':'confirmation')+' at '+checked.entry+' · stop '+checked.stop+' · target '+checked.target;
   if(checked.state==='QUALIFIED_NOT_SELECTED')return 'Qualified · another inversion selected under the saved context';
   return 'Blocked · '+checked.reasons.map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ');
  }
 }
 const linked=plans.find(r=>r.ai_context?.packet_id===candidate.packet_id&&r.selected?.selection?.trigger_event_id===candidate.id);
 if(linked){
  const p=linked.compiled?.plan;
  if(p)return (p.entry_mode==='retracement'?'Retracement plan':'Confirmation plan')+' · '+p.entry_reference.price+' · stop '+linked.compiled.price_review.stop+' · target '+linked.compiled.price_review.target;
  return 'Blocked · '+(linked.reasons||[]).map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ');
 }
 const review=state.ai?.records?.find(r=>r.packet_id===candidate.packet_id&&r.status==='validated');
 if(!review){
  const sequences=candidate.setup_sequences||[];
  const found=sequences.filter(s=>s.state==='SEQUENCE_MEASURED_NEEDS_AI_CONTEXT');
  if(found.length)return found.map(s=>s.explanation).join(' / ')+' · awaiting AI context';
  if(sequences.length)return 'No complete agreed sequence · '+[...new Set(sequences.flatMap(s=>s.reasons||[]))].map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ');
  return 'Awaiting review of this snapshot';
 }
 const decision=review.decision;
 if(decision.context.selected_trigger_id!==candidate.id)return 'Not selected by this review';
 if(decision.context.decision!=='candidate')return 'Review: '+decision.context.decision+' · '+decision.rationale;
 const missing=[...(review.review?.missing_plan_fields||[]),...(decision.context.missing_context||[])];
 return missing.length?'Missing: '+missing.join('; '):decision.entry_setup?'Entry selected · current guards pending':'Entry sequence not supplied by this review';
}

function showAI(a){
 if(!a)return;
 reviewVisual.update(a.records||[]);
 const labels={DISABLED:'Disabled',MISSING_API_KEY:'API key needed',MISSING_MODEL:'Model needed',CONFIG_ERROR:'Check configuration',WAITING_FOR_PAIRED_DATA:'Waiting for feeds',WAITING_FOR_FRESH_DATA:'Waiting for fresh data',WAITING_FOR_HISTORY:'Warming up history',WAITING_FOR_CONTIGUOUS_DATA:'Waiting for complete data',WEEKEND_NO_AI:'Weekend — AI paused',HOLIDAY_NO_AI:'US holiday — AI paused',CALENDAR_UPDATE_REQUIRED:'Calendar update required',OUTSIDE_REVIEW_WINDOW:'Outside review window',READY:'Ready for review',REVIEW_IN_PROGRESS:'Review in progress',PAUSED_AFTER_ERROR:'Paused after error',DAILY_LIMIT_REACHED:'Daily limit reached',WAITING_FOR_INTERVAL:'Waiting for next review',ALREADY_REVIEWED:'Snapshot reviewed',INPUT_TOO_LARGE:'Context too large'};
 const label=labels[a.status]||a.status;
 text('ai-state',label);text('ai-context-state',label);text('ai-info',(a.model||'No model configured')+' · '+a.calls_today+'/'+a.daily_limit+' calls today');text('progress-ai',label+' · observation only');
 text('ai-budget',a.review_window+' · at least '+a.min_interval_minutes+' minutes between reviews · '+a.calls_today+'/'+a.daily_limit+' daily attempts · maximum '+a.max_output_tokens+' output tokens per call.');
 text('ai-help',a.status==='PAUSED_AFTER_ERROR'?'Inspect the latest audit and fix the cause. Then increase TANJA_AI_REVISION in Railway to resume. Failed attempts still count toward the daily limit.':a.calendar&&!a.calendar.new_reviews_allowed?'New AI reviews are blocked: '+(a.calendar.holiday||labels[a.calendar.status]||a.calendar.status)+'. New York date: '+a.calendar.date+'. Data collection continues. Calendar verified through '+a.calendar.valid_through+'.':a.status==='WAITING_FOR_HISTORY'?'Collect at least three complete 4h bars and three 1h bars for BOTH markets. This normally requires overnight collection; missing earlier history remains a limitation.':'Scheduled context reviews · no broker orders.');
 const target=document.getElementById('ai-rows');target.replaceChildren();
 for(const r of a.records){const tr=document.createElement('tr');for(const v of [at(r.cutoff),at(r.finished),r.status+(r.error?' · '+r.error:''),r.usage?((r.usage.input_tokens??'?')+' in / '+(r.usage.output_tokens??'?')+' out'):'—']){const td=document.createElement('td');td.textContent=v;tr.appendChild(td);}const td=document.createElement('td'),link=document.createElement('a');link.href='/api/ai/audit/'+encodeURIComponent(r.id);link.target='_blank';link.rel='noopener';link.textContent='Open JSON';td.appendChild(link);tr.appendChild(td);target.appendChild(tr);}
 document.getElementById('ai-empty').hidden=!!a.records.length;
 const latest=a.records[0];
 const plain=xs=>(xs||[]).map(x=>x.toLowerCase().replaceAll('_',' ')).join('; ');
 const news=a.scheduled_news,account=a.account_guard;
 text('auto-news-status',news?(news.state==='CLEAR'?'Calendar current; outside the news entry window.':'New plans blocked: '+plain(news.reasons))+ (news.next_event?' Next: '+news.next_event.title+' · '+at(news.next_event.at)+' NY.':'')+(news.received_at?' Last received '+at(news.received_at)+' NY.':''):'Calendar unavailable; new plans blocked.');
 text('auto-account-status',account?.state==='REPORTED_LIMITS_CLEAR'?'Reported limits permit up to $'+account.budget_usd.toFixed(2)+' additional planned-price risk and '+account.max_contracts+' micro-equivalent contracts. Not broker verified; costs are extra.':'Account checks block new plans: '+plain(account?.reasons||['ACCOUNT_RISK_FIELDS_REQUIRED']));
 text('auto-record-guards',latest?'At the saved decision: news — '+(plain(latest.news_entry_reasons)||latest.scheduled_news?.state||'not checked')+'; account — '+(plain(latest.account_guard?.reasons)||latest.account_guard?.state||'not checked')+'.':'No saved decision yet.');
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
 const names={CONTEXT_WATCH_ARMED:'Context armed for new candidates',ENTRY_SELECTION_VERIFIED:'Entry sequence verified',AI_NOT_VALIDATED:'AI review not validated',ABSTAINED:'No trade proposed',CONTEXT_NOT_READY:'Context incomplete',UNSUPPORTED_VARIANT:'Unsupported setup',UNSUPPORTED_ENTRY_MODE:'Entry method unsupported',PLAN_INCOMPLETE:'Plan incomplete',RULE_REVIEW_REQUIRED:'Rules need review',AUDIT_REJECTED:'Evidence check failed'};
 rows('plan-rows',p.records.map(r=>[at(r.market_cutoff),at(r.available_at),names[r.state]||r.state,[...(r.reasons||[]),...(r.missing_fields||[]).map(s=>'Missing: '+s.replaceAll('_',' '))].join(' ')]));
 document.getElementById('plan-empty').hidden=p.records.length>0;
 for(const [id,list] of [['strategy-gaps',p.strategy_gaps],['automation-gaps',p.automation_gaps]]){const node=document.getElementById(id);node.replaceChildren();for(const message of list){const li=document.createElement('li');li.textContent=message;node.appendChild(li);}}
}

function showAutomatic(a){
 if(!a)return;
 const labels={WATCHING_CONTEXT:'Watching for a new inversion',CONTEXT_CANDIDATES_BLOCKED:'New inversions blocked by saved context',NEEDS_SOURCE_SELECTION:'Candidate needs setup-specific review',PLAN_READY:'Research plan selected',AI_CONTEXT_BLOCKED:'Setup blocked by AI context',NEWS_BLOCKED:'Setup blocked by news',ACCOUNT_RISK_BLOCKED:'Setup blocked by reported account limits',ABSTAIN:'No plan selected',COMPILER_BLOCKED:'Plan checks blocked',INPUT_REJECTED:'Input rejected'};
 const explain=r=>(r.reasons||[]).map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ');
 const latest=a.records[0];
 const plain=xs=>(xs||[]).map(x=>x.toLowerCase().replaceAll('_',' ')).join('; ');
 const news=a.scheduled_news,account=a.account_guard;
 text('auto-news-status',news?(news.state==='CLEAR'?'Calendar current; outside the news entry window.':'New plans blocked: '+plain(news.reasons))+ (news.next_event?' Next: '+news.next_event.title+' · '+at(news.next_event.at)+' NY.':'')+(news.received_at?' Last received '+at(news.received_at)+' NY.':''):'Calendar unavailable; new plans blocked.');
 text('auto-account-status',account?.state==='REPORTED_LIMITS_CLEAR'?'Reported limits permit up to $'+account.budget_usd.toFixed(2)+' additional planned-price risk and '+account.max_contracts+' micro-equivalent contracts. Not broker verified; costs are extra.':'Account checks block new plans: '+plain(account?.reasons||['ACCOUNT_RISK_FIELDS_REQUIRED']));
 text('auto-record-guards',latest?'At the saved decision: news — '+(plain(latest.news_entry_reasons)||latest.scheduled_news?.state||'not checked')+'; account — '+(plain(latest.account_guard?.reasons)||latest.account_guard?.state||'not checked')+'.':'No saved decision yet.');
 text('auto-state',!a.enabled?'Disabled':latest?(labels[latest.state]||latest.state):'Waiting for three feeds');
 text('auto-help',latest?at(latest.cutoff)+' NY · '+(explain(latest)||'A complete research plan passed the mechanical checks.'):'All three feeds must reach a closed minute before selection runs.');
 const atr=a.daily_atr||latest?.daily_atr;
 text('auto-atr-status',!atr?'Waiting for full-session ATR history.':atr.complete?'Daily ATR: '+atr.atr.toFixed(2)+' points · current session range '+atr.range.toFixed(2)+' points ('+(atr.range_to_atr*100).toFixed(1)+'% of ATR). '+(atr.reached?'New continuation entries blocked; open positions keep their management.':'ATR permits new continuation entries, subject to the other checks.')+' '+atr.convention:'ATR unavailable: '+(atr.reasons||[]).map(s=>s.toLowerCase().replaceAll('_',' ')).join('; ')+'. Incomplete candles are not filled.');
 const gate=latest?.ai_context;
 const gateReasons={AI_CONTEXT_MISSING:'No saved AI review is available.',AI_CONTEXT_LATEST_REVIEW_NOT_VALIDATED:'The latest review has no validated answer; older approval is not reused.',AI_CONTEXT_NOT_AVAILABLE_AT_BAR_CLOSE:'The AI answer arrived after this candle closed.',AI_CONTEXT_EXPIRED:'The context is older than 15 minutes.',AI_CONTEXT_PREVIOUS_SESSION:'The context belongs to an earlier New York session.',AI_CONTEXT_WAIT:'The AI chose to wait.',AI_CONTEXT_ABSTAIN:'The AI chose not to trade.',AI_CONTEXT_DIRECTION_CONFLICT:'The mechanical setup points in the opposite direction to the AI.',AI_CONTEXT_PRICE_INVALIDATED:'Price touched the thesis invalidation level.',AI_CONTEXT_OBJECTIVE_ALREADY_REACHED:'Price already reached a proposed AI objective.',AI_CONTEXT_MONITORING_GAP:'Missing or delayed candles prevent checking whether the thesis still holds.',AI_CONTEXT_INCOMPLETE:'The AI interpretation is incomplete.',AI_CONTEXT_NON_TRADING_REVIEW_DAY:'Weekend, excluded holiday or unsupported calendar date.',AI_CONTEXT_AUDIT_REJECTED:'The saved evidence failed verification.'};
 const reasons=latest?.state==='AI_CONTEXT_BLOCKED'?latest.reasons:gate?.reasons||[];
 text('auto-context-status',gate?reasons.length?reasons.map(x=>gateReasons[x]||x.toLowerCase().replaceAll('_',' ')).join(' '):'At this recorded check, the AI thesis permitted '+gate.direction+' research entries; the mechanical setup must also pass.':'This older record predates the AI entry connection.');
 const contextDetail=document.getElementById('auto-context-detail');contextDetail.replaceChildren();
 if(gate){for(const [label,value] of [['AI review',gate.ai_run_id||'None'],['Evidence cutoff',at(gate.source_cutoff)+' NY'],['Answer available',at(gate.available_at)+' NY'],['Expires',at(gate.expires_at)+' NY'],[(gate.invalidation_symbol||'MNQ')+' thesis invalidation',gate.invalidation_price??'Not available'],['AI explanation',gate.explanation||'No usable interpretation']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;contextDetail.append(dt,dd);}}
 const sizing=a.sizing;
 text('auto-risk',sizing?.budget_usd!=null?'Risk rule: 0.5% of today’s reported balance. Nominal budget before account limits: $'+sizing.budget_usd.toFixed(2)+' from last reported balance $'+sizing.balance.toFixed(2)+' ('+sizing.balance_as_of+'). Quantity is rounded down from stop distance; maximum '+a.configuration.paper_max_contracts+' MNQ. Balance is manually reported, not synchronized. Commission and slippage are extra.':'Risk rule: 0.5% of today’s reported balance. Save a fresh daily account snapshot under Overview before sizing a plan.');
 const choice=document.getElementById('auto-choice');choice.replaceChildren();
 const selected=latest?.selected,plan=latest?.compiled?.price_review;
 document.getElementById('auto-choice-empty').hidden=!!selected;
 if(selected){const v=selected.audit,s=selected.selection,p=latest.compiled?.plan;for(const [k,val] of [['Direction',(s.direction||'unknown').toUpperCase()],['Setup',(v.recipe||s.setup_profile||'unresolved').replaceAll('_',' ')],['Point of interest',(v.poi||'explicit context review').replaceAll('_',' ')],['Confirmation',v.timeframe+'m inversion close'+(s.requires_swing_break?' and selected '+(s.direction==='short'?'low':'high')+' break':'')],['Entry',s.entry_mode==='retracement'?'Optional further retracement'+(p?' at '+p.entry_reference.price:''):'After confirmation'],['Entry reason',s.entry_mode_reason||'Inspect selected evidence'],['Pending expiry',s.entry_valid_until?at(s.entry_valid_until)+' NY':'Context lifetime'],['Initial stop',v.stop],['Target',v.target.price+' · '+v.target.reason],['Quantity',v.quantity+' MNQ — hypothetical'],['Selection basis',v.selection_origin==='explicit_context_review'?'Explicit context review; inspect referenced assessments':'Unreviewed geometry ranking — not Tanja target priority']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=String(val);choice.append(dt,dd);}}
 text('auto-comparison',latest?JSON.stringify({version:latest.version,policy:latest.policy,ai_context:latest.ai_context,scheduled_news:latest.scheduled_news,news_entry_reasons:latest.news_entry_reasons,account_guard:latest.account_guard,geometry_state:latest.geometry_state,sizing:latest.sizing,direction_checks:latest.direction_checks,candidates:latest.candidates,candidate_evaluations:latest.candidate_evaluations,context_watch:latest.context_watch,daily_atr:latest.daily_atr,checks:plan},null,2):'No review yet.');
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

function showAccount(a,current){
 const money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n);
 text('account-asof',a?(current?'TODAY’S REPORTED SNAPSHOT · ':'DAILY UPDATE REQUIRED · ')+a.as_of:'Daily account update required');
 document.getElementById('save-account').disabled=accountBusy;
 for(const [id,value] of [['input-balance',a?.balance],['input-floor',a?.minimum_balance],['input-target',a?.target_balance],['input-daily',a?.daily_loss_remaining],['input-committed',a?.committed_risk_usd],['input-contracts',a?.committed_micro_contracts]]){const input=document.getElementById(id);if(!input.dataset.initialized&&value!=null){input.value=id==='input-contracts'?String(value):value.toFixed(2);input.dataset.initialized='true';}}
 for(const [id,val] of [['account-balance',a?.balance],['account-floor',a?.minimum_balance],['account-buffer',a?a.balance-a.minimum_balance:null],['account-target',a?Math.max(0,a.target_balance-a.balance):null]])text(id,val==null?'—':money(val));
}

let accountBusy=false;
document.getElementById('account-form').addEventListener('submit',async event=>{
 event.preventDefault();if(accountBusy)return;
 accountBusy=true;document.getElementById('save-account').disabled=true;
 text('account-save-result','Saving your daily account snapshot…');
 try{
  const body={balance:Number(document.getElementById('input-balance').value),minimum_balance:Number(document.getElementById('input-floor').value),target_balance:Number(document.getElementById('input-target').value),daily_loss_remaining:Number(document.getElementById('input-daily').value),committed_risk_usd:Number(document.getElementById('input-committed').value),committed_micro_contracts:Number(document.getElementById('input-contracts').value)};
  const response=await fetch('/api/account-snapshot',{method:'POST',headers:{'Content-Type':'application/json','X-Tanja-CSRF':testCSRF},body:JSON.stringify(body),signal:AbortSignal.timeout(10000)});
  const result=await response.json();if(!response.ok)throw Error(result.error||'Save failed');
  showAccount(result.account_snapshot,true);text('account-save-result','Saved. Reported-balance risk budget: $'+result.risk_budget_usd.toFixed(2)+'. Broker orders remain disabled.');
 }catch(error){text('account-save-result',error.message+' — refresh to verify the saved values before retrying.');}
 finally{accountBusy=false;document.getElementById('save-account').disabled=false;}
});
