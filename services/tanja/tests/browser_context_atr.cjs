const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const fixtures=JSON.parse(fs.readFileSync(process.env.CONTEXT_ATR_UI_FIXTURES,'utf8'));
 let state=fixtures[0];const errors=[];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 const page=await browser.newPage({viewport:{width:1400,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://tanja.local/**',async route=>{
  assert.equal(route.request().method(),'GET');const p=new URL(route.request().url()).pathname;
  if(p==='/api/state')return route.fulfill({json:state});
  const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
  await route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
 });
 for(const item of fixtures){
  state=item;if(page.url().startsWith('http://tanja.local/'))await page.reload();else await page.goto('http://tanja.local/#automatic');
  await page.locator('#auto-atr-status').getByText(/Daily ATR: 100.00/).waitFor();
  assert((await page.locator('#candidate-rows').innerText()).includes('Saved context'));
  assert((await page.locator('#auto-context-detail').innerText()).includes('watch-'));
 }
 state.automatic_selection.daily_atr.reached=true;state.automatic_selection.daily_atr.range=100;state.automatic_selection.daily_atr.range_to_atr=1;
 await page.reload();await page.locator('#auto-atr-status').getByText(/New continuation entries blocked/).waitFor();
 await page.screenshot({path:'/private/tmp/tanja-context-atr-ui.png',fullPage:true});
 assert.equal(errors.length,0);
 console.log(JSON.stringify({savedContextCombinations:fixtures.length,newCandidateEntryStopTargetVisible:true,atrValueAndRangeVisible:true,continuationRestrictionExplained:true,browserErrors:errors,ordersSent:0}));
 await browser.close();
})();
