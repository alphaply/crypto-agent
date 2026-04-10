let currentSessionId = null;
let sessions = [];
let configs = [];
let pendingApproval = null;
let currentMessages = [];
let multiSelectMode = false;
let selectedSessionIds = new Set();
let mobileSidebarOpen = false;
let approvalLoading = false;
let autoScrollEnabled = true;
let pendingStreamUpdate = false;
const streamBuffers = new Map();
let streamFlushTimer = null;
const AUTO_SCROLL_THRESHOLD = 56;

function scheduleUiTask(callback) {
  if (typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(callback);
    return;
  }
  window.setTimeout(callback, 16);
}

function renderMarkdown(content) {
  if (!window.marked) return String(content || '');
  try {
    return DOMPurify.sanitize(marked.parse(String(content || '')));
  } catch (error) {
    return String(content || '');
  }
}

function enhanceMarkdown(root = document) {
  if (!window.hljs) return;
  root.querySelectorAll('pre code').forEach(block => {
    if (block.dataset.highlighted === 'true') return;
    window.hljs.highlightElement(block);
    block.dataset.highlighted = 'true';
  });
}

function ensureMessagesScrollListener() {
  const container = document.getElementById('messages');
  if (!container || container.dataset.scrollBound === 'true') return container;

  container.dataset.scrollBound = 'true';
  container.addEventListener('scroll', () => {
    autoScrollEnabled = (container.scrollHeight - container.scrollTop - container.clientHeight) <= AUTO_SCROLL_THRESHOLD;
    const button = document.getElementById('jumpToBottomBtn');
    if (button) button.classList.toggle('hidden', autoScrollEnabled);
  });
  return container;
}

function scrollToBottomAndFollow() {
  const container = document.getElementById('messages');
  if (!container) return;
  autoScrollEnabled = true;
  container.scrollTop = container.scrollHeight;
}

function syncMobileSidebar() {
  const sidebar = document.getElementById('sessionSidebar');
  const overlay = document.getElementById('mobileOverlay');
  if (sidebar) sidebar.classList.toggle('-translate-x-full', !mobileSidebarOpen);
  if (overlay) overlay.classList.toggle('hidden', !mobileSidebarOpen);
  document.body.classList.toggle('overflow-hidden', mobileSidebarOpen && window.innerWidth < 1024);
}

function toggleMobileSidebar() {
  mobileSidebarOpen = !mobileSidebarOpen;
  syncMobileSidebar();
}

function closeMobileSidebar() {
  mobileSidebarOpen = false;
  syncMobileSidebar();
}

function setApprovalLoading(loading) {
  approvalLoading = loading;
  const yesBtn = document.getElementById('approvalYesBtn');
  const noBtn = document.getElementById('approvalNoBtn');
  if (yesBtn) yesBtn.disabled = loading;
  if (noBtn) noBtn.disabled = loading;
}

async function refreshCaptcha() {
  const image = document.getElementById('captcha-img');
  if (!image) return;
  try {
    const response = await fetch('/api/chat/captcha');
    const data = await response.json();
    if (data.success) image.src = data.image;
  } catch (error) {
    console.error('Refresh captcha failed', error);
  }
}

async function login() {
  const password = document.getElementById('password')?.value || '';
  const captchaContainer = document.getElementById('captcha-container');
  const captchaVisible = captchaContainer && !captchaContainer.classList.contains('hidden');
  const captcha = document.getElementById('captcha')?.value || '';
  const errorText = document.getElementById('authErr');

  if (!password.trim()) {
    if (errorText) errorText.textContent = '请输入密码';
    return;
  }
  if (captchaVisible && !captcha.trim()) {
    if (errorText) errorText.textContent = '请输入验证码';
    return;
  }

  try {
    const response = await fetch('/api/chat/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, captcha: captchaVisible ? captcha : '' })
    });
    const data = await response.json();
    if (!data.success) {
      if (data.need_captcha && captchaContainer) {
        captchaContainer.classList.remove('hidden');
        refreshCaptcha();
        const captchaInput = document.getElementById('captcha');
        if (captchaInput) captchaInput.value = '';
      }
      if (errorText) errorText.textContent = data.message || '验证失败';
      return;
    }
    window.location.reload();
  } catch (error) {
    window.alert('Network error');
  }
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

function setSessionIdInUrl(sessionId) {
  const url = new URL(window.location.href);
  if (sessionId) url.searchParams.set('sid', sessionId);
  else url.searchParams.delete('sid');
  window.history.replaceState({}, '', url.search);
}

function toggleMultiSelect() {
  multiSelectMode = !multiSelectMode;
  if (!multiSelectMode) selectedSessionIds.clear();
  renderSessions();
}

function toggleSelected(sessionId, checked) {
  if (checked) selectedSessionIds.add(sessionId);
  else selectedSessionIds.delete(sessionId);
  renderSessions();
}

function toggleSelectAllSessions() {
  if (selectedSessionIds.size === sessions.length) selectedSessionIds.clear();
  else sessions.forEach(session => selectedSessionIds.add(session.session_id));
  renderSessions();
}

function renderSessions() {
  const list = document.getElementById('sessionList');
  const multiBtn = document.getElementById('multiBtn');
  const selectAllBtn = document.getElementById('selectAllBtn');
  const deleteSelectedBtn = document.getElementById('deleteSelectedBtn');
  if (!list || !multiBtn) return;

  multiBtn.textContent = multiSelectMode ? '取消批量' : '批量管理';
  if (selectAllBtn) selectAllBtn.classList.toggle('hidden', !multiSelectMode);
  if (deleteSelectedBtn) deleteSelectedBtn.classList.toggle('hidden', !multiSelectMode || selectedSessionIds.size === 0);

  list.innerHTML = sessions.map(session => {
    const active = session.session_id === currentSessionId;
    return `
      <div class="group border border-transparent ${active ? 'bg-blue-50/80 shadow-sm rounded-2xl border-blue-100' : 'hover:bg-white/80 rounded-2xl transition-all'} m-1">
        <div class="p-3 md:p-3.5 flex items-start gap-2.5">
          ${multiSelectMode ? `<input type="checkbox" ${selectedSessionIds.has(session.session_id) ? 'checked' : ''} onchange="toggleSelected('${session.session_id}', this.checked)" class="mt-1 h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500" />` : ''}
          <button onclick="selectSession('${session.session_id}')" class="flex-1 text-left min-w-0">
            <div class="font-bold text-sm truncate text-slate-800">${escapeHtml(session.title || session.symbol)}</div>
            <div class="text-[10px] text-slate-400 mt-1 uppercase font-mono">${escapeHtml(session.symbol)} · ${escapeHtml(session.config_id)}</div>
          </button>
          ${!multiSelectMode ? `<button onclick="deleteOneSession('${session.session_id}')" class="opacity-100 lg:opacity-0 lg:group-hover:opacity-100 text-[10px] text-rose-400 hover:text-rose-600 p-1.5 transition-opacity">删除</button>` : ''}
        </div>
      </div>`;
  }).join('');
}

function normalizeOneMessage(message) {
  const normalized = { ...(message || {}) };
  normalized.content = String(normalized.content || '');
  normalized.reasoning_content = String(normalized.reasoning_content || '');

  const matched = normalized.content.match(/<thinking>([\s\S]*?)<\/thinking>/i);
  if (matched) {
    if (!normalized.reasoning_content) normalized.reasoning_content = matched[1].trim();
    normalized.content = normalized.content.replace(/<thinking>[\s\S]*?<\/thinking>\s*/ig, '').trimStart();
  }

  if (normalized.__thinking_open == null) {
    normalized.__thinking_open = !!(normalized.__streaming || normalized.__loading || normalized.reasoning_content);
  }
  return normalized;
}

function normalizeMessages(messages) {
  return (messages || []).map(normalizeOneMessage);
}

function rememberThinkingState(idx, wasOpen) {
  if (!currentMessages[idx]) return;
  currentMessages[idx].__thinking_open = !wasOpen;
}

function renderThinkingHtml(message, idx, isStreaming) {
  const reasoning = String(message.reasoning_content || '').trim();
  if (!reasoning) return '';

  const body = isStreaming
    ? `<div class="whitespace-pre-wrap text-[11px] text-slate-500 italic leading-relaxed">${escapeHtml(reasoning)}<span class="animate-pulse">...</span></div>`
    : `<div class="text-[11px] text-slate-500 italic leading-relaxed">${renderMarkdown(reasoning)}</div>`;

  return `
    <details data-thinking-wrap="1" class="mb-3 rounded-xl border border-slate-200 bg-slate-50/70 overflow-hidden" ${message.__thinking_open ? 'open' : ''}>
      <summary onclick="rememberThinkingState(${idx}, this.parentElement.open)" class="px-3 py-2 cursor-pointer list-none select-none text-[10px] font-bold uppercase tracking-widest text-slate-400 flex items-center justify-between">
        <span>思考过程</span>
        <svg class="w-3 h-3 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M19 9l-7 7-7-7" stroke-width="3"/></svg>
      </summary>
      <div data-thinking-body="1" class="px-3 pb-3 border-t border-slate-100/50">${body}</div>
    </details>`;
}

function renderToolCallsHtml(message) {
  if (!message.tool_calls || message.tool_calls.length === 0) return '';
  return message.tool_calls.map(toolCall => {
    let args = toolCall.args || {};
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args);
      } catch (error) {
        args = toolCall.args;
      }
    }

    const argsHtml = typeof args === 'object' && args !== null
      ? Object.entries(args).map(([key, value]) => `
          <div class="tool-arg-row">
            <span class="tool-arg-key">${escapeHtml(key)}:</span>
            <span class="tool-arg-val">${escapeHtml(typeof value === 'object' ? JSON.stringify(value) : value)}</span>
          </div>`).join('')
      : `<div class="text-slate-500 italic break-all">${escapeHtml(args)}</div>`;

    return `
      <details open class="mt-3 rounded-xl border border-blue-100 bg-blue-50/60 overflow-hidden group">
        <summary class="px-3 py-2 cursor-pointer list-none select-none text-[11px] font-medium text-blue-700 flex items-center justify-between hover:bg-blue-100/50 transition-colors">
          <div class="flex items-center gap-2">
            <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c-.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>
            <span class="font-bold tracking-wide">调用工具: ${escapeHtml(toolCall.name || '正在识别...')}</span>
          </div>
          <svg class="w-3.5 h-3.5 transition-transform group-open:rotate-180" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
        </summary>
        <div class="px-3 pb-3 pt-1 border-t border-blue-100/50 text-[10px] font-mono">
          ${argsHtml || '<span class="text-slate-400 italic">无参数</span>'}
        </div>
      </details>`;
  }).join('');
}

function renderToolMessage(message, idx, animationClass) {
  const toolContent = String(message.content || '').trim();
  return `
    <div id="msg-${idx}" class="flex items-start gap-2 mb-4 w-full">
      <details open class="flex-1 bg-slate-50 border border-slate-200 rounded-2xl overflow-hidden group max-w-[95%] sm:max-w-[85%] ${animationClass}">
        <summary class="px-3 py-2 cursor-pointer list-none select-none text-[11px] font-medium text-slate-600 flex items-center justify-between hover:bg-slate-100/50 transition-colors">
          <div class="flex items-center gap-2">
            <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            <span class="font-bold tracking-wide uppercase">工具返回结果</span>
          </div>
          <svg class="w-3.5 h-3.5 transition-transform group-open:rotate-180" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
        </summary>
        <div class="px-3 pb-3 pt-2 border-t border-slate-200/60 text-[10px] font-mono text-slate-600 whitespace-pre-wrap overflow-x-auto break-words max-h-64 overflow-y-auto scroll-thin">${escapeHtml(toolContent)}</div>
      </details>
    </div>`;
}

function buildMessageHtml(messages) {
  return messages.map((rawMessage, idx) => {
    const message = normalizeOneMessage(rawMessage);
    messages[idx] = message;

    const hasToolCalls = message.tool_calls && message.tool_calls.length > 0;
    const hasThinking = !!String(message.reasoning_content || '').trim();
    const isStreaming = message.__streaming || message.__loading;
    if (!message.content && !hasToolCalls && !hasThinking && !isStreaming) return '';

    const isUser = message.role === 'user';
    const isTool = message.role === 'tool';
    const animationClass = (isStreaming || idx < messages.length - 1) ? '' : 'animate-in slide-in-from-bottom-2 duration-300';
    if (isTool) return renderToolMessage(message, idx, animationClass);

    const bubbleClass = isUser
      ? 'ml-auto bg-blue-600 text-white rounded-br-none shadow-md'
      : 'bg-white/96 border border-slate-200 text-slate-800 rounded-bl-none shadow-sm';
    const bodyHtml = `<div data-msg-body="1" class="md-content">${message.content ? renderMarkdown(message.content) : (isStreaming ? '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>' : '')}</div>`;

    return `
      <div id="msg-${idx}" class="flex flex-col gap-1 mb-4 ${isUser ? 'items-end' : 'items-start'}">
        <div class="chat-bubble p-4 rounded-3xl max-w-[96%] sm:max-w-[88%] lg:max-w-[82%] ${bubbleClass} ${animationClass}">
          ${renderThinkingHtml(message, idx, isStreaming)}
          ${bodyHtml}
          <div data-tool-calls-wrap="1">${renderToolCallsHtml(message)}</div>
        </div>
      </div>`;
  }).join('');
}

function renderMessages(messages) {
  const container = ensureMessagesScrollListener();
  if (!container) return;
  container.innerHTML = buildMessageHtml(messages);
  enhanceMarkdown(container);
  if (autoScrollEnabled) scrollToBottomAndFollow();
}

function ensureThinkingDom(messageRoot, idx) {
  let wrap = messageRoot.querySelector('[data-thinking-wrap="1"]');
  if (wrap) return wrap;

  const bubble = messageRoot.querySelector('.chat-bubble');
  if (!bubble) return null;
  wrap = document.createElement('details');
  wrap.setAttribute('data-thinking-wrap', '1');
  wrap.className = 'mb-3 rounded-xl border border-slate-200 bg-slate-50/70 overflow-hidden';
  wrap.innerHTML = `
    <summary class="px-3 py-2 cursor-pointer list-none select-none text-[10px] font-bold uppercase tracking-widest text-slate-400 flex items-center justify-between">
      <span>思考过程</span>
      <svg class="w-3 h-3 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M19 9l-7 7-7-7" stroke-width="3"/></svg>
    </summary>
    <div data-thinking-body="1" class="px-3 pb-3 border-t border-slate-100/50"></div>`;
  wrap.querySelector('summary').onclick = () => rememberThinkingState(idx, wrap.open);
  bubble.insertBefore(wrap, bubble.firstChild);
  return wrap;
}

function flushStreamingUpdate() {
  pendingStreamUpdate = false;
  const streamingIndex = currentMessages.findLastIndex(message => message && message.__streaming);
  if (streamingIndex === -1) return;

  const message = currentMessages[streamingIndex];
  const messageRoot = document.getElementById(`msg-${streamingIndex}`);
  if (!messageRoot) {
    renderMessages(currentMessages);
    return;
  }

  const body = messageRoot.querySelector('[data-msg-body="1"]');
  if (body) {
    body.innerHTML = message.content ? renderMarkdown(message.content) : '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';
    enhanceMarkdown(body);
  }

  const toolWrap = messageRoot.querySelector('[data-tool-calls-wrap="1"]');
  if (toolWrap) toolWrap.innerHTML = renderToolCallsHtml(message);

  const reasoning = String(message.reasoning_content || '').trim();
  if (reasoning) {
    const wrap = ensureThinkingDom(messageRoot, streamingIndex);
    if (wrap) {
      wrap.open = message.__thinking_open !== false;
      const thinkingBody = wrap.querySelector('[data-thinking-body="1"]');
      if (thinkingBody) thinkingBody.innerHTML = `<div class="whitespace-pre-wrap text-[11px] text-slate-500 italic leading-relaxed">${escapeHtml(reasoning)}${message.__streaming ? '<span class="animate-pulse">...</span>' : ''}</div>`;
    }
  }

  if (autoScrollEnabled) scrollToBottomAndFollow();
}

function queueStreamingUpdate() {
  if (pendingStreamUpdate) return;
  pendingStreamUpdate = true;
  scheduleUiTask(flushStreamingUpdate);
}

function applyBufferedDelta(streamIdx, delta) {
  const message = currentMessages[streamIdx];
  if (!message) return;

  if (delta.reasoning) {
    message.reasoning_content = (message.reasoning_content || '') + delta.reasoning;
    if (message.__thinking_open == null) message.__thinking_open = true;
  }
  if (delta.content) {
    message.content = (message.content || '') + delta.content;
  }
  if (delta.tool_calls) {
    if (!message.tool_calls) message.tool_calls = [];
    delta.tool_calls.forEach(toolCall => {
      let existing = null;
      if (toolCall.id) {
        existing = message.tool_calls.find(item => item.id === toolCall.id);
      }
      if (!existing && toolCall.index != null) {
        for (let index = message.tool_calls.length - 1; index >= 0; index -= 1) {
          if (message.tool_calls[index].index === toolCall.index) {
            existing = message.tool_calls[index];
            break;
          }
        }
      }
      if (existing) {
        if (toolCall.name) existing.name = (existing.name || '') + toolCall.name;
        if (toolCall.args) existing.args = (existing.args || '') + toolCall.args;
        if (toolCall.id) existing.id = toolCall.id;
        if (toolCall.type) existing.type = toolCall.type;
      } else {
        message.tool_calls.push(toolCall);
      }
    });
  }
  message.__streaming = true;
}

function flushAllStreamBuffers() {
  if (streamFlushTimer) {
    window.clearInterval(streamFlushTimer);
    streamFlushTimer = null;
  }
  if (!streamBuffers.size) return;

  streamBuffers.forEach((buffer, streamIdx) => {
    applyBufferedDelta(streamIdx, buffer);
  });
  streamBuffers.clear();
  queueStreamingUpdate();
}

function ensureStreamFlushTimer() {
  if (streamFlushTimer) return;
  streamFlushTimer = window.setInterval(() => {
    if (!streamBuffers.size) {
      window.clearInterval(streamFlushTimer);
      streamFlushTimer = null;
      return;
    }

    streamBuffers.forEach((buffer, streamIdx) => {
      applyBufferedDelta(streamIdx, buffer);
    });
    streamBuffers.clear();
    queueStreamingUpdate();
  }, 36);
}

function appendStreamDelta(streamIdx, delta) {
  const message = currentMessages[streamIdx];
  if (!message) return;

  const prev = streamBuffers.get(streamIdx) || { content: '', reasoning: '', tool_calls: [] };
  if (delta.content) prev.content += delta.content;
  if (delta.reasoning) prev.reasoning += delta.reasoning;
  if (delta.tool_calls && delta.tool_calls.length) prev.tool_calls.push(...delta.tool_calls);
  streamBuffers.set(streamIdx, prev);
  ensureStreamFlushTimer();
}

function showApproval() {
  const bar = document.getElementById('approvalBar');
  if (!bar) return;
  if (!pendingApproval || !pendingApproval.value) {
    bar.classList.add('hidden');
    return;
  }

  const text = document.getElementById('approvalText');
  const value = pendingApproval.value;
  if (text) {
    text.innerHTML = `<span class="bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded font-mono font-bold mr-2 uppercase text-[10px]">${value.tool_name}</span> <span class="opacity-70">${JSON.stringify(value.tool_args)}</span>`;
  }
  bar.classList.remove('hidden');
}

async function bootstrap() {
  try {
    const response = await fetch('/api/chat/bootstrap');
    if (response.status === 401) {
      const authView = document.getElementById('authView');
      const chatView = document.getElementById('chatView');
      if (authView) authView.style.display = 'flex';
      if (chatView) chatView.style.display = 'none';
      return;
    }

    const data = await response.json();
    if (!data.success) return;
    sessions = data.sessions || [];
    configs = data.configs || [];
    renderSessions();

    const sidFromUrl = new URLSearchParams(window.location.search).get('sid');
    const sessionExists = sessions.some(session => session.session_id === sidFromUrl);
    if (sidFromUrl && sessionExists) {
      await selectSession(sidFromUrl, { skipUrlSync: true });
      return;
    }

    if (sessions.length > 0) {
      if (sidFromUrl) setSessionIdInUrl(null);
      await selectSession(sessions[0].session_id);
      return;
    }

    setSessionIdInUrl(null);
    openNewSession();
    const container = document.getElementById('messages');
    if (container) {
      container.innerHTML = `
        <div class="h-full flex flex-col items-center justify-center text-slate-400 p-8 text-center">
          <div class="w-16 h-16 bg-slate-100 rounded-full flex items-center justify-center mb-4 text-2xl">💬</div>
          <h3 class="text-slate-600 font-bold mb-2">欢迎使用 Crypto Agent 聊天</h3>
          <p class="text-xs max-w-xs leading-relaxed">请在左侧选择一个现有会话，或者点击“立即创建”开始一段新的对话。</p>
        </div>`;
    }
  } catch (error) {
    console.error('Bootstrap failed', error);
  }
}

async function selectSession(sessionId, options = {}) {
  if (multiSelectMode) return;
  currentSessionId = sessionId;
  const container = document.getElementById('messages');
  if (container) {
    container.innerHTML = '<div class="h-full flex flex-col items-center justify-center text-slate-400 text-xs animate-pulse space-y-2"><span>加载消息中...</span></div>';
  }

  if (!options.skipUrlSync) {
    setSessionIdInUrl(sessionId);
  }
  closeMobileSidebar();
  pendingApproval = null;
  showApproval();
  renderSessions();

  try {
    const response = await fetch(`/api/chat/sessions/${sessionId}/messages`);
    const data = await response.json();
    if (!data.success) {
      if (!options.isFallback) {
        setSessionIdInUrl(null);
        currentSessionId = null;
        bootstrap();
      }
      return;
    }

    const header = document.getElementById('chatHeader');
    if (header && data.session) header.textContent = data.session.title;
    currentMessages = normalizeMessages(data.messages || []);
    renderMessages(currentMessages);
  } catch (error) {
    if (container) container.innerHTML = '<div class="text-rose-500 p-4 text-center">加载失败</div>';
  }
}

async function clearCurrentChat() {
  if (!currentSessionId) {
    window.alert('请先选择一个会话');
    return;
  }
  if (!window.confirm('确定要清空当前对话的历史记录吗？此操作不可恢复！')) return;

  try {
    const response = await fetch(`/api/chat/sessions/${currentSessionId}/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const result = await response.json();
    if (!result.success) {
      window.alert(`清空失败: ${result.message || '未知错误'}`);
      return;
    }

    currentMessages = [];
    const container = document.getElementById('messages');
    if (container) container.innerHTML = '<div class="text-slate-400 text-sm text-center py-4">对话历史已清空，可以开始新的对话</div>';
  } catch (error) {
    console.error('清空聊天记录时出错:', error);
    window.alert('清空失败，请稍后再试');
  }
}

async function sendMessage() {
  const input = document.getElementById('input');
  const message = input?.value.trim();
  if (!input || !message || !currentSessionId || pendingApproval || approvalLoading) return;

  input.value = '';
  input.style.height = '40px';
  const isFirstMessage = currentMessages.length === 0;
  currentMessages.push({ role: 'user', content: message });
  const streamIdx = currentMessages.length;
  currentMessages.push({ role: 'assistant', content: '', reasoning_content: '', __streaming: true, __thinking_open: true });
  renderMessages(currentMessages);

  const eventSource = new EventSource(`/api/chat/sessions/${currentSessionId}/stream?message=${encodeURIComponent(message)}`);
  eventSource.onmessage = event => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'token') {
      appendStreamDelta(streamIdx, { content: payload.token || '' });
    } else if (payload.type === 'reasoning_token') {
      appendStreamDelta(streamIdx, { reasoning: payload.token || '' });
    } else if (payload.type === 'tool_calls') {
      appendStreamDelta(streamIdx, { tool_calls: payload.tool_calls });
    } else if (payload.type === 'done') {
      flushAllStreamBuffers();
      currentMessages = normalizeMessages(payload.messages || []);
      pendingApproval = payload.pending_approval;
      renderMessages(currentMessages);
      showApproval();
      eventSource.close();
      if (isFirstMessage) {
        fetch(`/api/chat/sessions/${currentSessionId}/summarize_title`, { method: 'POST' })
          .then(response => response.json())
          .then(data => {
            if (data.success) bootstrap();
          });
      }
    } else if (payload.type === 'error') {
      window.alert(`Streaming error: ${payload.message}`);
      eventSource.close();
    }
  };
  eventSource.onerror = () => eventSource.close();
}

async function respondApproval(approved) {
  const bar = document.getElementById('approvalBar');
  if (bar) bar.classList.add('hidden');
  setApprovalLoading(true);

  const streamIdx = currentMessages.length;
  currentMessages.push({
    role: 'assistant',
    content: '正在执行工具...',
    reasoning_content: '',
    __loading: true,
    __streaming: true,
    __thinking_open: true
  });
  renderMessages(currentMessages);

  const eventSource = new EventSource(`/api/chat/sessions/${currentSessionId}/stream?approval=${approved}`);
  eventSource.onmessage = event => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'tool_result') {
      currentMessages[streamIdx] = {
        role: 'tool',
        content: payload.content,
        tool_call_id: payload.tool_call_id,
        __thinking_open: false
      };
      if (currentMessages.length === streamIdx + 1) {
        currentMessages.push({
          role: 'assistant',
          content: '',
          reasoning_content: '',
          __loading: true,
          __streaming: true,
          __thinking_open: true
        });
      }
      renderMessages(currentMessages);
    } else if (payload.type === 'token' || payload.type === 'reasoning_token' || payload.type === 'tool_calls') {
      let lastAiIdx = -1;
      for (let index = currentMessages.length - 1; index >= 0; index -= 1) {
        if (currentMessages[index].role === 'assistant') {
          lastAiIdx = index;
          break;
        }
      }
      if (lastAiIdx !== -1) {
        const message = currentMessages[lastAiIdx];
        if (message.__loading) {
          message.content = '';
          delete message.__loading;
        }
        if (payload.type === 'token') appendStreamDelta(lastAiIdx, { content: payload.token || '' });
        if (payload.type === 'reasoning_token') appendStreamDelta(lastAiIdx, { reasoning: payload.token || '' });
        if (payload.type === 'tool_calls') appendStreamDelta(lastAiIdx, { tool_calls: payload.tool_calls });
      }
    } else if (payload.type === 'done') {
      flushAllStreamBuffers();
      currentMessages = normalizeMessages(payload.messages || []);
      pendingApproval = payload.pending_approval;
      renderMessages(currentMessages);
      showApproval();
      setApprovalLoading(false);
      eventSource.close();
    } else if (payload.type === 'error') {
      console.error('Stream error:', payload.message);
      setApprovalLoading(false);
      eventSource.close();
    }
  };
  eventSource.onerror = error => {
    console.error('EventSource failed:', error);
    setApprovalLoading(false);
    eventSource.close();
  };
}

async function deleteOneSession(sessionId) {
  if (!window.confirm('确定删除此会话?')) return;
  try {
    const response = await fetch(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' });
    const data = await response.json();
    if (!data.success) return;
    sessions = sessions.filter(session => session.session_id !== sessionId);
    renderSessions();
    if (currentSessionId === sessionId) {
      currentSessionId = null;
      currentMessages = [];
      const messages = document.getElementById('messages');
      if (messages) messages.innerHTML = '';
      const header = document.getElementById('chatHeader');
      if (header) header.textContent = '请选择一个会话';
      setSessionIdInUrl(null);
    }
  } catch (error) {
    window.alert('删除失败');
  }
}

async function deleteSelectedSessions() {
  if (!selectedSessionIds.size || !window.confirm(`确定删除选中的 ${selectedSessionIds.size} 个会话?`)) return;
  try {
    const response = await fetch('/api/chat/sessions', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selectedSessionIds) })
    });
    const data = await response.json();
    if (data.success) window.location.reload();
  } catch (error) {
    window.alert('批量删除失败');
  }
}

function openNewSession(event) {
  const modal = document.getElementById('newSessionModal');
  if (!modal) return;

  if (configs.length === 0) {
    const button = event ? event.currentTarget : null;
    if (button) button.disabled = true;
    bootstrap().then(() => {
      if (button) button.disabled = false;
      if (configs.length > 0) {
        openNewSession();
      } else {
        window.alert('未发现可用的交易对配置，请在控制台检查 SYMBOL_CONFIGS 是否配置正确。');
      }
    });
    return;
  }

  const select = document.getElementById('configSelect');
  if (select) {
    select.innerHTML = configs.map(config => {
      const displayName = `[${config.config_id}] ${config.symbol} (${config.model})`;
      return `<option value="${config.config_id}">${escapeHtml(displayName)}</option>`;
    }).join('');
  }
  modal.classList.remove('hidden');
}

function closeNewSession() {
  const modal = document.getElementById('newSessionModal');
  if (modal) modal.classList.add('hidden');
}

async function createSession() {
  const select = document.getElementById('configSelect');
  const title = document.getElementById('sessionTitle');
  if (!select) return;

  try {
    const response = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config_id: select.value,
        title: title ? title.value : ''
      })
    });
    const data = await response.json();
    if (!data.success) return;
    closeNewSession();
    await bootstrap();
    await selectSession(data.session_id);
  } catch (error) {
    window.alert('创建失败');
  }
}

function bindInputEvents() {
  const input = document.getElementById('input');
  if (!input) return;
  input.addEventListener('input', () => {
    scheduleUiTask(() => {
      input.style.height = 'auto';
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    });
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
}

function initChatApp() {
  bindInputEvents();
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 1024) closeMobileSidebar();
  });
  refreshCaptcha();
  bootstrap();
}

window.toggleMobileSidebar = toggleMobileSidebar;
window.closeMobileSidebar = closeMobileSidebar;
window.refreshCaptcha = refreshCaptcha;
window.login = login;
window.toggleMultiSelect = toggleMultiSelect;
window.toggleSelected = toggleSelected;
window.toggleSelectAllSessions = toggleSelectAllSessions;
window.selectSession = selectSession;
window.clearCurrentChat = clearCurrentChat;
window.scrollToBottomAndFollow = scrollToBottomAndFollow;
window.respondApproval = respondApproval;
window.sendMessage = sendMessage;
window.deleteOneSession = deleteOneSession;
window.deleteSelectedSessions = deleteSelectedSessions;
window.openNewSession = openNewSession;
window.closeNewSession = closeNewSession;
window.createSession = createSession;
window.rememberThinkingState = rememberThinkingState;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initChatApp);
} else {
  initChatApp();
}
