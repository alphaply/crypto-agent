/* Run against `vite preview`; every API call is intercepted, with no backend writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(require.resolve('playwright', {
  paths: [process.env.PLAYWRIGHT_MODULE_ROOT || process.cwd()],
}));
const origin = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:18974';
const output = path.resolve('frontend/node_modules/.cache/performance-loading');

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const errors = [];
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce' });
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => localStorage.setItem('crypto-agent-locale', 'zh'));
    const scripts = [];
    page.on('request', request => { if (request.resourceType() === 'script') scripts.push(request.url()); });
    const pending = {};
    const reply = (route, body) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    await page.route('**/api/**', route => {
      const endpoint = new URL(route.request().url()).pathname;
      if (endpoint === '/api/public/dashboard' || endpoint === '/api/public/compare') {
        pending[endpoint] = route;
        return;
      }
      return reply(route, endpoint === '/api/setup/status' ? { required: false } : {});
    });
    await page.goto(origin);
    await page.getByRole('heading', { name: '数据总览', exact: true }).waitFor();
    assert.ok(pending['/api/public/dashboard'] && pending['/api/public/compare']);
    assert.ok(!scripts.some(url => /(?:LineChart|installSVGRenderer|MarkdownBlock)-/.test(url)), 'Heavy rendering code blocks initial data requests');
    await reply(pending['/api/public/dashboard'], { agent_summaries: [], news_snapshot: { digest: '**异步摘要已加载**', items: [] } });
    await reply(pending['/api/public/compare'], { series: [{ config_id: 'fixture', label: 'Fixture', mode: 'STRATEGY', data_state: 'ready', points: [{ date: '2026-10-24', equity: 100 }, { date: '2026-10-25', equity: 110 }] }] });
    await page.locator('.equity-line-chart svg').waitFor();
    await page.locator('.intelligence-digest strong').filter({ hasText: '异步摘要已加载' }).waitFor();
    await page.screenshot({ path: path.join(output, 'dashboard-loaded.png'), fullPage: true });
    await page.close();

    for (const width of [1440, 390]) {
      const admin = await browser.newPage({ viewport: { width, height: 1100 }, reducedMotion: 'reduce' });
      admin.on('pageerror', error => errors.push(error.message));
      await admin.addInitScript(() => {
        localStorage.setItem('crypto-agent-token', 'fixture-token');
        localStorage.setItem('crypto-agent-locale', 'zh');
        localStorage.setItem('crypto-agent-admin-active-tab', 'tasks');
      });
      const loaded = [], counts = {};
      admin.on('request', request => { if (request.resourceType() === 'script') loaded.push(request.url()); });
      const account = { namespace: 'qqbot', user_id: 'fixture-user', name: '测试账户', cash: 1000, debt: 0, available_cash: 1000, net_equity: null, enabled: true, markets: ['spot', 'swap'], leverage: 5, spot: [], positions: [], valuation_status: 'incomplete', completeness: { valuation: 'incomplete', reason_codes: ['paper_performance_scan_incomplete', 'future_reason'] } };
      await admin.route('**/api/**', route => {
        const endpoint = new URL(route.request().url()).pathname;
        counts[endpoint] = (counts[endpoint] || 0) + 1;
        if (endpoint === '/api/setup/status') return reply(route, { required: false });
        if (endpoint === '/api/config') return reply(route, { globals: { secrets: {} }, agents: [], llm_providers: [], exchange_profiles: [], prompts: { files: [] }, options: {} });
        if (endpoint === '/api/mcp') return reply(route, { settings: { enabled: true }, profiles: [], keys: [], paper_settings: { enabled: true, initial_balance: 10000, max_leverage: 125, loan_limit: 1000000, fee_rate: 0.001, maintenance_margin_rate: 0.005 } });
        if (endpoint === '/api/mcp/paper/accounts') return reply(route, { items: [account], total: 1 });
        if (endpoint === '/api/mcp/paper/dashboard') return reply(route, { accounts: [account], account_total: 1, summary: { cash: account.cash, net_equity: null }, orders: [], events: [], leaderboard: [], reset_leaderboard: [] });
        if (route.request().method() === 'PATCH') {
          Object.assign(account, route.request().postDataJSON());
          return reply(route, { success: true });
        }
        return reply(route, {});
      });
      await admin.goto(`${origin}/console/config`);
      await admin.getByRole('button', { name: '新增 Agent', exact: true }).waitFor();
      assert.ok(!loaded.some(url => /(?:AgentWorkspace|PaperTradingPanel|McpSettingsPanel|NewsSettingsPanel|MarkdownBlock)-/.test(url)), 'Unopened admin panels load eagerly');
      if (width < 500) {
        await admin.getByRole('combobox', { name: '配置分区', exact: true }).click();
        await admin.locator('.ant-select-dropdown:visible').getByText('QQBot · Paper', { exact: true }).click();
      } else {
        await admin.getByRole('tab', { name: 'QQBot · Paper', exact: true }).click();
      }
      const row = admin.locator('.paper-accounts-section .ant-table-row').filter({ hasText: 'fixture-user' });
      await row.waitFor();
      await row.locator('.ant-table-row-expand-icon').click();
      await admin.getByText('历史交易记录尚未扫描完毕。 future_reason', { exact: true }).waitFor();
      await row.getByRole('button', { name: '调整账户', exact: true }).click();
      const modal = admin.getByRole('dialog');
      const refreshed = admin.waitForResponse(response => new URL(response.url()).pathname === '/api/mcp/paper/dashboard');
      await modal.locator('.ant-modal-footer .ant-btn-primary').click();
      await modal.waitFor({ state: 'hidden' });
      await refreshed;
      assert.equal(counts['/api/mcp'], 1, 'Account editing fetches unrelated MCP settings');
      assert.equal(counts['/api/mcp/paper/accounts'], 2);
      assert.equal(counts['/api/mcp/paper/dashboard'], 2);
      assert.ok(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Viewport overflow');
      await admin.evaluate(() => {
        window.scrollTo(0, 0);
        document.querySelectorAll('.ant-table-content, .ant-table-body').forEach(table => { table.scrollLeft = 0; });
      });
      await admin.screenshot({ path: path.join(output, `paper-reasons-${width}.png`), fullPage: true });
      await admin.close();
    }
    assert.deepEqual(errors, []);
    console.log('PASS: deferred chart/Markdown loading; admin panel isolation; paper reasons and one scoped refresh per mutation; desktop/mobile.');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
