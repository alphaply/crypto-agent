/**
 * Dashboard 核心业务逻辑
 */

// --- 状态与初始化 ---
let isAuthed = false;
let currentConfigs = [];
let currentPromptFile = '';
const markdownRenderQueue = new WeakSet();
let markdownObserver = null;
const statsRequests = new Map();
let compareEquityChart = null;

function getResolvedTheme() {
    if (window.AppTheme && typeof window.AppTheme.getResolvedTheme === 'function') {
        return window.AppTheme.getResolvedTheme();
    }
    return document.documentElement.getAttribute('data-theme') || 'light';
}

function getComparePalette() {
    const isDark = getResolvedTheme() === 'dark';
    return isDark
        ? ['#60a5fa', '#34d399', '#f87171', '#a78bfa', '#fbbf24', '#22d3ee', '#fb7185', '#4ade80', '#c4b5fd', '#38bdf8']
        : ['#2563eb', '#059669', '#dc2626', '#7c3aed', '#d97706', '#0f766e', '#0891b2', '#4338ca', '#be123c', '#65a30d'];
}

function getChartToken(name) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || '#94a3b8';
}

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
            renderMarkdown(true, compareWin);
            observeMarkdown(compareWin);
            loadCompareEquityChart();
        } else {
            compareWin.classList.add('hidden');
        }
    }

    const activeWindow = document.getElementById(`window-${agentName}`);
    if (activeWindow) {
        renderMarkdown(false, activeWindow);
        observeMarkdown(activeWindow);
    }

    if (agentName !== 'COMPARE') {
        loadAgentStats(agentName);
    }
}

function getCurrentSymbol() {
    const sel = document.getElementById('globalSymbolSelect');
    if (sel && sel.value) return sel.value;
    const urlParams = new URLSearchParams(window.location.search);
    return urlParams.get('symbol') || 'BTC/USDT';
}

function toggleCompareAll(checked) {
    const checks = document.querySelectorAll('.compare-config-check');
    checks.forEach(c => {
        c.checked = checked;
    });
    loadCompareEquityChart();
}

async function loadCompareEquityChart() {
    const canvas = document.getElementById('compare-equity-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    const selected = Array.from(document.querySelectorAll('.compare-config-check:checked')).map(x => x.value);
    if (selected.length === 0) {
        if (compareEquityChart) {
            compareEquityChart.destroy();
            compareEquityChart = null;
        }
        return;
    }

    const symbol = encodeURIComponent(getCurrentSymbol());
    const ids = encodeURIComponent(selected.join(','));

    try {
        const resp = await fetch(`/api/stats/equity_compare?symbol=${symbol}&config_ids=${ids}`);
        const data = await resp.json();
        if (!data.success || !data.series || data.series.length === 0) {
            return;
        }

        const labelSet = new Set();
        data.series.forEach(s => (s.points || []).forEach(p => labelSet.add(p.date)));
        const labels = Array.from(labelSet).sort();

        const palette = getComparePalette();
        const datasets = data.series.map((s, i) => {
            const map = new Map((s.points || []).map(p => [p.date, Number(p.equity)]));
            return {
                label: s.label || s.config_id,
                data: labels.map(d => map.has(d) ? map.get(d) : null),
                borderColor: palette[i % palette.length],
                backgroundColor: palette[i % palette.length],
                borderWidth: 2,
                spanGaps: true,
                tension: 0.2,
                pointRadius: 0,
            };
        });

        if (compareEquityChart) compareEquityChart.destroy();
        compareEquityChart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: getChartToken('--text-secondary')
                        }
                    },
                    tooltip: {
                        backgroundColor: getChartToken('--surface-3'),
                        borderColor: getChartToken('--border-soft'),
                        borderWidth: 1,
                        titleColor: getChartToken('--text-primary'),
                        bodyColor: getChartToken('--text-secondary'),
                        callbacks: {
                            label: function(ctx) {
                                const v = ctx.raw;
                                return `${ctx.dataset.label}: ${v == null ? '-' : Number(v).toFixed(2)}`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            maxTicksLimit: 8,
                            color: getChartToken('--text-muted')
                        },
                        grid: {
                            color: getChartToken('--border-soft')
                        }
                    },
                    y: {
                        ticks: {
                            color: getChartToken('--text-muted'),
                            callback: function(value) { return Number(value).toFixed(0); }
                        },
                        grid: {
                            color: getChartToken('--border-soft')
                        }
                    }
                }
            }
        });
    } catch (e) {
        console.error('Load compare equity chart error:', e);
    }
}

function scheduleIdleRender(task) {
    if (typeof window.requestIdleCallback === 'function') {
        window.requestIdleCallback(task, { timeout: 200 });
        return;
    }
    window.setTimeout(task, 16);
}

function renderMarkdownNode(div, force = false) {
    if (!div || !window.marked) return;
    if (!force && div.getAttribute('data-rendered') === 'true') return;
    if (!force && markdownRenderQueue.has(div)) return;

    markdownRenderQueue.add(div);
    scheduleIdleRender(() => {
        const rawContent = div.getAttribute('data-raw') || div.textContent;
        if (!div.getAttribute('data-raw')) {
            div.setAttribute('data-raw', rawContent);
        }

        div.classList.remove('whitespace-pre-wrap');
        div.classList.add('markdown-body');
        div.innerHTML = marked.parse(rawContent);
        div.setAttribute('data-rendered', 'true');
        markdownRenderQueue.delete(div);
    });
}

function renderMarkdown(force = false, root = document) {
    const containers = root.querySelectorAll('.markdown-content');
    containers.forEach(div => {
        if (force || div.dataset.markdownLazy === 'off') {
            renderMarkdownNode(div, force);
            return;
        }

        if (div.closest('details') && !div.closest('details').open) return;
        renderMarkdownNode(div, false);
    });
}

function observeMarkdown(root = document) {
    if (!('IntersectionObserver' in window)) {
        renderMarkdown(false, root);
        return;
    }

    if (!markdownObserver) {
        markdownObserver = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                renderMarkdownNode(entry.target);
                markdownObserver.unobserve(entry.target);
            });
        }, { rootMargin: '120px 0px' });
    }

    const containers = root.querySelectorAll('.markdown-content');
    containers.forEach(div => {
        if (div.getAttribute('data-rendered') === 'true') return;
        if (div.closest('details') && !div.closest('details').open) return;
        markdownObserver.observe(div);
    });
}

document.addEventListener('DOMContentLoaded', function() {
    const firstWindow = document.querySelector('.agent-window:not(.hidden)');
    if (firstWindow) {
        renderMarkdown(false, firstWindow);
        observeMarkdown(firstWindow);
        const configId = firstWindow.getAttribute('data-config-id');
        if (configId) loadAgentStats(configId);
    }

    document.addEventListener('toggle', event => {
        const target = event.target;
        if (!(target instanceof HTMLDetailsElement) || !target.open) return;
        observeMarkdown(target);
        renderMarkdown(false, target);
    }, true);

    window.addEventListener('app-theme-change', () => {
        if (document.getElementById('window-COMPARE') && !document.getElementById('window-COMPARE').classList.contains('hidden')) {
            loadCompareEquityChart();
        }
    });
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
        const resp = await fetch(`/api/orders?config_id=${configId}&page=${newPage}&per_page=10`);
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

        let reasonHtml;
        const reasonText = order.reason || '';
        const knownEmojis = ['✅', '❌'];
        let emoji = '';
        let text = reasonText;

        for (const e of knownEmojis) {
            if (reasonText.startsWith(e)) {
                emoji = e;
                text = reasonText.substring(e.length).trim();
                break;
            }
        }

        if (emoji) {
            reasonHtml = `
                <div class="flex items-start gap-1.5">
                    <span class="mt-px">${emoji}</span>
                    <span>${text}</span>
                </div>
            `;
        } else {
            reasonHtml = `<div>${reasonText}</div>`;
        }

        return `
            <div class="group bg-gray-50 hover:bg-white border border-gray-100 hover:border-blue-200 rounded-xl p-3 transition-all cursor-pointer" onclick="handleCopy('${order.entry_price}', event)">
                <div class="flex justify-between items-start mb-2">
                    <div class="flex items-center gap-2">
                        <span class="px-2 py-0.5 rounded-lg font-black uppercase text-[10px] shadow-sm text-white ${sideClass}">
                            ${order.side}
                        </span>
                        <span class="text-xs font-mono font-bold text-gray-700 group-hover:text-blue-600 transition-colors" title="点击复制价格">${order.entry_price}</span>
                        <span class="text-[9px] font-mono text-gray-400 bg-gray-100 px-1 rounded ml-1" title="订单 ID">${order.order_id}</span>
                    </div>
                    <span class="text-[9px] font-mono text-gray-400">${(order.timestamp || '').substring(11, 16)}</span>
                </div>
                <div class="text-[10px] text-gray-500 leading-relaxed line-clamp-2 group-hover:line-clamp-none transition-all">
                    ${reasonHtml}
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
    const captchaContainer = document.getElementById('auth-captcha-container');
    const isCaptchaVisible = !captchaContainer.classList.contains('hidden');
    const captcha = document.getElementById('auth-captcha').value;
    
    if (!pwd) return showToast('请输入密码', 'error');
    if (isCaptchaVisible && !captcha) return showToast('请输入验证码', 'error');

    try {
        const resp = await fetch('/api/chat/auth', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                password: pwd,
                captcha: isCaptchaVisible ? captcha : ''
            })
        });
        const data = await resp.json();
        
        if (data.need_captcha) {
            captchaContainer.classList.remove('hidden');
            refreshCaptcha();
            document.getElementById('auth-captcha').value = '';
        } else {
            captchaContainer.classList.add('hidden');
        }

        if (resp.ok && data.success) {
            isAuthed = true;
            closeAuthModal();
            showToast('验证成功', 'success');
            if (window._authSuccessCallback) {
                window._authSuccessCallback();
                window._authSuccessCallback = null;
            }
        } else {
            showToast(data.message || '认证失败', 'error');
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
                        <span class="text-[10px] px-1.5 py-0.5 rounded ${
                            conf.mode === 'REAL' ? 'bg-red-100 text-red-600' : 
                            (conf.mode === 'SPOT_DCA' ? 'bg-emerald-100 text-emerald-600' : 'bg-blue-100 text-blue-600')
                        } font-bold">${conf.mode}</span>
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

function onModeChange(mode) {
    const isDca = mode === 'SPOT_DCA';
    
    document.getElementById('section-interval').classList.toggle('hidden', isDca);
    document.getElementById('section-dca').classList.toggle('hidden', !isDca);

    // 处理默认 prompt 的自动选择
    if (isDca) {
        const targetPrompt = 'dca.txt';
        const select = document.getElementById('edit-prompt-file');
        for (let i = 0; i < select.options.length; i++) {
            if (select.options[i].value === targetPrompt) {
                select.selectedIndex = i;
                break;
            }
        }
    }
}

function onDcaFreqChange(freq) {
    document.getElementById('div-dca-weekday').classList.toggle('hidden', freq !== '1w');
}

async function editSymbol(index) {
    const conf = currentConfigs[index];
    if (!conf) return;

    document.getElementById('symbol-modal-title').textContent = '编辑交易对';
    document.getElementById('edit-config-index').value = index;
    
    document.getElementById('edit-config-id').value = conf.config_id || '';
    document.getElementById('edit-symbol').value = conf.symbol || '';
    const mode = conf.mode || 'STRATEGY';
    document.getElementById('edit-mode').value = mode;
    document.getElementById('edit-leverage').value = conf.leverage || '';
    document.getElementById('edit-temp').value = conf.temperature || '';
    document.getElementById('edit-model').value = conf.model || '';
    document.getElementById('edit-api-base').value = conf.api_base || '';
    document.getElementById('edit-api-key').value = '';
    document.getElementById('edit-bn-key').value = '';
    document.getElementById('edit-bn-secret').value = '';

    // 运行周期与定投参数
    document.getElementById('edit-interval').value = conf.run_interval || (mode === 'REAL' ? 15 : 60);
    document.getElementById('edit-dca-freq').value = conf.dca_freq || '1d';
    document.getElementById('edit-dca-time').value = conf.dca_time || '08:00';
    document.getElementById('edit-dca-weekday').value = conf.dca_weekday || '0';
    document.getElementById('edit-dca-amount').value = conf.dca_amount || conf.dca_budget || '100';

    onModeChange(mode);
    onDcaFreqChange(conf.dca_freq || '1d');

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

    // 默认值
    document.getElementById('edit-interval').value = 60;
    document.getElementById('edit-dca-freq').value = '1d';
    document.getElementById('edit-dca-time').value = '08:00';
    document.getElementById('edit-dca-amount').value = '100';

    onModeChange('STRATEGY');

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

    const mode = document.getElementById('edit-mode').value;
    const newConf = {
        config_id: document.getElementById('edit-config-id').value,
        symbol: symbol,
        mode: mode,
        prompt_file: document.getElementById('edit-prompt-file').value,
        leverage: parseInt(document.getElementById('edit-leverage').value) || 10,
        temperature: parseFloat(document.getElementById('edit-temp').value) || 0.5,
        model: document.getElementById('edit-model').value,
        api_base: document.getElementById('edit-api-base').value,
        enabled: idx === -1 ? true : (currentConfigs[idx].enabled !== false)
    };

    // 专属参数
    if (mode === 'SPOT_DCA') {
        newConf.dca_freq = document.getElementById('edit-dca-freq').value;
        newConf.dca_time = document.getElementById('edit-dca-time').value;
        newConf.dca_amount = parseFloat(document.getElementById('edit-dca-amount').value) || 100;
        if (newConf.dca_freq === '1w') {
            newConf.dca_weekday = parseInt(document.getElementById('edit-dca-weekday').value);
        }
    } else {
        newConf.run_interval = Math.max(15, parseInt(document.getElementById('edit-interval').value) || 15);
    }

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
    const captchaContainer = document.getElementById('delete-captcha-container');
    const isCaptchaVisible = !captchaContainer.classList.contains('hidden');
    const captcha = document.getElementById('delete-captcha').value;
    let symbol = document.getElementById('symbolSelect') ? document.getElementById('symbolSelect').value : null;
    if (!symbol) {
        const urlParams = new URLSearchParams(window.location.search);
        symbol = urlParams.get('symbol') || 'BTC/USDT';
    }
    
    if (!pwd) return showToast('请输入管理员密码', 'error');
    if (isCaptchaVisible && !captcha) return showToast('请输入验证码', 'error');

    try {
        const response = await fetch('/api/clean_history', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                symbol: symbol, 
                password: pwd,
                captcha: isCaptchaVisible ? captcha : ''
            })
        });
        const result = await response.json();
        
        if (result.need_captcha) {
            captchaContainer.classList.remove('hidden');
            refreshDeleteCaptcha();
            document.getElementById('delete-captcha').value = '';
        } else {
            captchaContainer.classList.add('hidden');
        }

        if (result.success) {
            closeDeleteModal();
            showToast(result.message, 'success');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(result.message, 'error');
        }
    } catch (e) {
        showToast('网络请求失败', 'error');
    }
}

// --- 记录编辑 (Edit Daily Summary) ---

async function refreshEditDailySummaryCaptcha() {
    const img = document.getElementById('edit-daily-summary-captcha-img');
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

function openEditDailySummaryModal(dateStr, configId, event) {
    if (event) event.stopPropagation();
    document.getElementById('edit-daily-summary-modal').classList.remove('hidden');
    refreshEditDailySummaryCaptcha();
    
    document.getElementById('edit-daily-summary-date').value = dateStr;
    document.getElementById('edit-daily-summary-config').value = configId;
    
    const wrap = document.getElementById(`daily-content-wrap-${configId}-${dateStr}`);
    if (wrap) {
        const contentBlock = wrap.querySelector('.daily-content-block');
        document.getElementById('edit-daily-summary-content').value = contentBlock ? (contentBlock.getAttribute('data-raw') || contentBlock.textContent) : '';
    } else {
        document.getElementById('edit-daily-summary-content').value = '';
    }
    
    setTimeout(() => document.getElementById('edit-daily-summary-pass').focus(), 100);
}

function closeEditDailySummaryModal() {
    document.getElementById('edit-daily-summary-modal').classList.add('hidden');
    document.getElementById('edit-daily-summary-pass').value = '';
    document.getElementById('edit-daily-summary-captcha').value = '';
}

async function submitEditDailySummary() {
    const dateStr = document.getElementById('edit-daily-summary-date').value;
    const configId = document.getElementById('edit-daily-summary-config').value;
    const content = document.getElementById('edit-daily-summary-content').value;
    const pwd = document.getElementById('edit-daily-summary-pass').value;
    
    const captchaContainer = document.getElementById('edit-daily-summary-captcha-container');
    const isCaptchaVisible = !captchaContainer.classList.contains('hidden');
    const captcha = document.getElementById('edit-daily-summary-captcha').value;
    
    if (!pwd) return showToast('请输入管理员密码', 'error');
    if (isCaptchaVisible && !captcha) return showToast('请输入验证码', 'error');

    try {
        const response = await fetch('/api/daily_summary/update', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                date: dateStr,
                config_id: configId,
                summary: content,
                password: pwd,
                captcha: isCaptchaVisible ? captcha : ''
            })
        });
        const result = await response.json();
        
        if (result.need_captcha) {
            captchaContainer.classList.remove('hidden');
            refreshEditDailySummaryCaptcha();
            document.getElementById('edit-daily-summary-captcha').value = '';
        } else {
            captchaContainer.classList.add('hidden');
        }

        if (result.success) {
            closeEditDailySummaryModal();
            showToast(result.message, 'success');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(result.message, 'error');
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

// --- Agent 做单统计 & 实盘仓位 ---

const loadedStats = new Set();

async function loadAgentStats(configId) {
    if (loadedStats.has(configId)) return;
    if (statsRequests.has(configId)) return statsRequests.get(configId);
    loadedStats.add(configId);

    const request = (async () => {
    try {
        const resp = await fetch(`/api/stats/agent/${configId}`);
        const data = await resp.json();
        if (!data.success) return;

        const s = data.stats;
        const el = (id) => document.getElementById(`${id}-${configId}`);

        // 基础/合约指标 (如果存在)
        const totalEl = el('stat-total');
        if (totalEl) totalEl.textContent = s.total_orders;

        const lsEl = el('stat-ls');
        if (lsEl) lsEl.textContent = s.long_short_ratio;

        const buyEl = el('stat-buy');
        if (buyEl) buyEl.textContent = `多:${s.buy_count}`;

        const sellEl = el('stat-sell');
        if (sellEl) sellEl.textContent = `空:${s.sell_count}`;

        const cancelEl = el('stat-cancel');
        if (cancelEl) {
            cancelEl.textContent = `${s.cancel_rate}%`;
            cancelEl.className = `text-lg font-black ${s.cancel_rate > 30 ? 'text-red-500' : (s.cancel_rate > 10 ? 'text-amber-500' : 'text-emerald-600')}`;
        }
        
        const cancelCountEl = el('stat-cancel-count');
        if (cancelCountEl) cancelCountEl.textContent = `${s.cancel_count} 次取消`;

        const closeEl = el('stat-close');
        if (closeEl) closeEl.textContent = s.close_count;

        // 定投专项指标 (如果存在)
        if (s.mode === 'SPOT_DCA' && s.dca_stats) {
            const dcaInvEl = el('stat-dca-invested');
            if (dcaInvEl) dcaInvEl.textContent = s.dca_stats.total_invested.toFixed(2);
            
            const dcaBuyEl = el('stat-dca-buy-count');
            if (dcaBuyEl) dcaBuyEl.textContent = s.dca_stats.buy_count;
            
            const dcaCostEl = el('stat-dca-avg-cost');
            if (dcaCostEl) dcaCostEl.textContent = s.dca_stats.avg_cost.toFixed(4);
            
            const dcaQtyEl = el('stat-dca-qty');
            if (dcaQtyEl) dcaQtyEl.textContent = s.dca_stats.total_qty.toFixed(5);

            const dcaAmtEl = el('stat-dca-amount');
            if (dcaAmtEl) dcaAmtEl.textContent = s.dca_stats.dca_amount_per;
        }

        const periodEl = el('stat-period');
        if (periodEl && s.first_order_at && s.last_order_at) {
            periodEl.textContent = `${s.first_order_at.substring(5, 10)} ~ ${s.last_order_at.substring(5, 10)}`;
        }
    } catch (e) {
        console.error('Load agent stats error:', e);
    }

    // 自动加载仓位面板 (REAL 和 STRATEGY 模式)
    const win = document.getElementById(`window-${configId}`);
    const mode = win ? win.getAttribute('data-mode') : '';
    if (mode === 'REAL' || mode === 'STRATEGY') {
        loadPositionStats(configId);
    }
    })().finally(() => {
        statsRequests.delete(configId);
    });

    statsRequests.set(configId, request);
    return request;
}

async function loadPositionStats(configId) {
    const panel = document.getElementById(`position-panel-${configId}`);
    if (!panel) return;
    panel.classList.remove('hidden');

    const container = document.getElementById(`pos-container-${configId}`);
    container.innerHTML = '<div class="text-center text-gray-500 text-xs py-2">⏳ 加载中...</div>';

    try {
        const resp = await fetch(`/api/stats/position/${configId}`);
        const data = await resp.json();
        if (!data.success) {
            container.innerHTML = `<div class="text-center text-red-400 text-xs py-2">❌ ${data.message}</div>`;
            return;
        }

        // 余额
        const balEl = document.getElementById(`pos-balance-${configId}`);
        if (balEl && data.balance) balEl.textContent = `💰 ${data.balance.toFixed(2)} USDT`;

        // 仓位渲染
        if (data.positions && data.positions.length > 0) {
            container.innerHTML = data.positions.map(p => {
                const isLong = p.side === 'LONG';
                const pnlColor = p.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                const sideColor = isLong ? 'bg-emerald-500' : 'bg-red-500';
                const notional = Number(p.notional || 0);
                const qty = Number(p.qty || p.contracts || 0);
                const roiPct = Number(p.roi_pct ?? p.pnl_pct ?? 0);
                const lev = Number(p.leverage || 1);
                return `
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between bg-white/5 rounded-xl px-3 sm:px-4 py-2.5 sm:py-3 gap-1.5 sm:gap-3">
                        <div class="flex items-center gap-2 sm:gap-3">
                            <span class="px-1.5 sm:px-2 py-0.5 rounded-md text-[9px] sm:text-[10px] font-black text-white ${sideColor} flex-shrink-0">${p.side}</span>
                            <div class="min-w-0">
                                <div class="text-[11px] sm:text-xs font-bold">仓位: ${notional.toFixed(2)} USDT</div>
                                <div class="text-[8px] sm:text-[9px] text-gray-400 truncate">数量: ${qty.toFixed(4)} | 开仓: ${p.entry_price} | 标记: ${p.mark_price}</div>
                            </div>
                        </div>
                        <div class="text-right sm:text-right pl-6 sm:pl-0 flex-shrink-0">
                            <div class="text-xs sm:text-sm font-black ${pnlColor}">${p.unrealized_pnl >= 0 ? '+' : ''}${p.unrealized_pnl} USDT</div>
                            <div class="text-[8px] sm:text-[9px] ${pnlColor}">ROI: ${roiPct >= 0 ? '+' : ''}${roiPct.toFixed(2)}% | ${lev.toFixed(2)}x</div>
                        </div>
                    </div>`;
            }).join('');
        } else {
            container.innerHTML = '<div class="text-center text-gray-500 text-xs py-3">暂无持仓</div>';
        }

        // 盈亏摘要
        if (data.summary) {
            const summaryPanel = document.getElementById(`pos-summary-${configId}`);
            if (summaryPanel) summaryPanel.classList.remove('hidden');

            const pnlEl = document.getElementById(`pos-pnl-${configId}`);
            if (pnlEl) {
                const pnl = data.summary.realized_pnl;
                pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${pnl} USDT`;
                pnlEl.className = `text-sm font-black ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`;
            }

            const wrEl = document.getElementById(`pos-winrate-${configId}`);
            if (wrEl) {
                const wr = data.summary.win_rate;
                wrEl.textContent = `${wr}%`;
                wrEl.className = `text-sm font-black ${wr >= 50 ? 'text-emerald-400' : 'text-amber-400'}`;
            }

            const trEl = document.getElementById(`pos-trades-${configId}`);
            if (trEl) trEl.textContent = data.summary.total_trades;
        }

        // 历史模拟仓位展示
        if (data.recent_trades && data.recent_trades.length > 0) {
            const recentContainer = document.getElementById(`recent-trades-container-${configId}`);
            const recentList = document.getElementById(`recent-trades-list-${configId}`);
            if (recentContainer && recentList) {
                recentContainer.classList.remove('hidden');
                recentList.innerHTML = data.recent_trades.map(t => {
                    const isWin = t.pnl >= 0;
                    const pnlColor = isWin ? 'text-emerald-500' : 'text-red-500';
                    const sideColor = t.side.toUpperCase().includes('BUY') ? 'bg-emerald-500' : 'bg-red-500';
                    // 仅显示HH:mm以便排版紧凑
                    const timeStr = (t.time || '').substring(11, 16);
                    return `
                    <div class="flex justify-between items-center bg-gray-50 rounded px-2 py-1.5 border border-gray-100">
                        <div class="flex items-center gap-2">
                            <span class="px-1 py-0.5 rounded text-[8px] font-black text-white ${sideColor}">${t.side}</span>
                            <div class="flex flex-col">
                                <span class="text-[9px] font-mono font-bold text-gray-700">${t.amount} @ ${t.entry_price || '0'} -> ${t.price}</span>
                                <span class="text-[8px] text-gray-400">${timeStr}</span>
                            </div>
                        </div>
                        <div class="text-[10px] font-black ${pnlColor}">
                            ${isWin ? '+' : ''}${parseFloat(t.pnl).toFixed(2)}
                        </div>
                    </div>`;
                }).join('');
            }
        }
    } catch (e) {
        container.innerHTML = `<div class="text-center text-red-400 text-xs py-2">❌ 网络错误</div>`;
        console.error('Load position stats error:', e);
    }
}
