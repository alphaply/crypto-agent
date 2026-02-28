/**
 * Dashboard 核心业务逻辑
 */

// --- 状态与初始化 ---
let isAuthed = false;
let currentConfigs = [];
let currentPromptFile = '';

// --- 多 Agent Tab 切换逻辑 ---

function switchAgentTab(agentName) {
    // 1. 切换按钮样式
    const buttons = document.querySelectorAll('.agent-tab-btn');
    buttons.forEach(btn => {
        if (btn.id === `tab-btn-${agentName}`) {
            btn.classList.add('bg-white', 'shadow-sm', 'text-blue-600');
            btn.classList.remove('text-gray-500', 'hover:bg-gray-100');
        } else {
            btn.classList.remove('bg-white', 'shadow-sm', 'text-blue-600');
            btn.classList.add('text-gray-500', 'hover:bg-gray-100');
        }
    });

    // 2. 切换窗口显示
    const windows = document.querySelectorAll('.agent-window');
    windows.forEach(win => {
        if (win.id === `window-${agentName}`) {
            win.classList.remove('hidden');
        } else {
            win.classList.add('hidden');
        }
    });

    // 特殊处理对比视图
    const compareWin = document.getElementById('window-COMPARE');
    if (compareWin) {
        if (agentName === 'COMPARE') {
            compareWin.classList.remove('hidden');
            // 切换到对比视图时，强制对内部所有内容重新渲染 Markdown
            renderMarkdown(true, compareWin);
        } else {
            compareWin.classList.add('hidden');
        }
    }

    // 3. 正常渲染可见视窗
    renderMarkdown();
}

function renderMarkdown(force = false, root = document) {
    const containers = root.querySelectorAll('.markdown-content');
    containers.forEach(div => {
        if (!force && div.getAttribute('data-rendered') === 'true') return;
        
        // 如果是强制重绘，且已经渲染过，我们需要从原始备份或文本中恢复
        let rawContent = div.getAttribute('data-raw') || div.textContent;
        
        // 首次渲染时备份原始文本
        if (!div.getAttribute('data-raw')) {
            div.setAttribute('data-raw', rawContent);
        }

        div.classList.remove('whitespace-pre-wrap');
        div.classList.add('markdown-body');
        div.innerHTML = marked.parse(rawContent);
        div.setAttribute('data-rendered', 'true');
    });
}

document.addEventListener('DOMContentLoaded', function() {
    renderMarkdown();
});

// --- UI 通用组件 ---

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return console.log(message);
    
    const toast = document.createElement('div');
    toast.className = `pointer-events-auto transform rounded-lg px-4 py-3 shadow-lg flex items-center gap-3 min-w-[300px] toast-enter ${
        type === 'success' ? 'bg-white border-l-4 border-emerald-500 text-gray-800' : 'bg-white border-l-4 border-red-500 text-gray-800'
    }`;
    
    const icon = type === 'success' 
        ? `<svg class="w-5 h-5 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>`
        : `<svg class="w-5 h-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>`;

    toast.innerHTML = `${icon}<div class="flex-1 text-sm font-medium">${message}</div>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.remove('toast-enter');
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function handleCopy(text, e) { 
    if(!text || text === '-' || text === 'None') return;
    if(e) e.stopPropagation();
    navigator.clipboard.writeText(String(text)).then(() => {
        showToast(`已复制: ${text}`, 'success');
    }).catch(() => {
        showToast('复制失败', 'error');
    });
}

// 订单分页状态记录
const orderPages = {};

async function changeOrderPage(configId, delta) {
    if (!orderPages[configId]) orderPages[configId] = 1;
    const newPage = orderPages[configId] + delta;
    if (newPage < 1) return;

    const container = document.getElementById(`order-container-${configId}`);
    const indicator = document.getElementById(`page-indicator-${configId}`);
    
    // 视觉反馈：半透明加载感
    container.style.opacity = '0.5';

    try {
        const resp = await fetch(`/api/orders?config_id=${configId}&page=${newPage}`);
        const data = await resp.json();
        
        if (data.success && data.orders.length > 0) {
            orderPages[configId] = newPage;
            indicator.textContent = newPage;
            renderOrdersToContainer(configId, data.orders);
        } else if (data.success && data.orders.length === 0 && delta > 0) {
            showToast('已经是最后一页了', 'success');
        }
    } catch (e) {
        showToast('加载订单失败', 'error');
    } finally {
        container.style.opacity = '1';
    }
}

function renderOrdersToContainer(configId, orders) {
    const container = document.getElementById(`order-container-${configId}`);
    if (!container) return;

    container.innerHTML = orders.map(order => {
        const s = (order.side || '').toLowerCase();
        let sideClass = 'bg-gray-400';
        if (s.includes('buy')) sideClass = 'bg-emerald-500';
        else if (s.includes('sell')) sideClass = 'bg-red-500';
        else if (s.includes('close')) sideClass = 'bg-orange-500';
        else if (s.includes('cancel')) sideClass = 'bg-gray-400';

        const tpSlHtml = (order.take_profit > 0 || order.stop_loss > 0) 
            ? `<div class="mt-2 flex gap-3 text-[9px] font-bold">
                <span class="text-emerald-600" onclick="handleCopy('${order.take_profit}', event); event.stopPropagation();">🎯 TP: ${parseInt(order.take_profit)}</span>
                <span class="text-red-500" onclick="handleCopy('${order.stop_loss}', event); event.stopPropagation();">🛡️ SL: ${parseInt(order.stop_loss)}</span>
               </div>`
            : '';

        return `
            <div class="group bg-gray-50 hover:bg-white border border-gray-100 hover:border-blue-200 rounded-xl p-3 transition-all cursor-pointer" onclick="handleCopy('${order.entry_price}', event)">
                <div class="flex justify-between items-start mb-2">
                    <div class="flex items-center gap-2">
                        <span class="px-2 py-0.5 rounded-lg font-black uppercase text-[10px] shadow-sm text-white ${sideClass}">
                            ${order.side}
                        </span>
                        <span class="text-xs font-mono font-bold text-gray-700 group-hover:text-blue-600 transition-colors" title="点击复制价格">${order.entry_price}</span>
                    </div>
                    <span class="text-[9px] font-mono text-gray-400">${(order.timestamp || '').substring(11, 16)}</span>
                </div>
                <div class="text-[10px] text-gray-500 leading-relaxed line-clamp-2 group-hover:line-clamp-none transition-all">
                    ${order.reason}
                </div>
                ${tpSlHtml}
            </div>
        `;
    }).join('');
}

// --- 认证逻辑 ---

async function refreshCaptcha() {
    const img = document.getElementById('captcha-img');
    if (!img) return;
    try {
        const resp = await fetch('/api/chat/captcha');
        const data = await resp.json();
        if (data.success) {
            img.src = data.image;
        }
    } catch (e) {
        console.error('加载验证码失败');
    }
}

async function checkAuth(callback) {
    if (isAuthed) return callback();
    openAuthModal(callback);
}

function openAuthModal(onSuccess) {
    document.getElementById('auth-modal').classList.remove('hidden');
    refreshCaptcha(); // 弹出时自动刷新验证码
    window._authSuccessCallback = onSuccess;
    setTimeout(() => document.getElementById('auth-pass').focus(), 100);
}

function closeAuthModal() {
    document.getElementById('auth-modal').classList.add('hidden');
    document.getElementById('auth-pass').value = '';
    document.getElementById('auth-captcha').value = '';
}

async function submitAuth() {
    const pwd = document.getElementById('auth-pass').value;
    const captcha = document.getElementById('auth-captcha').value;
    
    if (!pwd) return showToast('请输入密码', 'error');
    if (!captcha) return showToast('请输入计算结果', 'error');

    try {
        const resp = await fetch('/api/chat/auth', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                password: pwd,
                captcha: captcha
            })
        });
        const data = await resp.json();
        if (resp.ok) {
            isAuthed = true;
            closeAuthModal();
            showToast('验证成功', 'success');
            if (window._authSuccessCallback) {
                window._authSuccessCallback();
                window._authSuccessCallback = null;
            }
        } else {
            showToast(data.message || '认证失败', 'error');
            refreshCaptcha(); // 失败后自动刷新验证码
            document.getElementById('auth-captcha').value = '';
        }
    } catch (e) {
        showToast('验证失败: ' + e, 'error');
    }
}

// --- 配置管理核心 (Config) ---

async function openConfigModal() {
    checkAuth(async () => {
        document.getElementById('config-modal').classList.remove('hidden');
        try {
            const resp = await fetch('/api/config/raw');
            const data = await resp.json();
            if (data.success) {
                currentConfigs = data.configs || [];
                document.getElementById('global-leverage').value = data.global.leverage;
                document.getElementById('global-scheduler').checked = data.global.enable_scheduler;
                renderSymbolList();
                updateRawJsonFromConfigs();
            }
        } catch (e) {
            showToast('加载配置失败', 'error');
        }
    });
}

function switchConfigTab(tab) {
    const isList = tab === 'list';
    const isPrompt = tab === 'prompt';
    const isRaw = tab === 'raw';

    document.getElementById('config-view-list').classList.toggle('hidden', !isList);
    document.getElementById('config-view-prompt').classList.toggle('hidden', !isPrompt);
    document.getElementById('config-view-raw').classList.toggle('hidden', !isRaw);
    
    document.getElementById('tab-btn-list').className = isList ? 'px-3 py-1 rounded-md bg-white shadow-sm text-blue-600' : 'px-3 py-1 rounded-md text-gray-500';
    document.getElementById('tab-btn-prompt').className = isPrompt ? 'px-3 py-1 rounded-md bg-white shadow-sm text-blue-600' : 'px-3 py-1 rounded-md text-gray-500';
    document.getElementById('tab-btn-raw').className = isRaw ? 'px-3 py-1 rounded-md bg-white shadow-sm text-blue-600' : 'px-3 py-1 rounded-md text-gray-500';
    
    if (isRaw) updateRawJsonFromConfigs();
    else if (isList) updateConfigsFromRawJson();
    else if (isPrompt) loadPrompts();
}

function renderSymbolList() {
    const listDiv = document.getElementById('symbol-config-list');
    const badge = document.getElementById('config-count-badge');
    if (!listDiv) return;
    listDiv.innerHTML = '';
    badge.textContent = currentConfigs.length;

    currentConfigs.forEach((conf, index) => {
        const isEnabled = conf.enabled !== false;
        const card = document.createElement('div');
        card.className = `config-card bg-white p-4 rounded-xl shadow-sm border ${isEnabled ? 'border-gray-200' : 'border-gray-100 opacity-60 bg-gray-50'}`;
        
        card.innerHTML = `
            <div class="flex justify-between items-start mb-3">
                <div>
                    <div class="flex items-center gap-2">
                        <span class="font-bold text-gray-900">${conf.symbol}</span>
                        <span class="text-[10px] px-1.5 py-0.5 rounded ${conf.mode === 'REAL' ? 'bg-red-100 text-red-600' : 'bg-blue-100 text-blue-600'} font-bold">${conf.mode}</span>
                    </div>
                    <div class="text-[10px] text-gray-400 font-mono mt-1">${conf.model}</div>
                </div>
                <label class="switch scale-75 origin-right">
                    <input type="checkbox" ${isEnabled ? 'checked' : ''} onchange="toggleSymbolEnabled(${index}, this.checked)">
                    <span class="slider"></span>
                </label>
            </div>
            <div class="flex justify-between items-center mt-4 pt-3 border-t border-gray-100">
                <div class="text-[10px] text-gray-500 flex gap-3">
                    <span>⚡ ${conf.leverage || '-'}x</span>
                    <span>🌡️ ${conf.temperature || '-'}</span>
                </div>
                <div class="flex gap-2">
                    <button onclick="editSymbol(${index})" class="text-blue-600 hover:bg-blue-50 p-1.5 rounded-lg transition-colors">
                        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" stroke-width="2"/></svg>
                    </button>
                    <button onclick="deleteSymbol(${index})" class="text-red-400 hover:text-red-600 p-1.5 rounded-lg transition-colors">
                        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" stroke-width="2"/></svg>
                    </button>
                </div>
            </div>
        `;
        listDiv.appendChild(card);
    });
}

function toggleSymbolEnabled(index, enabled) {
    currentConfigs[index].enabled = enabled;
    renderSymbolList();
}

function deleteSymbol(index) {
    if (confirm(`确定要删除 ${currentConfigs[index].symbol} 的配置吗？`)) {
        currentConfigs.splice(index, 1);
        renderSymbolList();
    }
}

// --- 编辑与添加 (修复后的关键部分) ---

function toggleSumSection(enabled) {
    const section = document.getElementById('sum-section');
    if (!section) return;
    section.style.opacity = enabled ? '1' : '0.4';
    section.style.pointerEvents = enabled ? 'auto' : 'none';
}

async function editSymbol(index) {
    const conf = currentConfigs[index];
    if (!conf) return;

    document.getElementById('symbol-modal-title').textContent = '编辑交易对';
    document.getElementById('edit-config-index').value = index;
    
    document.getElementById('edit-config-id').value = conf.config_id || '';
    document.getElementById('edit-symbol').value = conf.symbol || '';
    document.getElementById('edit-mode').value = conf.mode || 'STRATEGY';
    document.getElementById('edit-leverage').value = conf.leverage || '';
    document.getElementById('edit-temp').value = conf.temperature || '';
    document.getElementById('edit-model').value = conf.model || '';
    document.getElementById('edit-api-base').value = conf.api_base || '';
    document.getElementById('edit-api-key').value = '';
    document.getElementById('edit-bn-key').value = '';
    document.getElementById('edit-bn-secret').value = '';

    const sum = conf.summarizer || {};
    const hasSum = !!sum.model;
    document.getElementById('enable-sum-cfg').checked = hasSum;
    toggleSumSection(hasSum);
    document.getElementById('edit-sum-model').value = sum.model || '';
    document.getElementById('edit-sum-base').value = sum.api_base || '';
    document.getElementById('edit-sum-key').value = '';

    await refreshPromptSelect(conf.prompt_file);
    document.getElementById('symbol-edit-modal').classList.remove('hidden');
}

async function addNewSymbolConfig() {
    document.getElementById('symbol-modal-title').textContent = '添加新配置';
    document.getElementById('edit-config-index').value = '-1';
    
    const newId = `cfg-${Date.now()}`;
    document.getElementById('edit-config-id').value = newId;
    document.getElementById('edit-symbol').value = '';
    document.getElementById('edit-mode').value = 'STRATEGY';
    document.getElementById('edit-leverage').value = '10';
    document.getElementById('edit-temp').value = '0.5';
    document.getElementById('edit-model').value = 'gpt-4o-mini';
    document.getElementById('edit-api-base').value = '';
    document.getElementById('edit-api-key').value = '';
    document.getElementById('edit-bn-key').value = '';
    document.getElementById('edit-bn-secret').value = '';
    
    document.getElementById('enable-sum-cfg').checked = false;
    toggleSumSection(false);
    document.getElementById('edit-sum-model').value = '';
    document.getElementById('edit-sum-base').value = '';
    document.getElementById('edit-sum-key').value = '';

    await refreshPromptSelect('strategy.txt');
    document.getElementById('symbol-edit-modal').classList.remove('hidden');
}

function applySymbolEdit() {
    const idx = parseInt(document.getElementById('edit-config-index').value);
    const symbol = document.getElementById('edit-symbol').value.trim();
    if (!symbol) return showToast('请输入交易对', 'error');

    const newConf = {
        config_id: document.getElementById('edit-config-id').value,
        symbol: symbol,
        mode: document.getElementById('edit-mode').value,
        prompt_file: document.getElementById('edit-prompt-file').value,
        leverage: parseInt(document.getElementById('edit-leverage').value) || 10,
        temperature: parseFloat(document.getElementById('edit-temp').value) || 0.5,
        model: document.getElementById('edit-model').value,
        api_base: document.getElementById('edit-api-base').value,
        enabled: idx === -1 ? true : (currentConfigs[idx].enabled !== false)
    };

    const key = document.getElementById('edit-api-key').value;
    if (key) newConf.api_key = key;
    else if (idx !== -1) newConf.api_key = currentConfigs[idx].api_key;

    const bnKey = document.getElementById('edit-bn-key').value;
    const bnSec = document.getElementById('edit-bn-secret').value;
    if (bnKey) newConf.binance_api_key = bnKey;
    else if (idx !== -1) newConf.binance_api_key = currentConfigs[idx].binance_api_key;
    if (bnSec) newConf.binance_secret = bnSec;
    else if (idx !== -1) newConf.binance_secret = currentConfigs[idx].binance_secret;

    if (document.getElementById('enable-sum-cfg').checked) {
        newConf.summarizer = {
            model: document.getElementById('edit-sum-model').value,
            api_base: document.getElementById('edit-sum-base').value,
            api_key: document.getElementById('edit-sum-key').value || (idx !== -1 && currentConfigs[idx].summarizer ? currentConfigs[idx].summarizer.api_key : "")
        };
    } else {
        delete newConf.summarizer;
    }

    if (idx === -1) currentConfigs.push(newConf);
    else currentConfigs[idx] = newConf;

    renderSymbolList();
    closeSymbolEditModal();
    showToast('已更新到临时列表', 'success');
}

// --- Prompt 辅助 ---

async function refreshPromptSelect(selectedFile) {
    const select = document.getElementById('edit-prompt-file');
    if (!select) return;
    select.innerHTML = '<option>加载中...</option>';
    try {
        const resp = await fetch('/api/prompts/list');
        const data = await resp.json();
        if (data.success) {
            select.innerHTML = '';
            data.files.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f;
                opt.textContent = f;
                if (f === selectedFile) opt.selected = true;
                select.appendChild(opt);
            });
        }
    } catch (e) {
        select.innerHTML = '<option value="">加载失败</option>';
    }
}

// --- 外部文件引用补充逻辑 ---

function updateRawJsonFromConfigs() {
    document.getElementById('config-json').value = JSON.stringify(currentConfigs, null, 4);
}

function updateConfigsFromRawJson() {
    try {
        currentConfigs = JSON.parse(document.getElementById('config-json').value);
        renderSymbolList();
    } catch(e) {}
}

async function saveConfigFromUI() {
    if (!document.getElementById('config-view-raw').classList.contains('hidden')) {
        try {
            currentConfigs = JSON.parse(document.getElementById('config-json').value);
        } catch (e) {
            return showToast('JSON 格式错误，无法保存', 'error');
        }
    }
    const globalSettings = {
        leverage: parseInt(document.getElementById('global-leverage').value) || 10,
        enable_scheduler: document.getElementById('global-scheduler').checked
    };
    try {
        const resp = await fetch('/api/config/save', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({configs: currentConfigs, global: globalSettings})
        });
        const data = await resp.json();
        if (data.success) {
            showToast('配置已永久保存并重载', 'success');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast('保存失败: ' + data.message, 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    }
}

// --- Prompt 模板管理逻辑 ---

async function loadPrompts() {
    const listDiv = document.getElementById('prompt-file-list');
    if (!listDiv) return;
    listDiv.innerHTML = '<div class="text-[10px] text-gray-400 p-2">加载中...</div>';
    try {
        const resp = await fetch('/api/prompts/list');
        const data = await resp.json();
        if (data.success) {
            listDiv.innerHTML = '';
            data.files.forEach(name => {
                const btn = document.createElement('button');
                btn.className = `text-left px-3 py-2 rounded-lg text-[11px] font-mono transition-all ${currentPromptFile === name ? 'bg-blue-600 text-white shadow-md' : 'hover:bg-white text-gray-600'}`;
                btn.textContent = name;
                btn.onclick = () => openPrompt(name);
                listDiv.appendChild(btn);
            });
        }
    } catch (e) {
        showToast('加载列表失败', 'error');
    }
}

async function openPrompt(name) {
    currentPromptFile = name;
    document.getElementById('current-prompt-name').textContent = name;
    document.getElementById('prompt-editor').value = '读取中...';
    
    const isCore = ['real.txt', 'strategy.txt'].includes(name);
    const delBtn = document.getElementById('btn-delete-prompt');
    if (delBtn) delBtn.classList.toggle('hidden', isCore);

    try {
        const resp = await fetch(`/api/prompts/read?name=${name}`);
        const data = await resp.json();
        if (data.success) {
            document.getElementById('prompt-editor').value = data.content;
            loadPrompts(); 
        }
    } catch (e) {
        showToast('读取内容失败', 'error');
    }
}

async function saveCurrentPrompt() {
    if (!currentPromptFile) return showToast('请先选择或创建文件', 'error');
    const content = document.getElementById('prompt-editor').value;
    try {
        const resp = await fetch('/api/prompts/save', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: currentPromptFile, content: content})
        });
        const data = await resp.json();
        if (data.success) {
            showToast('Prompt 已保存', 'success');
        } else {
            showToast(data.message, 'error');
        }
    } catch (e) {
        showToast('保存失败', 'error');
    }
}

function createNewPrompt() {
    const name = prompt('请输入新 Prompt 的文件名 (例如: experimental.txt):');
    if (!name) return;
    if (!name.endsWith('.txt')) return showToast('必须以 .txt 结尾', 'error');
    currentPromptFile = name;
    document.getElementById('current-prompt-name').textContent = name;
    document.getElementById('prompt-editor').value = '# 在此输入 Prompt 内容...';
    saveCurrentPrompt().then(() => loadPrompts());
}

async function deleteCurrentPrompt() {
    if (!currentPromptFile) return;
    if (!confirm(`确定要删除 ${currentPromptFile} 吗？`)) return;
    try {
        const resp = await fetch('/api/prompts/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: currentPromptFile})
        });
        const data = await resp.json();
        if (data.success) {
            showToast('文件已删除', 'success');
            currentPromptFile = '';
            document.getElementById('current-prompt-name').textContent = '未选择文件';
            document.getElementById('prompt-editor').value = '';
            loadPrompts();
        }
    } catch (e) {
        showToast('删除失败', 'error');
    }
}

function closeConfigModal() {
    document.getElementById('config-modal').classList.add('hidden');
}

function closeSymbolEditModal() {
    document.getElementById('symbol-edit-modal').classList.add('hidden');
}

function formatConfigJson() {
    const area = document.getElementById('config-json');
    try {
        area.value = JSON.stringify(JSON.parse(area.value), null, 4);
    } catch (e) {
        showToast('JSON 格式不正确', 'error');
    }
}

// --- 删除历史 ---

async function refreshDeleteCaptcha() {
    const img = document.getElementById('delete-captcha-img');
    if (!img) return;
    try {
        const resp = await fetch('/api/chat/captcha');
        const data = await resp.json();
        if (data.success) {
            img.src = data.image;
        }
    } catch (e) {
        console.error('加载验证码失败');
    }
}

function openDeleteModal() {
    document.getElementById('delete-modal').classList.remove('hidden');
    refreshDeleteCaptcha();
    setTimeout(() => document.getElementById('admin-pass').focus(), 100);
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    document.getElementById('admin-pass').value = '';
    document.getElementById('delete-captcha').value = '';
}

async function confirmDeleteAction() {
    const pwd = document.getElementById('admin-pass').value;
    const captcha = document.getElementById('delete-captcha').value;
    const symbol = document.getElementById('symbolSelect').value;
    
    if (!pwd) return showToast('请输入管理员密码', 'error');
    if (!captcha) return showToast('请输入验证码', 'error');

    try {
        const response = await fetch('/api/clean_history', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                symbol: symbol, 
                password: pwd,
                captcha: captcha
            })
        });
        const result = await response.json();
        if (result.success) {
            closeDeleteModal();
            showToast(result.message, 'success');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(result.message, 'error');
            refreshDeleteCaptcha();
            document.getElementById('delete-captcha').value = '';
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    }
}

// --- 订单筛选 ---

function filterOrders(agentName) {
    const rows = document.querySelectorAll('#orderTable tbody tr');
    let visibleCount = 0;
    rows.forEach(row => {
        const rowAgent = row.getAttribute('data-agent');
        if (agentName === 'ALL' || rowAgent === agentName) {
            row.style.display = '';
            visibleCount++;
        } else {
            row.style.display = 'none';
        }
    });
    document.getElementById('orderCount').textContent = visibleCount;
}
