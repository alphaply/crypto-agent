/* Isolated UI regression: start Vite, then node this-file [playwright module path]. */
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[2] || 'playwright');
const origin = process.env.TEST_FRONTEND_URL || 'http://127.0.0.1:18972';

async function exercise(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 960 }, reducedMotion: 'reduce' });
  const agent = { config_id: 'fixture', display_name: 'Memory fixture', symbol: 'ETH/USDT', symbols: ['ETH/USDT'], mode: 'STRATEGY', enabled: true, decision: { legacy: true } };
  const row = { id: 1, config_id: 'fixture', symbol: 'ETH/USDT', bucket_start: '2026-10-29 13:00:00.123456', bucket_end: '2026-10-29 13:00:00', market_summary: 'Original memory' };
  const errors = [], writes = [];
  let failure = null, releaseWorkspace, delayWorkspace = false;
  await context.route('**/api/**', async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') {
      assert.equal(path, '/api/history/short-memories');
      assert.equal(request.method(), 'PUT');
      const payload = request.postDataJSON();
      writes.push({ payload, token: request.headers().authorization });
      if (failure === 'network') return route.abort('failed');
      if (failure) return route.fulfill({ status: failure, json: { detail: 'Isolated save failure' } });
      assert.equal(request.headers().authorization, 'Bearer session-two');
      assert.equal(payload.bucket_start, row.bucket_start);
      assert.equal(payload.config_id, 'fixture');
      assert.ok(!('position_summary' in payload));
      row.market_summary = payload.market_summary;
      delayWorkspace = true;
      return route.fulfill({ json: { success: true, updated: 1 } });
    }
    let payload = { success: true };
    if (path === '/api/setup/status') payload = { required: false };
    if (path === '/api/auth/me') payload = { sub: 'admin' };
    if (path === '/api/public/dashboard') payload = { agent_summaries: [agent], symbols: ['ETH/USDT'], current_symbol: 'ETH/USDT' };
    if (path === '/api/public/short-memories') payload = { short_memories: [row] };
    if (path.startsWith('/api/public/workspace/')) {
      if (delayWorkspace) await new Promise((resolve) => { releaseWorkspace = resolve; });
      payload = { agent, position: {}, orders: { orders: [] }, short_memories: { short_memories: [row] }, kline: {} };
    }
    await route.fulfill({ json: payload }).catch(() => {});
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`${origin}/agents`);
  await page.getByText('Original memory', { exact: true }).waitFor();
  const edit = page.getByRole('button', { name: /编辑短期记忆|Edit short-term memory/ });
  assert.equal(await edit.count(), 0);
  const other = await context.newPage();
  await other.goto(`${origin}/agents`);
  const setToken = (value) => other.evaluate(async (token) => {
    const { setApiToken } = await import('/src/lib/api.js');
    setApiToken(token);
  }, value);
  await setToken('session-one');
  await edit.waitFor(); // No dashboard refresh or page reload after login.
  await setToken('session-two');
  await edit.click();
  await page.locator('.ant-modal textarea').fill('GDP 2026-10-29 12:30 UTC');
  const save = page.locator('.ant-modal .ant-btn-primary');
  for (const kind of [401, 500, 'network']) {
    failure = kind;
    await save.click();
    await page.locator('.ant-modal .ant-alert-error').waitFor();
    if (kind === 401) assert.match(await page.locator('.ant-modal').innerText(), /登录已失效|session has expired/);
    assert.equal(await page.locator('.ant-modal textarea').inputValue(), 'GDP 2026-10-29 12:30 UTC');
    assert.equal(row.market_summary, 'Original memory');
    assert.equal(writes.at(-1).token, 'Bearer session-two');
  }
  failure = null;
  await save.click();
  await page.locator('.ant-modal').waitFor({ state: 'hidden' });
  await page.locator('.agent-view-stack p').filter({ hasText: row.market_summary }).waitFor();
  // The saved text is visible even while the full workspace response is delayed.
  delayWorkspace = false;
  releaseWorkspace?.();
  await page.reload();
  await page.locator('.agent-view-stack p').filter({ hasText: row.market_summary }).waitFor();
  await page.goto(`${origin}/console/agents`);
  await edit.waitFor();
  await page.goto(`${origin}/agents`);
  await edit.waitFor();
  await setToken('');
  await edit.waitFor({ state: 'hidden' });
  const authHeader = await page.evaluate(async () => {
    const { api } = await import('/src/lib/api.js');
    api.defaults.headers.common.Authorization = 'Bearer stale-default';
    return (await api.get('/auth/me')).config.headers.Authorization || null;
  });
  assert.equal(authHeader, null);
  // The management editor uses the same API and preserves failed drafts too.
  await setToken('session-two');
  await page.goto(`${origin}/tests/decision-memory-fixture.html`);
  await page.getByRole('button', { name: 'edit', exact: true }).click();
  await page.locator('.ant-modal textarea').fill('Management correction 12:30 UTC');
  failure = 500;
  await page.locator('.ant-modal .ant-btn-primary').click();
  await page.locator('.ant-modal .ant-alert-error').waitFor();
  assert.equal(await page.locator('.ant-modal textarea').inputValue(), 'Management correction 12:30 UTC');
  failure = null;
  await page.locator('.ant-modal .ant-btn-primary').click();
  await page.locator('.ant-modal').waitFor({ state: 'hidden' });
  assert.equal(row.market_summary, 'Management correction 12:30 UTC');
  assert.equal(writes.at(-1).token, 'Bearer session-two');
  delayWorkspace = false;
  releaseWorkspace?.();
  assert.deepEqual(errors, []);
  await context.close();
  console.log(`Memory edit browser regression passed (${width}px): cross-tab login/rotation/logout, 401/500/network, preserved draft, immediate update, reload.`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const width of [1440, 390]) await exercise(browser, width); }
  finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
