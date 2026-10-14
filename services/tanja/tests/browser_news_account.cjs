const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
const state={csrf_token:'fixture',feeds:{ES:{count:0},MNQ:{count:0},NQ:{count:0}},candidates:[],jobs:[],diagnostics:[],data_health:{level:'ok'},account_snapshot_current:true,account_snapshot:{balance:48962.2,minimum_balance:48161.9,target_balance:53000,as_of:'fixture'},automatic_selection:{enabled:true,configuration:{paper_max_contracts:40},records:[],scheduled_news:{state:'BLOCKED',reasons:['NEWS_FETCH_FAILED']},account_guard:{state:'BLOCKED',reasons:['ACCOUNT_RISK_FIELDS_REQUIRED']}}};
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];let saved;
page.on('pageerror',e=>errors.push(e.message));
await page.route('http://tanja.local/**',async route=>{
 const req=route.request(),p=new URL(req.url()).pathname;
 if(p==='/api/account-snapshot'){
  assert.equal(req.method(),'POST');saved=req.postDataJSON();
  return route.fulfill({json:{ok:true,account_snapshot:{...saved,as_of:'fixture'},risk_budget_usd:244.81,orders_enabled:false}});
 }
 assert.equal(req.method(),'GET');
 if(p==='/api/state')return route.fulfill({json:state});
 const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
 return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
});
await page.goto('http://tanja.local/#automatic');
await page.getByText('New plans blocked: news fetch failed',{exact:true}).waitFor();
assert((await page.locator('#auto-account-status').innerText()).includes('account risk fields required'));
await page.goto('http://tanja.local/#overview');
await page.locator('#input-balance').waitFor();
assert.equal(await page.locator('#input-daily').inputValue(),'');
await page.locator('#input-daily').fill('800');await page.locator('#input-committed').fill('50');await page.locator('#input-contracts').fill('2');
await page.locator('#save-account').click();await page.getByText(/Saved. Reported-balance risk budget/).waitFor();
assert.equal(saved.daily_loss_remaining,800);assert.equal(saved.committed_risk_usd,50);assert.equal(saved.committed_micro_contracts,2);
state.automatic_selection.scheduled_news={state:'CLEAR',reasons:[],received_at:1791554400,next_event:{title:'Synthetic USD release',at:1791558000}};
state.automatic_selection.account_guard={state:'REPORTED_LIMITS_CLEAR',reasons:[],budget_usd:194.81,max_contracts:38};
await page.goto('http://tanja.local/#automatic');await page.reload();
await page.getByText(/Reported limits permit up to \$194.81/).waitFor();
await page.screenshot({path:'/private/tmp/tanja-news-account-desktop.png',fullPage:true});
await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/private/tmp/tanja-news-account-mobile.png',fullPage:true});
assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
assert.equal(errors.length,0);
console.log(JSON.stringify({calendarFailureVisible:true,noInventedAllowance:true,accountFieldsSaved:true,currentGuardStatus:true,mobileFits:true,errors,orders:0}));
await browser.close();
})();
