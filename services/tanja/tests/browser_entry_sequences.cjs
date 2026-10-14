// Synthetic API fixtures only; no service, provider call or order transport.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const fixtures=JSON.parse(fs.readFileSync(process.env.ENTRY_UI_FIXTURES,'utf8'));
 let state=fixtures[0];const errors=[];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 const page=await browser.newPage({viewport:{width:1400,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://tanja.local/**',async route=>{
  assert.equal(route.request().method(),'GET');const p=new URL(route.request().url()).pathname;
  if(p==='/api/state')return route.fulfill({json:state});
  if(p.startsWith('/api/ai/audit/'))return route.fulfill({json:state.fixture_audit});
  const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
  await route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
 });
 for(const item of fixtures){
  state=item;
  if(page.url().startsWith('http://tanja.local/'))await page.reload();
  else await page.goto('http://tanja.local/#automatic');
  const selected=state.automatic_selection.records[0].selected.selection;
  await page.locator('#auto-choice').getByText(selected.direction.toUpperCase(),{exact:true}).waitFor();
  const candidate=await page.locator('#candidate-rows').innerText();
  assert(candidate.includes(selected.entry_mode==='retracement'?'Retracement plan':'Confirmation plan'),selected.setup_profile+' '+selected.direction+' '+selected.entry_mode+' rendered: '+candidate);
  assert(candidate.includes('stop')&&candidate.includes('target'));
  const choice=await page.locator('#auto-choice').innerText();
  assert(choice.includes('1m inversion close'));
  assert(!choice.includes('and selected'));
  if(selected.entry_mode==='retracement')assert(choice.includes('Optional further retracement'));
 }
 await page.screenshot({path:'/private/tmp/tanja-entry-sequences-ui.png',fullPage:true});
 state.automatic_selection.records=[];state.ai.records=[];
 await page.reload();await page.getByText('Awaiting review of this snapshot',{exact:true}).waitFor({state:'attached'});
 assert.equal(errors.length,0);
 console.log(JSON.stringify({combinations:fixtures.length,entryStopTargetVisible:true,optionalRetracementVisible:true,noCompulsorySwingBreak:true,unreviewedSnapshotExplained:true,browserErrors:errors,ordersSent:0}));
 await browser.close();
})();
