const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const base=JSON.parse(fs.readFileSync(process.env.NQ_UI_FIXTURES,'utf8'))[0];
 let item=structuredClone(base);const errors=[];let posts=0;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 const page=await browser.newPage({viewport:{width:1400,height:1100}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://tanja.local/**',async route=>{
  if(route.request().method()!=='GET')posts++;
  assert.equal(route.request().method(),'GET');const p=new URL(route.request().url()).pathname;
  if(p==='/api/state')return route.fulfill({json:item.state});
  if(p.startsWith('/api/ai/audit/'))return route.fulfill({json:item.audit});
  const name=p==='/'?'index.html':p.replace(/^\/static\//,'');
  await route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(process.cwd()+'/services/tanja/static/'+name)});
 });
 const cases=[
  {name:'unconfigured',feedback:{account_verified:false,loss_count:null},quote:'not configured',account:'not configured'},
  {name:'pending fees',feedback:{account_verified:false,loss_count:null,quote_feed:{quotes_current:true},accounting:{accounting_ready:false,fill_count:12,provisional_loss_count:2,reasons:['ACCOUNT_WIDE_COVERAGE_UNVERIFIED','FILL_FEES_NOT_FINAL']}},quote:'are current',account:'New entries blocked: account wide coverage unverified; fill fees not final'},
  {name:'stale source',feedback:{account_verified:false,loss_count:null,quote_feed:{quotes_current:false},accounting:{accounting_ready:false,fill_count:12,provisional_loss_count:1,reasons:['RECONCILED_EXECUTIONS_OR_FEES_CHANGED']}},quote:'fresh ES, NQ and MNQ',account:'reconciled executions or fees changed'},
  {name:'reconciled synthetic window',feedback:{account_verified:false,loss_count:2,quote_feed:{quotes_current:true},accounting:{accounting_ready:true}},quote:'are current',account:'reconciled for the current window'}
 ];
 for(const c of cases){
  item=structuredClone(base);item.state.trading_loop={status:'DISABLED',reason:'No orders enabled.',orders_enabled:false,feedback:c.feedback};
  await page.goto('http://tanja.local/#executions');await page.reload();
  await page.waitForFunction(()=>document.querySelector('#loop-quotes').textContent.length>0);
  assert((await page.locator('#loop-quotes').innerText()).includes(c.quote),c.name);
  assert((await page.locator('#loop-accounting').innerText()).includes(c.account),c.name);
  if(c.name==='pending fees')await page.screenshot({path:'/private/tmp/tanja-quotes-accounting-blocked-ui.png',fullPage:true});
  if(c.feedback.loss_count===null)assert((await page.locator('#loop-losses').innerText()).includes('unavailable'));
  else assert((await page.locator('#loop-losses').innerText()).includes('2 net losing'));
 }
 assert.equal(errors.length,0);assert.equal(posts,0);
 await page.screenshot({path:'/private/tmp/tanja-quotes-accounting-ui.png',fullPage:true});
 await browser.close();
 console.log(JSON.stringify({cases:cases.length,browserErrors:errors,mutatingRequests:posts,ordersSent:0}));
})().catch(e=>{console.error(e);process.exit(1)});
