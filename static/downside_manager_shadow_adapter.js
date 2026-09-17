/* Presentation-only adapter for the existing read-only replay and live APIs. */
window.DMData=(()=>{
 const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const num=(v,n=2)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('en-US',{minimumFractionDigits:n,maximumFractionDigits:n});
 const signed=(v,n=2)=>v==null?'—':`${Number(v)>0?'+':''}${num(v,n)}`;
 const r=v=>v==null?'—':`${signed(v,2)}R`;
 const money=v=>v==null?'—':`${Number(v)<0?'-':'+'}$${num(Math.abs(Number(v)),2)}`;
 const date=v=>{if(v==null)return '—';if(typeof v==='string')return v;return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v));};
 const clock=v=>{if(v==null)return '—';return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v));};
 const cls=v=>v==null?'':Number(v)>0?'positive':Number(v)<0?'negative':'';
 const label=d=>{const x=String(d?.recommendation||'').toUpperCase();if(x.includes('ENTRY'))return 'ENTRY';if(x.includes('PROTECT'))return 'PROTECT';if(x==='BE'||x.includes('BREAKEVEN'))return 'BE';if(x.includes('CLOSE'))return 'CLOSE';return 'HOLD';};
 const context=d=>{const m=d?.m1||{},z=d?.dol||{};return [m.opposite_break?'Opposite MSS/BOS':m.structure_relative>0?'M1 supportive':m.structure_relative<0?'M1 opposite':'M1 neutral',m.fvg_hold_valid==null?'FVG unknown':m.fvg_hold_valid?'FVG holds':'FVG failed',z.execution_status?`Exec DOL ${z.execution_status}`:null].filter(Boolean).join(' · ');};
 const decisions=t=>t.detail?.manager?.decisions||[];
 const status=t=>t.status==='REPLAYED'?'HISTORICAL REPLAY':'LIVE SHADOW';
 async function api(path){const res=await fetch(path,{credentials:'same-origin'});if(!res.ok)throw Error(`${res.status} ${path}`);return res.json();}
 function stats(rows,key){const v=rows.map(x=>x[key]).filter(x=>Number.isFinite(x)),pos=v.filter(x=>x>0).reduce((a,b)=>a+b,0),neg=v.filter(x=>x<0).reduce((a,b)=>a+b,0);let cum=0,peak=0,dd=0;for(const x of v){cum+=x;peak=Math.max(peak,cum);dd=Math.max(dd,peak-cum)}return {wr:v.length?100*v.filter(x=>x>0).length/v.length:null,pf:neg<0?pos/-neg:null,net:v.reduce((a,b)=>a+b,0),dd,count:v.length};}
 return {esc,num,signed,r,money,date,clock,cls,label,context,decisions,status,api,stats};
})();
