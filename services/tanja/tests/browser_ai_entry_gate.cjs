// Run from repository root with PLAYWRIGHT_MODULE and optional CHROME_PATH.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
const t=1791554400;
const gate={state:'BLOCKED',reasons:['AI_CONTEXT_EXPIRED'],ai_run_id:'fixture-review',source_cutoff:t-900,available_at:t-870,expires_at:t,explanation:'Synthetic explanation: context is now too old.'};
const state={feeds:{ES:{count:0},MNQ:{count:0},NQ:{count:0}},candidates:[],jobs:[],diagnostics:[],data_health:{level:'ok'},automatic_selection:{enabled:true,configuration:{paper_max_contracts:40},records:[{cutoff:t,state:'AI_CONTEXT_BLOCKED',reasons:['AI_CONTEXT_EXPIRED'],ai_context:gate,selected:null,revision:'fixture'}]}};
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||undefined});
const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
await page.route('http://tanja.local/**',async route=>{
const p=new URL(route.request().url()).pathname;
assert.equal(route.request().method(),'GET');
if(p==='/api/state')return route.fulfill({json:state});
const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
});
await page.goto('http://tanja.local/#automatic');
await page.getByText('The context is older than 15 minutes.',{exact:true}).waitFor();
assert.equal(await page.getByText('Download Pine',{exact:true}).count(),0);
assert((await page.locator('#auto-context-detail').innerText()).includes('fixture-review'));
await page.screenshot({path:'/private/tmp/tanja-ai-entry-gate.png',fullPage:true});
state.automatic_selection.records[0].reasons=['AI_CONTEXT_DIRECTION_CONFLICT'];
gate.state='PERMITTED';gate.reasons=[];gate.direction='short';
await page.reload();await page.getByText('The mechanical setup points in the opposite direction to the AI.',{exact:true}).waitFor();
assert.equal(errors.length,0);
console.log(JSON.stringify({expiryExplanation:true,directionConflict:true,reviewProvenance:true,blockedPineHidden:true,browserErrors:errors,brokerOrders:0}));await browser.close();
})();
