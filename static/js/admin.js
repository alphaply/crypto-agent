let configs = [];
let promptFiles = [];
let currentPrompt = '';
let editingConfigId = null;
let pricingRows = [];
let promptEditor = null;

function initPromptEditor() {
  const textarea = document.getElementById('prompt-editor');
  if (!textarea || typeof window.CodeMirror === 'undefined') return;

  promptEditor = window.CodeMirror.fromTextArea(textarea, {
    mode: 'text/plain',
    lineNumbers: true,
    lineWrapping: true,
    indentUnit: 2,
    tabSize: 2,
    autofocus: false,
    extraKeys: {
      'Ctrl-S': () => savePrompt(),
      'Cmd-S': () => savePrompt(),
      'Ctrl-F': 'findPersistent',
      'Cmd-F': 'findPersistent',
    },
  });

  promptEditor.setSize('100%', 380);
}

function setPromptEditorValue(content) {
  if (promptEditor) {
    promptEditor.setValue(content || '');
    return;
  }
  const editor = document.getElementById('prompt-editor');
  if (editor) editor.value = content || '';
}

function getPromptEditorValue() {
  if (promptEditor) return promptEditor.getValue();
  return document.getElementById('prompt-editor')?.value || '';
}

function toast(message, type = 'ok') {
  const box = document.getElementById('admin-toast');
  if (!box) {
    alert(message);
    return;
  }
  const div = document.createElement('div');
  div.className = `px-3 py-2 rounded-lg text-sm shadow ${type === 'ok' ? 'bg-emerald-600 text-white' : 'bg-red-600 text-white'}`;
  div.textContent = message;
  box.appendChild(div);
  setTimeout(() => div.remove(), 2600);
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]));
}

async function refreshCaptcha() {
  const img = document.getElementById('captcha-img');
  if (!img) return;
  const resp = await fetch('/api/chat/captcha');
  const data = await resp.json();
  if (data.success) img.src = data.image;
}

async function loginAdmin() {
  const password = document.getElementById('admin-password')?.value || '';
  const captchaWrap = document.getElementById('captcha-wrap');
  const captchaVisible = captchaWrap && !captchaWrap.classList.contains('hidden');
  const captcha = document.getElementById('captcha-input')?.value || '';
  const err = document.getElementById('login-error');

  const resp = await fetch('/api/chat/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password, captcha: captchaVisible ? captcha : '' }),
  });
  const data = await resp.json();
  if (!data.success) {
    if (data.need_captcha && captchaWrap) {
      captchaWrap.classList.remove('hidden');
      await refreshCaptcha();
    }
    if (err) err.textContent = data.message || '登录失败';
    return;
  }
  window.location.reload();
}

async function logoutAdmin() {
  await fetch('/api/admin/logout', { method: 'POST' });
  window.location.reload();
}

function getFieldValue(id) {
  return (document.getElementById(id)?.value || '').trim();
}

function setFieldValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value == null ? '' : String(value);
}

function normalizeConfigFromForm() {
  const mode = getFieldValue('cfg-mode') || 'STRATEGY';
  const configId = getFieldValue('cfg-config_id');
  const symbol = getFieldValue('cfg-symbol').toUpperCase();
  const model = getFieldValue('cfg-model');

  if (!configId || !symbol || !model) {
    throw new Error('config_id / symbol / model 为必填项');
  }

  const cfg = {
    config_id: configId,
    symbol,
    mode,
    model,
    enabled: getFieldValue('cfg-enabled') !== 'false',
    prompt_file: getFieldValue('cfg-prompt_file') || (mode === 'SPOT_DCA' ? 'dca.txt' : mode === 'REAL' ? 'real.txt' : 'strategy.txt'),
    api_base: getFieldValue('cfg-api_base'),
    api_key: getFieldValue('cfg-api_key'),
  };

  const temp = Number(getFieldValue('cfg-temperature'));
  if (Number.isFinite(temp)) cfg.temperature = temp;

  if (mode === 'SPOT_DCA') {
    const dcaAmount = Number(getFieldValue('cfg-dca_amount'));
    cfg.dca_amount = Number.isFinite(dcaAmount) ? dcaAmount : 50;
    cfg.dca_freq = getFieldValue('cfg-dca_freq') || '1d';
    if (cfg.dca_freq === '1w') {
      const weekday = Number(getFieldValue('cfg-dca_weekday'));
      cfg.dca_weekday = Number.isFinite(weekday) ? weekday : 0;
    }
    cfg.dca_time = getFieldValue('cfg-dca_time') || '08:00';

    const initialCost = Number(getFieldValue('cfg-initial_cost'));
    if (Number.isFinite(initialCost)) cfg.initial_cost = initialCost;
    const initialQty = Number(getFieldValue('cfg-initial_qty'));
    if (Number.isFinite(initialQty)) cfg.initial_qty = initialQty;
  } else {
    const leverage = Number(getFieldValue('cfg-leverage'));
    cfg.leverage = Number.isFinite(leverage) && leverage > 0 ? leverage : 1;

    const interval = Number(getFieldValue('cfg-run_interval'));
    cfg.run_interval = Number.isFinite(interval) && interval > 0 ? interval : (mode === 'REAL' ? 15 : 60);

    if (mode === 'REAL') {
      const binanceApiKey = getFieldValue('cfg-binance_api_key');
      const binanceSecret = getFieldValue('cfg-binance_secret');
      if (binanceApiKey) cfg.binance_api_key = binanceApiKey;
      if (binanceSecret) cfg.binance_secret = binanceSecret;
    }
  }

  const sumModel = getFieldValue('cfg-sum-model');
  const sumApiBase = getFieldValue('cfg-sum-api_base');
  const sumApiKey = getFieldValue('cfg-sum-api_key');
  if (sumModel || sumApiBase || sumApiKey) {
    cfg.summarizer = {};
    if (sumModel) cfg.summarizer.model = sumModel;
    if (sumApiBase) cfg.summarizer.api_base = sumApiBase;
    if (sumApiKey) cfg.summarizer.api_key = sumApiKey;
  }

  return cfg;
}

function setConfigForm(cfg) {
  setFieldValue('cfg-config_id', cfg?.config_id || '');
  setFieldValue('cfg-symbol', cfg?.symbol || '');
  setFieldValue('cfg-mode', cfg?.mode || 'STRATEGY');
  setFieldValue('cfg-enabled', String(cfg?.enabled !== false));
  setFieldValue('cfg-model', cfg?.model || '');
  setFieldValue('cfg-temperature', cfg?.temperature ?? '');
  setFieldValue('cfg-prompt_file', cfg?.prompt_file || '');
  setFieldValue('cfg-api_base', cfg?.api_base || '');
  setFieldValue('cfg-api_key', cfg?.api_key || '');

  setFieldValue('cfg-leverage', cfg?.leverage ?? 1);
  setFieldValue('cfg-run_interval', cfg?.run_interval ?? '');
  setFieldValue('cfg-binance_api_key', cfg?.binance_api_key || '');
  setFieldValue('cfg-binance_secret', cfg?.binance_secret || '');

  setFieldValue('cfg-dca_amount', cfg?.dca_amount ?? 50);
  setFieldValue('cfg-dca_freq', cfg?.dca_freq || '1d');
  setFieldValue('cfg-dca_weekday', cfg?.dca_weekday ?? 0);
  setFieldValue('cfg-dca_time', cfg?.dca_time || '08:00');
  setFieldValue('cfg-initial_cost', cfg?.initial_cost ?? 0);
  setFieldValue('cfg-initial_qty', cfg?.initial_qty ?? 0);

  setFieldValue('cfg-sum-model', cfg?.summarizer?.model || '');
  setFieldValue('cfg-sum-api_base', cfg?.summarizer?.api_base || '');
  setFieldValue('cfg-sum-api_key', cfg?.summarizer?.api_key || '');

  const badge = document.getElementById('config-form-badge');
  if (badge) badge.textContent = cfg?.config_id || '未选择';

  onModeChange();
  onDcaFreqChange();
}

function onModeChange() {
  const mode = getFieldValue('cfg-mode') || 'STRATEGY';
  document.querySelectorAll('.mode-dca-only').forEach(el => {
    el.classList.toggle('hidden', mode !== 'SPOT_DCA');
  });
  document.querySelectorAll('.mode-common').forEach(el => {
    el.classList.toggle('hidden', mode === 'SPOT_DCA');
  });
  document.querySelectorAll('.mode-real-only').forEach(el => {
    el.classList.toggle('hidden', mode !== 'REAL');
  });
}

function onDcaFreqChange() {
  const freq = getFieldValue('cfg-dca_freq') || '1d';
  document.querySelectorAll('.dca-weekday-wrap').forEach(el => {
    el.classList.toggle('hidden', freq !== '1w');
  });
}

function renderConfigList() {
  const el = document.getElementById('config-list');
  if (!el) return;

  el.innerHTML = configs.map(cfg => {
    const active = editingConfigId === cfg.config_id;
    return `
      <div class="border ${active ? 'border-blue-300 bg-blue-50/50' : 'border-slate-200'} rounded-xl p-3 flex items-center justify-between gap-3">
        <button onclick="selectConfig('${escapeHtml(cfg.config_id)}')" class="text-left flex-1 min-w-0">
          <div class="font-bold text-slate-800 text-sm truncate">${escapeHtml(cfg.config_id)}</div>
          <div class="text-xs text-slate-500 truncate">${escapeHtml(cfg.symbol)} · ${escapeHtml(cfg.mode || 'STRATEGY')} · ${cfg.enabled === false ? 'disabled' : 'enabled'}</div>
        </button>
        <button onclick="deleteConfig('${escapeHtml(cfg.config_id)}')" class="px-2 py-1 rounded-lg border border-red-200 text-red-600 text-xs hover:bg-red-50">删除</button>
      </div>
    `;
  }).join('');

  const raw = document.getElementById('config-json');
  if (raw) raw.value = JSON.stringify(configs, null, 2);
}

function selectConfig(configId) {
  const cfg = configs.find(item => item.config_id === configId);
  if (!cfg) return;
  editingConfigId = configId;
  setConfigForm(cfg);
  renderConfigList();
}

function startCreateConfig() {
  editingConfigId = null;
  setConfigForm({ mode: 'STRATEGY', enabled: true, run_interval: 60, leverage: 1, dca_freq: '1d', dca_time: '08:00', dca_amount: 50 });
  renderConfigList();
}

async function loadConfigs() {
  const resp = await fetch('/api/config/raw');
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '加载配置失败', 'err');
    return;
  }

  configs = data.configs || [];
  renderConfigList();

  if (editingConfigId) {
    const cfg = configs.find(item => item.config_id === editingConfigId);
    if (cfg) {
      setConfigForm(cfg);
      return;
    }
  }
  if (configs.length > 0) {
    selectConfig(configs[0].config_id);
  } else {
    startCreateConfig();
  }
}

function syncFormToJson() {
  try {
    const normalized = normalizeConfigFromForm();
    const idx = configs.findIndex(item => item.config_id === normalized.config_id);
    if (idx >= 0) configs[idx] = normalized;
    else configs.push(normalized);
    editingConfigId = normalized.config_id;
    renderConfigList();
    toast('已同步到 JSON');
  } catch (error) {
    toast(error.message || '同步失败', 'err');
  }
}

async function upsertConfig(saveToEnv) {
  let normalized;
  try {
    normalized = normalizeConfigFromForm();
  } catch (error) {
    toast(error.message || '表单校验失败', 'err');
    return;
  }

  if (editingConfigId && editingConfigId !== normalized.config_id) {
    const oldIndex = configs.findIndex(item => item.config_id === editingConfigId);
    if (oldIndex >= 0) configs.splice(oldIndex, 1);
  }

  const idx = configs.findIndex(item => item.config_id === normalized.config_id);
  if (idx >= 0) configs[idx] = normalized;
  else configs.push(normalized);

  editingConfigId = normalized.config_id;
  renderConfigList();

  if (!saveToEnv) {
    toast('已保存到本地配置列表，点击“保存 JSON”可写入 .env');
    return;
  }

  const resp = await fetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ configs, global: {} }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '写入 .env 失败', 'err');
    return;
  }

  toast('配置已写入 .env');
  await loadConfigs();
}

async function deleteConfig(configId) {
  const depResp = await fetch(`/api/config/dependencies/${configId}`);
  const depData = await depResp.json();
  if (!depData.success) {
    toast(depData.message || '读取依赖失败', 'err');
    return;
  }

  const counts = depData.counts || {};
  const msg = `确认删除 ${configId}?\nsummary=${counts.summaries || 0}, orders=${counts.orders || 0}, token_usage=${counts.token_usage || 0}`;
  if (!confirm(msg)) return;

  const delResp = await fetch(`/api/config/delete/${configId}`, { method: 'POST' });
  const delData = await delResp.json();
  if (!delData.success) {
    toast(delData.message || '删除失败', 'err');
    return;
  }

  toast(delData.message || '删除成功');
  if (editingConfigId === configId) editingConfigId = null;
  await loadConfigs();
}

async function saveRawConfigs() {
  const raw = document.getElementById('config-json');
  if (!raw) return;

  let parsed;
  try {
    parsed = JSON.parse(raw.value);
  } catch (e) {
    toast('JSON 格式错误', 'err');
    return;
  }

  const resp = await fetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ configs: parsed, global: {} }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '保存失败', 'err');
    return;
  }

  toast('JSON 配置已保存');
  configs = parsed;
  await loadConfigs();
}

function renderPromptList() {
  const el = document.getElementById('prompt-list');
  if (!el) return;
  el.innerHTML = promptFiles.map(name => `
    <button onclick="openPrompt('${escapeHtml(name)}')" class="w-full text-left px-2 py-1.5 rounded ${name === currentPrompt ? 'bg-blue-600 text-white' : 'hover:bg-slate-100 text-slate-700'} text-xs font-mono">${escapeHtml(name)}</button>
  `).join('');
}

async function loadPrompts() {
  const resp = await fetch('/api/prompts/list');
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '加载模板失败', 'err');
    return;
  }
  promptFiles = data.files || [];
  renderPromptList();
}

async function openPrompt(name) {
  currentPrompt = name;
  const title = document.getElementById('current-prompt');
  if (title) title.textContent = name;
  renderPromptList();

  const resp = await fetch(`/api/prompts/read?name=${encodeURIComponent(name)}`);
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '读取模板失败', 'err');
    return;
  }

  setPromptEditorValue(data.content || '');
}

function normalizePromptName(rawName) {
  let name = String(rawName || '').trim();
  if (!name) throw new Error('请输入模板文件名');
  if (!name.endsWith('.txt')) name += '.txt';
  if (!/^[A-Za-z0-9._-]+\.txt$/.test(name)) {
    throw new Error('文件名仅支持字母、数字、._-，并以 .txt 结尾');
  }
  return name;
}

async function createPrompt() {
  let name;
  try {
    name = normalizePromptName(document.getElementById('new-prompt-name')?.value);
  } catch (error) {
    toast(error.message, 'err');
    return;
  }

  if (promptFiles.includes(name)) {
    toast('该模板已存在，已直接打开');
    await openPrompt(name);
    return;
  }

  currentPrompt = name;
  const title = document.getElementById('current-prompt');
  if (title) title.textContent = name;

  setPromptEditorValue('# Prompt template\n');

  await savePrompt();
  await loadPrompts();
  await openPrompt(name);
  toast(`模板已创建: ${name}`);
}

async function savePrompt() {
  if (!currentPrompt) {
    toast('请先选择模板', 'err');
    return;
  }

  const content = getPromptEditorValue();
  const resp = await fetch('/api/prompts/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: currentPrompt, content }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '保存失败', 'err');
    return;
  }

  toast('模板保存成功');
}

async function deletePrompt() {
  if (!currentPrompt) return;
  if (!confirm(`确认删除 ${currentPrompt}?`)) return;

  const resp = await fetch('/api/prompts/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: currentPrompt }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '删除失败', 'err');
    return;
  }

  currentPrompt = '';
  const title = document.getElementById('current-prompt');
  if (title) title.textContent = '未选择';
  setPromptEditorValue('');

  toast('模板已删除');
  await loadPrompts();
}

function renderPricingTable() {
  const tbody = document.getElementById('pricing-list-table');
  if (!tbody) return;

  tbody.innerHTML = pricingRows.map((row, idx) => `
    <tr class="border-t border-slate-100">
      <td class="px-3 py-2">
        <input data-role="model" data-idx="${idx}" value="${escapeHtml(row.model)}" class="w-full border border-slate-200 rounded-lg p-1.5 text-xs" ${row.locked ? 'readonly' : ''}>
      </td>
      <td class="px-3 py-2">
        <input data-role="input" data-idx="${idx}" type="number" step="0.000001" value="${row.input_price_per_m ?? 0}" class="w-full border border-slate-200 rounded-lg p-1.5 text-xs">
      </td>
      <td class="px-3 py-2">
        <input data-role="output" data-idx="${idx}" type="number" step="0.000001" value="${row.output_price_per_m ?? 0}" class="w-full border border-slate-200 rounded-lg p-1.5 text-xs">
      </td>
      <td class="px-3 py-2">
        <div class="flex gap-2">
          <button onclick="savePricingRow(${idx})" class="px-2 py-1 rounded border border-slate-200 text-xs hover:bg-slate-100">保存</button>
          <button onclick="deletePricingRow(${idx})" class="px-2 py-1 rounded border border-red-200 text-red-600 text-xs hover:bg-red-50">删除</button>
        </div>
      </td>
    </tr>
  `).join('');
}

async function loadPricing() {
  const resp = await fetch('/api/stats/pricing');
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '加载定价失败', 'err');
    return;
  }

  pricingRows = (data.pricing || []).map(item => ({ ...item, locked: true }));
  renderPricingTable();
}

function addPricingRow() {
  const model = getFieldValue('pricing-new-model');
  if (!model) {
    toast('请先输入模型名', 'err');
    return;
  }
  if (pricingRows.some(row => row.model === model)) {
    toast('该模型已存在', 'err');
    return;
  }

  pricingRows.unshift({ model, input_price_per_m: 0, output_price_per_m: 0, locked: false });
  setFieldValue('pricing-new-model', '');
  renderPricingTable();
}

async function savePricingRow(index) {
  const row = pricingRows[index];
  if (!row || !row.model) {
    toast('无效的模型行', 'err');
    return;
  }

  const tbody = document.getElementById('pricing-list-table');
  if (!tbody) return;
  const model = tbody.querySelector(`input[data-role="model"][data-idx="${index}"]`)?.value?.trim() || '';
  const inputPrice = Number(tbody.querySelector(`input[data-role="input"][data-idx="${index}"]`)?.value || 0);
  const outputPrice = Number(tbody.querySelector(`input[data-role="output"][data-idx="${index}"]`)?.value || 0);

  if (!model) {
    toast('模型名不能为空', 'err');
    return;
  }

  const resp = await fetch('/api/stats/pricing', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, input_price: inputPrice, output_price: outputPrice }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '定价保存失败', 'err');
    return;
  }

  pricingRows[index] = {
    model,
    input_price_per_m: inputPrice,
    output_price_per_m: outputPrice,
    locked: true,
  };
  renderPricingTable();
  toast(`定价已保存: ${model}`);
}

async function saveAllPricingRows() {
  for (let index = 0; index < pricingRows.length; index += 1) {
    const row = pricingRows[index];
    if (!row.model) continue;
    // eslint-disable-next-line no-await-in-loop
    await savePricingRow(index);
  }
}

async function deletePricingRow(index) {
  const row = pricingRows[index];
  if (!row || !row.model) {
    toast('无效的模型行', 'err');
    return;
  }

  if (!confirm(`确认删除模型定价: ${row.model} ?`)) return;

  const resp = await fetch('/api/stats/pricing', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: row.model }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '删除定价失败', 'err');
    return;
  }

  pricingRows.splice(index, 1);
  renderPricingTable();
  toast(`定价已删除: ${row.model}`);
}

async function cleanHistory() {
  const symbol = document.getElementById('clean-symbol')?.value || '';
  if (!symbol) return;
  if (!confirm(`确认清空 ${symbol} 的历史与财务数据？`)) return;

  const resp = await fetch('/api/clean_history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '清空失败', 'err');
    return;
  }
  toast(data.message || '已清空');
}

window.addEventListener('DOMContentLoaded', async () => {
  if (!document.getElementById('config-list')) return;

  initPromptEditor();
  const nameInput = document.getElementById('new-prompt-name');
  if (nameInput) {
    nameInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        createPrompt();
      }
    });
  }

  await loadConfigs();
  await loadPrompts();
  await loadPricing();

  window.onModeChange = onModeChange;
  window.onDcaFreqChange = onDcaFreqChange;
  window.startCreateConfig = startCreateConfig;
  window.selectConfig = selectConfig;
  window.upsertConfig = upsertConfig;
  window.syncFormToJson = syncFormToJson;
  window.saveRawConfigs = saveRawConfigs;
  window.deleteConfig = deleteConfig;

  window.createPrompt = createPrompt;
  window.openPrompt = openPrompt;
  window.savePrompt = savePrompt;
  window.deletePrompt = deletePrompt;
  window.loadPrompts = loadPrompts;

  window.addPricingRow = addPricingRow;
  window.savePricingRow = savePricingRow;
  window.saveAllPricingRows = saveAllPricingRows;
  window.deletePricingRow = deletePricingRow;

  window.cleanHistory = cleanHistory;
  window.refreshCaptcha = refreshCaptcha;
  window.loginAdmin = loginAdmin;
  window.logoutAdmin = logoutAdmin;
});