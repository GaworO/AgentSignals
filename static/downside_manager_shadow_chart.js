/* SVG rendering only. All prices, decisions and outcomes come from the frozen API. */
window.DMChart=(()=>{
 const D=window.DMData;
 function render(host,trade,selected,onSelect){
  const detail=trade.detail,candles=detail?.candles||[],decisions=D.decisions(trade);
  if(!candles.length){host.innerHTML='<div class="empty">M1 candles are not available for this trade yet.</div>';return;}
  const nearest=ms=>{let j=0,best=Infinity;for(let i=0;i<candles.length;i++){const d=Math.abs(candles[i][0]-ms);if(d<best){best=d;j=i}}return j;};
  const last=decisions[selected]||decisions.at(-1),dol=last?.dol||{};
  const levels=[['ENTRY',trade.entry,'#5a9eff'],['INITIAL SL',trade.initial_sl,'#f06e7d'],['MANAGER SL',last?.virtual_sl_after_action??last?.virtual_sl,'#efb463'],['FIXED +2R',trade.fixed_tp,'#36d09b'],['EXECUTION DOL',dol.execution_price,'#55cad7'],['HTF DOL',dol.htf_price,'#aa91e7']].filter(x=>Number.isFinite(x[1]));
  const core=candles.flatMap(x=>[x[2],x[3]]).concat([trade.entry,trade.initial_sl,trade.fixed_tp].filter(Number.isFinite));
  const coreHi=Math.max(...core),coreLo=Math.min(...core),coreSpan=Math.max(coreHi-coreLo,1);
  const values=core.concat(levels.filter(x=>x[1]>=coreLo-coreSpan*.25&&x[1]<=coreHi+coreSpan*.25).map(x=>x[1]));
  let hi=Math.max(...values),lo=Math.min(...values),pad=Math.max((hi-lo)*.08,.5);hi+=pad;lo-=pad;
  const W=Math.max(host.clientWidth||900,candles.length*8+210),H=490,L=47,R=160,T=25,B=32,PW=W-L-R,PH=H-T-B;
  const x=i=>L+(i+.5)*PW/candles.length,y=p=>T+(hi-p)/(hi-lo)*PH;
  const svg=[];svg.push(`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="M1 Candles with decision markers"><rect width="${W}" height="${H}" fill="#0b1726"/>`);
  for(let i=0;i<=5;i++){const v=lo+(hi-lo)*i/5,Y=y(v);svg.push(`<line x1="${L}" x2="${W-R}" y1="${Y}" y2="${Y}" stroke="#243b52" opacity=".6"/><text x="4" y="${Y+3}" fill="#718ba4" font-size="10">${D.num(v)}</text>`);}
  candles.forEach((b,i)=>{const X=x(i),up=b[4]>=b[1],c=up?'#36c997':'#ed6d7f',bw=Math.max(3,Math.min(7,PW/candles.length*.6));svg.push(`<line x1="${X}" x2="${X}" y1="${y(b[2])}" y2="${y(b[3])}" stroke="${c}" stroke-width="1.3"/><rect x="${X-bw/2}" y="${Math.min(y(b[1]),y(b[4]))}" width="${bw}" height="${Math.max(1.5,Math.abs(y(b[1])-y(b[4])))}" fill="${c}"/>`);});
  const labels=levels.map(([name,p,c])=>({name,p,c,raw:y(p)})).sort((a,b)=>a.raw-b.raw);
  labels.forEach((it,i)=>{it.labelY=Math.max(T+10,Math.min(H-B-10,it.raw));if(i&&it.labelY<labels[i-1].labelY+21)it.labelY=labels[i-1].labelY+21;});
  const overflow=labels.length?Math.max(0,labels.at(-1).labelY-(H-B-10)):0;if(overflow)labels.forEach(it=>it.labelY-=overflow);
  labels.forEach(({name,p,c,raw,labelY})=>{const outside=raw<T||raw>H-B;if(!outside)svg.push(`<line x1="${L}" x2="${W-R}" y1="${raw}" y2="${raw}" stroke="${c}" stroke-width="1" stroke-dasharray="5 5" opacity=".8"/>`);svg.push(`<rect x="${W-R+4}" y="${labelY-10}" width="${R-6}" height="20" rx="3" fill="#12283a"/><text x="${W-R+8}" y="${labelY+3}" fill="${c}" font-size="9" font-weight="700">${outside?(raw<T?'↑ ':'↓ '):''}${name} ${D.num(p)}</text>`);});
  decisions.forEach((d,j)=>{const X=x(nearest(d.decision_ms)),Y=y(d.price??trade.entry),name=D.label(d),color=({ENTRY:'#5a9eff',HOLD:'#70879e',PROTECT:'#efb463',BE:'#efb463',CLOSE:'#f06e7d'})[name],active=j===selected,rr=active?8:name==='HOLD'?3:6;svg.push(`<circle data-mark="${j}" cx="${X}" cy="${Y}" r="${rr+5}" fill="transparent" style="cursor:pointer"/><circle data-mark="${j}" cx="${X}" cy="${Y}" r="${rr}" fill="${color}" stroke="#07121f" stroke-width="2" style="cursor:pointer"/>`);if(name==='PROTECT')svg.push(`<path data-mark="${j}" d="M ${X} ${Y-5} l 4 8 h -8 Z" fill="#0b1726" style="cursor:pointer"/>`);});
  const exit=detail?.manager;if(exit?.exit_ms!=null){const X=x(nearest(exit.exit_ms)),hit=String(exit.exit_reason||'').toUpperCase();const type=hit.includes('TARGET')?'TARGET':hit.includes('STOP')?'STOP':'CLOSE';const color=type==='TARGET'?'#36d09b':'#f06e7d';svg.push(`<path d="M ${X} ${H-19} l -6 -10 h 12 Z" fill="${color}"/><text x="${Math.min(W-R-55,X+9)}" y="${H-18}" fill="${color}" font-size="10" font-weight="800">${type}</text>`);}
  svg.push(`<text x="${L}" y="${H-5}" fill="#7d92aa" font-size="10">${D.date(candles[0][0])}</text><text x="${W-R-115}" y="${H-5}" fill="#7d92aa" font-size="10">${D.date(candles.at(-1)[0])}</text></svg>`);host.innerHTML=svg.join('');
  const tooltip=document.getElementById('chart-tooltip');host.querySelectorAll('[data-mark]').forEach(el=>{const j=Number(el.dataset.mark),d=decisions[j];el.addEventListener('click',()=>onSelect(j));el.addEventListener('mouseenter',()=>{tooltip.textContent=`${D.date(d.decision_ms)} · ${D.label(d)}\nPrice ${D.num(d.price)} · ${D.r(d.current_r)} · P ${D.num(d.probability,3)}\nMFE ${D.r(d.mfe_r)} · MAE ${D.r(d.mae_r)} · Giveback ${D.r(d.giveback_r)}\n${D.context(d)}`;tooltip.hidden=false;});el.addEventListener('mouseleave',()=>tooltip.hidden=true);});
 }
 return {render};
})();
