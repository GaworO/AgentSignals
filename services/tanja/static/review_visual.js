'use strict';
// Display only the saved evidence for the selected review. Never request a model response.
const reviewVisual=(()=>{
 const el=(tag,content,cls)=>{const n=document.createElement(tag);if(content!=null)n.textContent=content;if(cls)n.className=cls;return n;};
 const pretty=s=>String(s||'not supplied').replaceAll('_',' ');
 const when=t=>Number.isFinite(t)?new Date(t*1000).toLocaleString('en-GB',{timeZone:'America/New_York',hour12:false}):'Unavailable';
 const price=n=>Number.isFinite(n)?n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'Not selected';
 const valid=r=>r.status==='validated'&&r.decision;
 const summary=r=>!valid(r)?'No usable decision: '+pretty(r.status)+(r.error?' — '+r.error:'')+'.':
   'The AI '+({candidate:'identified a possible setup',wait:'chose to wait',abstain:'chose not to trade'}[r.decision.context?.decision]||'recorded a review')+
   ' with '+pretty(r.decision.context?.bias)+' bias. '+(r.decision.rationale||'No explanation supplied.');
 let records=[],selected=null,signature='',audit=null,requestNumber=0,symbol='MNQ',tf=1;
 const cache=new Map();
 function update(next){
  records=next;
  const key=JSON.stringify(next.map(r=>[r.id,r.status,r.finished,r.error]));
  if(key===signature)return;signature=key;
  const current=next.find(r=>r.id===selected);
  if(!current&&next.length)selected=next[0].id;
  const list=document.getElementById('review-cards');list.replaceChildren();
  for(const r of next){const b=el('button',null,'review-card');b.type='button';b.dataset.reviewId=r.id;
   b.append(el('strong',when(r.cutoff)+' NY'),el('span',pretty(r.status)),el('p',summary(r)));
   b.addEventListener('click',()=>choose(r.id));list.append(b);}
  if(next.length)choose(selected);
  else{selected=null;audit=null;requestNumber++;document.getElementById('review-detail').replaceChildren(el('p','No AI reviews recorded yet.'));}
 }
 async function choose(id){
  selected=id;const r=records.find(r=>r.id===id);if(!r)return;
  document.querySelectorAll('.review-card').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.reviewId===id)));
  const container=document.getElementById('review-detail');container.replaceChildren(el('p','Loading this review’s saved evidence…'));
  const generation=++requestNumber,cacheKey=id+':'+r.status+':'+r.finished;
  try{
   let a=cache.get(cacheKey);
   if(!a){const response=await fetch('/api/ai/audit/'+encodeURIComponent(id),{cache:'no-store',signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw Error('HTTP '+response.status);a=await response.json();
    if(a.id!==id||a.cutoff!==r.cutoff)throw Error('Review identity mismatch');
    if(r.status!=='running'){cache.set(cacheKey,a);if(cache.size>20)cache.delete(cache.keys().next().value);}}
   if(generation!==requestNumber)return;audit=a;symbol=a.decision?.analysis_symbol||'MNQ';tf=a.decision?.timeframe_minutes||1;render();
  }catch(e){if(generation!==requestNumber)return;container.replaceChildren(el('p','Could not load this saved chart: '+e.message),el('p','Select the review again to retry. No AI call or order was sent.'));}
 }
 function render(){
  const a=audit,p=a.snapshot?.packet,d=valid(a)?a.decision:null,c=d?.context;
  const box=document.getElementById('review-detail');box.replaceChildren();
  box.append(el('h3','Review at '+when(a.cutoff)+' NY'),el('p',summary(a),'review-summary'),el('p','Market evidence ends at '+when(a.cutoff)+' NY. Answer received: '+when(a.finished)+' NY. No later candles are shown.'));
  if(!p||p.as_of!==a.cutoff){box.append(el('p','Saved evidence is missing or its cutoff does not match; chart unavailable.'));return;}
  const controls=el('div',null,'actions');
  const markets=['NQ','MNQ','ES'].filter(s=>Object.values(p.evidence||{}).some(e=>e.symbol===s));
  for(const [name,values,value,change] of [['Market',markets,symbol,v=>symbol=v],['Timeframe',[1,2,3,5,15,60,240],tf,v=>tf=Number(v)]]){
   const label=el('label',name+' '),select=el('select');select.setAttribute('aria-label',name);
   for(const v of values){const o=el('option',name==='Market'?v:v+'m');o.value=v;o.selected=String(v)===String(value);select.append(o);}
   select.addEventListener('change',()=>{change(select.value);render();});label.append(select);controls.append(label);
  }
  box.append(controls);
  const evidence=p.evidence||{},entries=Object.entries(evidence).filter(([,e])=>Number.isFinite(e.available_at)&&e.available_at<=p.as_of);
  const bars=entries.map(([,e])=>e).filter(e=>e.kind==='bar'&&e.symbol===symbol&&e.timeframe===tf&&e.time+tf*60===e.available_at&&['open','high','low','close'].every(k=>Number.isFinite(e[k]))).sort((a,b)=>a.time-b.time).slice(-80);
  const selectedIds=new Set([...(d?.poi_evidence_ids||[]),c?.selected_trigger_id,d?.selected_swing?.evidence_id]);
  const features=entries.filter(([id,e])=>selectedIds.has(id)&&e.symbol===symbol);
  if(bars.length)box.append(chart(bars,features,d,p.as_of));else box.append(el('p','No closed '+symbol+' '+tf+'m candles were retained in this review.'));
  box.append(el('p','Green/red candles show price. Blue bands show selected areas of interest; amber marks the selected trigger or swing. Dashed stop/target lines are AI proposals, not broker orders. Only explicitly selected evidence is drawn.'));
  if(!d){box.append(el('p','This attempt has no validated interpretation. Price history is shown without proposed trade levels.'));return;}
  const details=el('div',null,'review-facts');
  const facts=[['Bias',pretty(c.bias)],['Decision',pretty(c.decision)],['Entry approach',pretty(d.entry_mode)],['Proposed stop',price(d.initial_stop?.price)],['Proposed targets',(d.management?.targets||[]).map(t=>price(t.price)+' — '+t.reason).join('; ')||'Not selected']];
  for(const [k,v] of facts){const tile=el('div');tile.append(el('small',k),el('p',v));details.append(tile);}box.append(details);
  const claims=el('ul',null,'review-claims'),names={higher_timeframe_context_valid:'Higher-timeframe direction',setup_origin_at_range_extreme:'Setup at range edge',liquidity_event_complete:'Liquidity event',confirmation_accepted:'Entry confirmation',structure_intact:'Price structure'};
  for(const [k,v] of Object.entries(c.claims||{})){claims.append(el('li',(v.value===true?'AI says yes':v.value===false?'AI says no':'Unknown')+' · '+(names[k]||pretty(k))+': '+(v.reason||'No explanation supplied.')));}box.append(el('h3','What supported the decision'),claims);
  const missing=[...new Set([...(c.missing_context||[]),...(d.missing_fields||[]),...(a.review?.missing_plan_fields||[])])];
  box.append(el('p',missing.length?'Still missing: '+missing.map(pretty).join('; '):'No missing fields reported. This does not confirm strategy fidelity or live-order readiness.','callout'));
 }
 function chart(bars,features,d,cutoff){
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1000 360');svg.setAttribute('role','img');svg.setAttribute('aria-label',symbol+' '+tf+' minute frozen candlestick chart ending '+when(cutoff));svg.classList.add('review-chart');
  const add=(tag,attrs,text)=>{const n=document.createElementNS(svg.namespaceURI,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text!=null)n.textContent=text;svg.append(n);return n;};
  const sameSymbol=d?.execution_symbol===symbol;
  const lines=sameSymbol?[{value:d.initial_stop?.price,label:'Proposed stop',color:'#ffb09f'},...(d.management?.targets||[]).map((t,i)=>({value:t.price,label:'Target '+(i+1),color:'#83d7b4'}))].filter(l=>Number.isFinite(l.value)):[];
  const values=bars.flatMap(b=>[b.low,b.high]).concat(lines.map(l=>l.value));
  features.forEach(([,e])=>['lower','upper','price'].forEach(k=>{if(Number.isFinite(e[k]))values.push(e[k]);}));
  const lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.09,.25),y=v=>300-(v-lo+pad)/(hi-lo+2*pad)*270;
  const start=bars[0].time,end=bars.at(-1).available_at,x=t=>55+(t-start)/Math.max(end-start,60)*740;
  for(let i=0;i<=4;i++){const value=lo+(hi-lo)*i/4;add('line',{x1:50,x2:800,y1:y(value),y2:y(value),stroke:'#243141'});add('text',{x:808,y:y(value)+4,fill:'#9cacbd','font-size':12},price(value));}
  const legend=el('ul',null,'chart-levels');
  features.forEach(([id,e],i)=>{
   const label=(id===d?.context?.selected_trigger_id?'Trigger':e.kind==='pivot'?'Swing':'Area of interest')+' '+(i+1),color=label.startsWith('Area')?'#82bbff':'#f0c675';
   if(Number.isFinite(e.lower)&&Number.isFinite(e.upper))add('rect',{x:55,y:y(e.upper),width:740,height:Math.max(1,y(e.lower)-y(e.upper)),fill:color,opacity:.13});
   if(Number.isFinite(e.price))add('line',{x1:55,x2:795,y1:y(e.price),y2:y(e.price),stroke:color,'stroke-dasharray':'3 4'});
   legend.append(el('li',label+' · '+symbol+' '+e.timeframe+'m '+e.kind+' · '+(Number.isFinite(e.price)?price(e.price):price(e.lower)+'–'+price(e.upper))));
  });
  const width=Math.min(10,740*tf*60/Math.max(end-start,60)*.65);
  for(const b of bars){const cx=x(b.time+tf*30),color=b.close>=b.open?'#83d7b4':'#ed9b9b';
   add('line',{x1:cx,x2:cx,y1:y(b.high),y2:y(b.low),stroke:color});const n=add('rect',{x:cx-width/2,y:y(Math.max(b.open,b.close)),width,height:Math.max(1,Math.abs(y(b.open)-y(b.close))),fill:color});
   const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=when(b.time)+' NY · O '+price(b.open)+' H '+price(b.high)+' L '+price(b.low)+' C '+price(b.close);n.append(title);
  }
  lines.forEach(l=>{add('line',{x1:55,x2:795,y1:y(l.value),y2:y(l.value),stroke:l.color,'stroke-dasharray':'6 4'});legend.append(el('li',l.label+' · '+price(l.value)));});
  for(const [t,anchor] of [[start,'start'],[end,'end']])add('text',{x:x(t),y:330,fill:'#9cacbd','font-size':12,'text-anchor':anchor},when(t)+' NY');
  const wrap=el('div'),plot=el('div',null,'chart-scroll');plot.append(svg);wrap.append(plot,legend);return wrap;
 }
 return {update};
})();
