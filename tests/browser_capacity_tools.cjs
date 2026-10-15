/* Real React fixtures, synthetic-only HTTP, no production calls or credentials. */
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(require.resolve('playwright', { paths: [process.env.PLAYWRIGHT_MODULE_ROOT || process.cwd()] }));

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const destination = path.resolve(process.env.UI_EVIDENCE_DIR || 'data/ui-evidence');
  fs.mkdirSync(destination, { recursive: true });
  const evidence = [];
  try {
    for (const locale of ['zh', 'en']) {
      for (const width of [1440, 390]) {
        const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
        const errors = [], requests = [];
        page.on('pageerror', error => errors.push(String(error)));
        await page.route('**/api/**', async route => {
          requests.push(route.request().url());
          const query = new URL(route.request().url()).searchParams;
          if (query.get('version') && query.get('version') !== 'fixed-1') throw Error('Version changed between evidence pages');
          const offset = Number(query.get('offset') || 0);
          const raw = '{"status":"submitted","order_status":"waiting_trigger","requested_amount":0.03,"filled_amount":0,"原始证据":"' + '中'.repeat(5000) + '"}';
          await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ text: raw.slice(offset, offset + 2048), view: query.get('view'),
            version: 'fixed-1', offset, next_offset: offset + 2048, has_more: offset + 2048 < raw.length, read_only: true }) });
        });
        await page.goto(`http://127.0.0.1:18973/tests/single-call-fixture.html?locale=${locale}`, { waitUntil: 'networkidle' });
        if (requests.length) throw Error('Audit fetched before user opened it');
        const content = await page.locator('main').innerText();
        if (!content.includes('waiting_trigger') || !content.includes('0.03')) throw Error('Accepted/unfilled facts missing');
        if (/get_trade_operation|get_operation/.test(content)) throw Error('Retired operation tool in default UI');
        await page.screenshot({ path: path.join(destination, `single-call-default-${locale}-${width}.png`), fullPage: true });
        await page.getByText(locale === 'zh' ? '完整回执详情' : 'Complete receipt evidence', { exact: true }).click();
        await page.getByText('fixed-1', { exact: false }).waitFor();
        if (requests.length !== 1) throw Error('Opening detail must fetch exactly one bounded page');
        await page.getByRole('button', { name: locale === 'zh' ? '下一页' : 'Next', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('.content-disclosure[open] pre')?.textContent?.includes('中'));
        if (requests.length !== 2 || !requests[1].includes('version=fixed-1')) throw Error('Audit continuation not fixed and bounded');
        if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error('UI overflows viewport');
        if (errors.length) throw Error(errors.join('\n'));
        const screenshot = path.join(destination, `single-call-${locale}-${width}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        evidence.push({ locale, width, screenshot, requests: requests.length });
        await page.close();
      }
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(destination, 'single-call-report.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ passed: true, evidence }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
