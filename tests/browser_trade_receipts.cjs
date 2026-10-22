/* Synthetic local React fixtures only; no production requests. */
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(require.resolve('playwright', {paths:[process.env.PLAYWRIGHT_MODULE_ROOT || process.cwd()]}));
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
 const dir=path.resolve(process.env.UI_EVIDENCE_DIR || 'data/latency-ui'); fs.mkdirSync(dir,{recursive:true});
 for(const locale of ['zh','en']) for(const width of [1440,390]) for(const theme of ['light','dark']) {
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'}); const errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/api/**',r=>r.abort());
  await page.goto(`http://127.0.0.1:18973/tests/tool-receipts-fixture.html?locale=${locale}&theme=${theme}`,{waitUntil:'networkidle'});
  await page.locator('.tool-trade-row').first().waitFor();
  const text=await page.locator('main').innerText();
  if(!text.includes(locale==='zh'?'成功':'Success') || !text.includes('0.13')) throw Error('Missing accepted requested amount');
  for(const price of ['2,400', '2,450', '68,500', '0.00001234', '2,210']) if(!text.includes(price)) throw Error('Missing price: '+price);
  if(!text.includes(locale==='zh'?'未提供':'Not provided') || !text.includes(locale==='zh'?'市价':'Market')) throw Error('Missing honest price labels');
  if(await page.locator('.tool-trade-results details').count())throw Error('Trading result still folded');
  if(text.includes('JSON') || text.includes('Show full result') || text.includes('展开完整结果') || text.includes('待核验') || text.includes('审计记录'))throw Error('Internal receipt UI remains');
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');
  if(errors.length)throw Error(errors.join('\n'));
  await page.screenshot({path:path.join(dir,`receipts-${locale}-${width}-${theme}.png`),fullPage:true});
  await page.goto(`http://127.0.0.1:18973/tests/single-call-fixture.html?locale=${locale}&theme=${theme}`,{waitUntil:'networkidle'});
  const call=await page.locator('main').innerText();
  if(call.includes('JSON') || await page.locator('details.decision-tool-call').count() || await page.locator('.decision-tool-result details').count())throw Error('Call or audit JSON remains');
  await page.screenshot({path:path.join(dir,`decision-${locale}-${width}-${theme}.png`),fullPage:true});
  await page.close();
 }
 console.log('16 UI screenshots verified');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
