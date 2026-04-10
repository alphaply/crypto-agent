let configs = [];
let promptFiles = [];
let currentPrompt = '';

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

function renderConfigList() {
  const el = document.getElementById('config-list');
  if (!el) return;
  el.innerHTML = configs.map(cfg => `
    <div class="border border-slate-200 rounded-xl p-3 flex items-center justify-between gap-3">
      <div>
        <div class="font-bold text-slate-800 text-sm">${cfg.config_id}</div>
        <div class="text-xs text-slate-500">${cfg.symbol} · ${cfg.mode || 'STRATEGY'}</div>
      </div>
      <button onclick="deleteConfig('${cfg.config_id}')" class="px-2 py-1 rounded-lg border border-red-200 text-red-600 text-xs hover:bg-red-50">删除</button>
    </div>
  `).join('');
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
  const raw = document.getElementById('config-json');
  if (raw) raw.value = JSON.stringify(configs, null, 2);
}

async function deleteConfig(configId) {
  const depResp = await fetch(`/api/config/dependencies/${configId}`);
  const depData = await depResp.json();
  if (!depData.success) {
    toast(depData.message || '读取依赖失败', 'err');
    return;
  }

  const counts = depData.counts || {};
  const msg = `确认删除 ${configId}?\nchat_sessions=${counts.chat_sessions || 0}, open_mock_orders=${counts.open_mock_orders || 0}`;
  if (!confirm(msg)) return;

  const delResp = await fetch(`/api/config/delete/${configId}`, { method: 'POST' });
  const delData = await delResp.json();
  if (!delData.success) {
    toast(delData.message || '删除失败', 'err');
    return;
  }
  toast(delData.message || '删除成功');
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
  toast('配置保存成功');
  await loadConfigs();
}

function renderPromptList() {
  const el = document.getElementById('prompt-list');
  if (!el) return;
  el.innerHTML = promptFiles.map(name => `
    <button onclick="openPrompt('${name}')" class="w-full text-left px-2 py-1.5 rounded ${name === currentPrompt ? 'bg-blue-600 text-white' : 'hover:bg-slate-100 text-slate-700'} text-xs font-mono">${name}</button>
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
  document.getElementById('current-prompt').textContent = name;
  renderPromptList();
  const resp = await fetch(`/api/prompts/read?name=${encodeURIComponent(name)}`);
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '读取模板失败', 'err');
    return;
  }
  document.getElementById('prompt-editor').value = data.content || '';
}

async function createPrompt() {
  const name = (document.getElementById('new-prompt-name')?.value || '').trim();
  if (!name.endsWith('.txt')) {
    toast('文件名需以 .txt 结尾', 'err');
    return;
  }
  currentPrompt = name;
  document.getElementById('current-prompt').textContent = name;
  document.getElementById('prompt-editor').value = '';
  await savePrompt();
}

async function savePrompt() {
  if (!currentPrompt) {
    toast('请先选择模板', 'err');
    return;
  }
  const content = document.getElementById('prompt-editor')?.value || '';
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
  await loadPrompts();
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
  document.getElementById('current-prompt').textContent = '未选择';
  document.getElementById('prompt-editor').value = '';
  toast('模板已删除');
  await loadPrompts();
}

async function savePricing() {
  const model = document.getElementById('pricing-model')?.value || '';
  const input_price = document.getElementById('pricing-input')?.value || '0';
  const output_price = document.getElementById('pricing-output')?.value || '0';

  const resp = await fetch('/api/stats/pricing', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, input_price, output_price }),
  });
  const data = await resp.json();
  if (!data.success) {
    toast(data.message || '定价保存失败', 'err');
    return;
  }
  toast('定价保存成功');
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
  if (document.getElementById('config-list')) {
    await loadConfigs();
    await loadPrompts();
  }
});
