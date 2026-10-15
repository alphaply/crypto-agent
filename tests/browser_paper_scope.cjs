/* Actual React components, synthetic mixed ledger, no external requests. */
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(require.resolve('playwright', { paths: [process.env.PLAYWRIGHT_MODULE_ROOT || process.cwd()] }));
async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const destination = path.resolve('data/ui-evidence'); fs.mkdirSync(destination, { recursive: true });
  const results = [];
  try {
    for (const locale of ['zh', 'en']) for (const width of [1440, 390]) {
      const zh = locale === 'zh', errors = [], audits = [], dashboards = [];
      const page = await browser.newPage({ viewport: { width, height: 1100 }, reducedMotion: 'reduce' });
      page.on('pageerror', e => errors.push(String(e)));
      const account = {namespace:'qqbot',user_id:'fixture-mixed',name:'Mixed fixture',cash:1000,available_cash:700,debt:100,used_margin:200,
        loan_limit:5000,leverage:5,markets:['spot','swap'],enabled:true,net_equity:null,spot:[],positions:[],
        completeness:{valuation:'incomplete'},filter:{excluded_resources:{spot:1,positions:1},unresolved_resources:{orders:1},retained_reserved_cash:300,retained_margin:200},valuation_status:'incomplete'};
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url()); let body = {};
        if (url.pathname === '/api/mcp') body = {settings:{enabled:true},connection:{url:'https://fixture.invalid/mcp'},profiles:[],keys:[],oauth_connections:[],tools:[],audit:[],
          paper_settings:{enabled:true,initial_balance:10000,max_leverage:125,loan_limit:1000000,fee_rate:.001,maintenance_margin_rate:.005,exchange:'binance'}};
        else if (url.pathname.endsWith('/audit')) { audits.push(url.href); body={text:'{"历史来源":"okx","原始证据":"保留资金"}',version:'fixed-paper',next_offset:null,has_more:false,read_only:true}; }
        else if (url.pathname.endsWith('/accounts')) body={items:[account],total:1};
        else if (url.pathname.endsWith('/dashboard')) {
          dashboards.push(url.href); const continued = url.searchParams.get('account_cursor') === '17';
          body={accounts:[account],account_total:1,summary:{cash:1000,debt:100,net_equity:null,open_orders:0},orders:[],events:[],leaderboard:[],unranked_count:1,
            scan_complete:continued,next_cursor:continued?{}:{accounts:17,orders:21,events:41,namespaces:'qqbot'}};
        }
        await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(`http://127.0.0.1:18973/tests/paper-fixture.html?locale=${locale}`,{waitUntil:'networkidle'});
      await page.getByText('fixture-mixed',{exact:true}).first().waitFor();
      if (audits.length) throw Error('Raw ledger eagerly loaded');
      await page.getByText(zh?'估值不完整':'Incomplete valuation',{exact:true}).first().waitFor();
      const rows=page.locator('.ant-table-row').filter({hasText:'fixture-mixed'});
      if (!(await rows.first().innerText()).includes('—')) throw Error('Missing equity rendered as zero');
      await page.getByRole('button',{name:zh?'继续扫描下一页':'Continue to next page',exact:true}).click();
      await page.waitForTimeout(100);
      if (!dashboards.some(u=>u.includes('account_cursor=17')&&u.includes('order_cursor=21')&&u.includes('event_cursor=41')&&u.includes('namespace_cursor=qqbot'))) throw Error('Storage cursors lost');
      await page.screenshot({path:path.join(destination,`paper-scope-${locale}-${width}.png`),fullPage:true});
      await rows.first().locator('.ant-table-row-expand-icon').click();
      await page.getByText(zh?'原始账本审计（含已过滤历史）':'Original ledger audit (including filtered history)',{exact:true}).click();
      await page.getByText('fixed-paper',{exact:false}).waitFor();
      if (audits.length!==1) throw Error('Ledger disclosure was not one bounded request');
      if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error('Viewport overflow');
      if(errors.length) throw Error(errors.join('\n'));
      await page.screenshot({path:path.join(destination,`paper-audit-${locale}-${width}.png`),fullPage:true});
      results.push({locale,width,lazyAuditRequests:audits.length,cursorContinuation:true}); await page.close();
    }
  } finally {await browser.close();}
  fs.writeFileSync(path.join(destination,'paper-scope-report.json'),JSON.stringify(results,null,2)); console.log(JSON.stringify({passed:true,results}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
