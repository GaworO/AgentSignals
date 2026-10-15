const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const fixtures=JSON.parse(fs.readFileSync(process.env.NQ_UI_FIXTURES,'utf8'));
 let item=fixtures[0];const errors=[];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 const page=await browser.newPage({viewport:{width:1400,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://tanja.local/**',async route=>{
  assert.equal(route.request().method(),'GET');const p=new URL(route.request().url()).pathname;
  if(p==='/api/state')return route.fulfill({json:item.state});
  if(p.startsWith('/api/ai/audit/'))return route.fulfill({json:item.audit});
  const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
  await route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
 });
 for(const fixture of fixtures){
  item=fixture;
  item.state.market_readiness={bars_ready:false,entry_reasons:['NQ_FEED_STALE_OR_FUTURE'],execution_prices_attested:false,
   daily_atr:{state:'NEEDS_HISTORY',complete:false,missing_sessions:[{trade_date:'2026-10-12',missing_minutes:1}]}};
  if(page.url().startsWith('http://tanja.local/')){await page.goto('http://tanja.local/#context');await page.reload();}else await page.goto('http://tanja.local/#context');
  await page.locator('#review-detail svg[aria-label^="NQ "]').waitFor();
  assert((await page.locator('#candidate-lead').innerText()).includes('100 NQ'));
  assert(!(await page.locator('.chart-levels').innerText()).includes('Proposed stop'));
  await page.getByLabel('Market',{exact:true}).selectOption('MNQ');
  assert((await page.locator('.chart-levels').innerText()).includes('Proposed stop'));
  await page.goto('http://tanja.local/#executions');
  assert((await page.locator('#loop-feedback').innerText()).includes('missing or stale'));
  assert((await page.locator('#loop-losses').innerText()).includes('unavailable'));
  await page.goto('http://tanja.local/#feed');
  assert((await page.locator('#market-readiness-reasons').innerText()).includes('nq feed stale or future'));
  assert((await page.locator('#market-readiness-atr').innerText()).includes('2026-10-12: 1 missing minutes'));
  assert((await page.locator('#market-readiness-contract').innerText()).includes('not been established'));
 }
 assert.equal(errors.length,0);
 await page.screenshot({path:'/private/tmp/tanja-nq-loop-ui.png',fullPage:true});
 console.log(JSON.stringify({combinations:fixtures.length,nqAnalysisLabel:true,mnqOrderLevelsOnlyOnMnq:true,brokerFeedbackBlockerVisible:true,marketReadinessBlockersVisible:true,browserErrors:errors,ordersSent:0}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
