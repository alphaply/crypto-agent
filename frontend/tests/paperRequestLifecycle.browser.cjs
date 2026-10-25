/* Production UI with delayed/mocked APIs; does not contact a trading backend. */
const assert = require('node:assert/strict');
const modulePaths = [process.env.PLAYWRIGHT_MODULE_ROOT || process.cwd()];
const { chromium } = require(require.resolve('playwright', { paths: modulePaths }));
const { expect } = require(require.resolve('playwright/test', { paths: modulePaths }));
const origin = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:18974';
const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce' });
    const errors = [], held = [], aborted = [], reads = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('requestfailed', request => aborted.push(request.url()));
    await page.addInitScript(() => {
      localStorage.setItem('crypto-agent-token', 'fixture-token');
      localStorage.setItem('crypto-agent-locale', 'zh');
      localStorage.setItem('crypto-agent-admin-active-tab', 'paper');
    });
    let holdReads = false, failAccounts = false, heldSettings, settingsGate;
    let heldScope = '', scopeGate, heldScopeReads = [], positionGate, heldPosition;
    let holdMcp = false, mcpGate, heldMcp;
    const readsGate = gate();
    const paperSettings = { enabled: true, initial_balance: 10000, max_leverage: 125, loan_limit: 1000000, fee_rate: 0.001, maintenance_margin_rate: 0.005 };
    const mcpSettings = { enabled: true, public_url: 'https://fixture.invalid' };
    const reply = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url()), endpoint = url.pathname, method = route.request().method();
      if (endpoint === '/api/setup/status') return reply(route, { required: false });
      if (endpoint === '/api/config') return reply(route, { globals: { secrets: {} }, agents: [], llm_providers: [], exchange_profiles: [], prompts: { files: [] }, options: {} });
      if (endpoint === '/api/mcp') {
        const body = JSON.parse(JSON.stringify({ settings: mcpSettings, connection: { url: 'https://fixture.invalid/mcp' }, profiles: [], keys: [], paper_settings: paperSettings }));
        if (holdMcp) { heldMcp = { route, body }; mcpGate.resolve(); return; }
        return reply(route, body);
      }
      if (method === 'PUT') {
        heldSettings = { route, body: route.request().postDataJSON(), endpoint };
        settingsGate.resolve();
        return;
      }
      if (method === 'POST' && endpoint.includes('/positions/')) {
        heldPosition = route;
        positionGate.resolve();
        return;
      }
      if (method === 'PATCH') return reply(route, { success: true });
      const userId = url.searchParams.get('user_id') || 'old-user';
      const account = { namespace: 'qqbot', user_id: userId, name: userId, cash: userId === 'new-user' ? 2000 : 1000, debt: 0, available_cash: 1000, enabled: true, leverage: 5, markets: ['spot', 'swap'], positions: [], spot: [] };
      if (endpoint === '/api/mcp/paper/accounts' || endpoint === '/api/mcp/paper/dashboard') {
        reads.push(url);
        const position = { resource_id: 'fixture-position', symbol: 'BTC/USDT:USDT', side: 'LONG', exchange: 'binance', leverage: 5, margin: 100, amount: 0.01 };
        const body = endpoint.endsWith('/accounts') ? { items: [account], total: 60 } : { accounts: [{ ...account, positions: [position] }], account_total: 1, summary: { cash: account.cash }, orders: [], events: [], leaderboard: [], reset_leaderboard: [], next_cursor: { accounts: url.searchParams.has('account_cursor') ? 29 : 17 } };
        if (heldScope === userId) {
          heldScopeReads.push({ route, body });
          if (heldScopeReads.length === 2) scopeGate.resolve();
          return;
        }
        if (holdReads && userId === 'old-user') {
          held.push({ route, body });
          if (held.length === 2) readsGate.resolve();
          return;
        }
        return failAccounts && endpoint.endsWith('/accounts') ? reply(route, { detail: 'Account refresh unavailable' }, 503) : reply(route, body);
      }
      return reply(route, {});
    });
    await page.goto(`${origin}/console/config`);
    const rows = page.locator('.paper-accounts-section .ant-table-row');
    await rows.filter({ hasText: 'old-user' }).waitFor();

    // Manual refreshes must be cancelled when a newer filter is selected.
    holdReads = true;
    await page.locator('.paper-filter-bar button.ant-btn').click();
    await page.getByRole('button', { name: /刷新数据$/ }).click();
    await readsGate.promise;
    await page.getByRole('textbox', { name: '用户 ID', exact: true }).fill('new-user');
    await rows.filter({ hasText: 'new-user' }).waitFor();
    for (const item of held) await reply(item.route, item.body);
    await expect(rows).toContainText('new-user');
    await expect(rows).not.toContainText('old-user');
    await expect(page.locator('.paper-balance-strip')).toContainText('2,000');
    await expect.poll(() => aborted.filter(url => /\/paper\/(accounts|dashboard)/.test(url)).length).toBeGreaterThanOrEqual(2);
    holdReads = false;

    // A successful write must not hide a failed follow-up read.
    failAccounts = true;
    await rows.getByRole('button', { name: '调整账户', exact: true }).click();
    await page.getByRole('dialog').locator('.ant-modal-footer .ant-btn-primary').click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await expect(page.locator('.paper-workspace > .ant-alert')).toContainText('Account refresh unavailable');
    failAccounts = false;

    // Editing a second value during a save must retain the newer draft.
    await page.locator('.paper-settings-collapse').getByText('交易与银行设置', { exact: true }).click();
    await page.locator('#paper-setting-initial_balance').fill('20000');
    await page.getByRole('switch', { name: '启用模拟交易', exact: true }).click();
    settingsGate = gate();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await settingsGate.promise;
    await page.locator('#paper-setting-initial_balance').fill('30000');
    // A key creation may refresh MCP metadata before the Paper save completes.
    holdMcp = true; mcpGate = gate();
    await page.locator('.paper-hero-actions button.ant-btn').click();
    await page.getByRole('dialog').locator('.ant-modal-footer .ant-btn-primary').click();
    await mcpGate.promise;
    holdMcp = false;
    Object.assign(paperSettings, heldSettings.body);
    await reply(heldSettings.route, { success: true, settings: paperSettings });
    await expect(page.locator('.paper-service-tag')).toContainText('服务已暂停');
    await reply(heldMcp.route, heldMcp.body);
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await expect(page.locator('.paper-service-tag')).toContainText('服务已暂停');
    await expect(page.locator('#paper-setting-initial_balance')).toHaveValue('30000');
    await expect(page.getByText('有未保存修改', { exact: true })).toBeVisible();
    assert.equal(paperSettings.initial_balance, 20000);

    // MCP reads also preserve unsaved settings and in-flight edits.
    await page.getByRole('tab', { name: 'MCP', exact: true }).click();
    const publicOrigin = page.getByPlaceholder('https://agent.example.com', { exact: true });
    await publicOrigin.fill('https://draft.invalid');
    const reread = page.waitForResponse(response => new URL(response.url()).pathname === '/api/mcp');
    await page.getByRole('button', { name: /刷新$/ }).click();
    await reread;
    await expect(publicOrigin).toHaveValue('https://draft.invalid');
    settingsGate = gate();
    await page.getByRole('button', { name: '保存接入设置', exact: true }).click();
    await settingsGate.promise;
    await publicOrigin.fill('https://newer-draft.invalid');
    Object.assign(mcpSettings, heldSettings.body);
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/mcp');
    await reply(heldSettings.route, { success: true, settings: mcpSettings });
    await saved;
    await expect(publicOrigin).toHaveValue('https://newer-draft.invalid');
    assert.equal(mcpSettings.public_url, 'https://draft.invalid');

    // Saving from page three while scope B is still loading must read B page one.
    await page.getByRole('tab', { name: 'QQBot · Paper', exact: true }).click();
    const thirdPage = page.waitForResponse(response => new URL(response.url()).searchParams.get('offset') === '40');
    await page.locator('.paper-accounts-section .ant-pagination-item-3').click();
    await thirdPage;
    settingsGate = gate();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await settingsGate.promise;
    heldScope = 'scope-b'; scopeGate = gate(); heldScopeReads = [];
    await page.getByRole('textbox', { name: '用户 ID', exact: true }).fill(heldScope);
    await scopeGate.promise;
    heldScope = '';
    Object.assign(paperSettings, heldSettings.body);
    await reply(heldSettings.route, { success: true, settings: paperSettings });
    await expect(rows).toContainText('scope-b');
    for (const item of heldScopeReads) await reply(item.route, item.body);
    assert.ok(reads.filter(url => url.pathname.endsWith('/accounts') && url.searchParams.get('user_id') === 'scope-b').every(url => url.searchParams.get('offset') === '0'));

    // A position response arriving after a scope switch must not reuse its old scan cursor.
    const scanned = page.waitForResponse(response => new URL(response.url()).searchParams.get('account_cursor') === '17');
    await page.getByRole('button', { name: '继续扫描下一页', exact: true }).click();
    await scanned;
    await page.getByRole('button', { name: '调整仓位', exact: true }).click();
    positionGate = gate();
    await page.getByRole('button', { name: '提交调整', exact: true }).click();
    await positionGate.promise;
    heldScope = 'scope-c'; scopeGate = gate(); heldScopeReads = [];
    // Force models a prop change while the modal is pending, without waiting for its overlay.
    await page.locator('#paper-user').fill(heldScope, { force: true });
    await scopeGate.promise;
    await expect(page.getByRole('button', { name: '继续扫描下一页', exact: true })).toHaveCount(0);
    heldScope = '';
    await reply(heldPosition, { success: true, status: 'completed', cash_change: 0, leverage_before: 5, leverage_after: 5, margin_before: 100, margin_after: 100, available_cash: 1000 });
    await expect(rows).toContainText('scope-c');
    for (const item of heldScopeReads) await reply(item.route, item.body);
    assert.ok(reads.filter(url => url.pathname.endsWith('/dashboard') && url.searchParams.get('user_id') === 'scope-c').every(url => !url.searchParams.has('account_cursor')));
    await page.getByRole('button', { name: /^完\s*成$/ }).click();
    assert.deepEqual(errors, []);
    console.log('PASS: obsolete reads cancelled; current scope and its pagination preserved; refresh errors retained; Paper and MCP save drafts preserved.');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
