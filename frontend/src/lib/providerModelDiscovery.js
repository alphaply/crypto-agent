// Connection identity deliberately preserves path case and explicit ports.
export function discoveryConnectionKey(provider = {}) {
  const raw = String(provider.api_base || '').trim().replace(/\/+$/, '');
  const match = /^(https?):\/\/([^/?#]+)([^?#]*)$/i.exec(raw);
  if (!match || /[\s\\]/.test(raw) || [...raw].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127) || match[2].includes('@')) return '';
  try { new URL(raw); } catch { return ''; }
  return JSON.stringify([
    `${match[1].toLowerCase()}://${match[2].toLowerCase()}${match[3]}`,
    provider.api_protocol || 'chat', provider.compatibility_mode || 'auto',
    provider.api_protocol === 'decisions' ? provider.decisions_api || 'bai' : null,
  ]);
}

export function discoveryConnection(provider, saved, keyState = {}) {
  const connectionKey = discoveryConnectionKey(provider);
  const secret = provider.secrets?.api_key || {};
  let credential = { mode: 'none' };
  if (!secret.clear) {
    if (keyState.edited) {
      if (secret.value?.trim()) credential = { mode: 'draft', api_key: secret.value };
    } else if (saved?.provider_id === provider.provider_id && connectionKey &&
        connectionKey === discoveryConnectionKey(saved) &&
        (saved.secrets?.api_key?.configured || saved.secrets?.api_key?.value)) {
      credential = { mode: 'saved', provider_id: provider.provider_id };
    }
  }
  return {
    // No secret in the identity, cache keys, logs or browser storage.
    identity: JSON.stringify([connectionKey, provider.provider_id, credential.mode, keyState.version || 0]),
    ready: Boolean(connectionKey && credential.mode !== 'none'),
    incomplete: !connectionKey ? 'invalid_connection' : credential.mode === 'none' ?
      (secret.value && !secret.clear && !keyState.edited ? 'connection_changed' : 'missing_credentials') : null,
    body: {
      api_base: String(provider.api_base || '').trim().replace(/\/+$/, ''),
      api_protocol: provider.api_protocol || 'chat', compatibility_mode: provider.compatibility_mode || 'auto',
      decisions_api: provider.decisions_api || 'bai', credential,
    },
  };
}

export function normalizeDiscoveredModels(rows) {
  const unique = new Map();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (typeof row?.id !== 'string' || !row.id.trim() || unique.has(row.id)) continue;
    unique.set(row.id, { id: row.id, name: typeof row.name === 'string' ? row.name : row.id,
      description: typeof row.description === 'string' ? row.description : '' });
  }
  return [...unique.values()].sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

export function modelMatches(search, model) {
  const needle = search.toLocaleLowerCase();
  return `${model.id} ${model.name}`.toLocaleLowerCase().includes(needle);
}

const messages = {
  invalid_connection: ['请填写有效的 HTTP(S) API Base，完成后自动获取。', 'Enter a valid HTTP(S) API Base to fetch models.'],
  missing_credentials: ['请为当前连接填写 API Key，离开输入框后自动获取。', 'Enter an API key for this connection; models load after leaving the field.'],
  connection_changed: ['连接已变更，请为当前连接重新填写 API Key。', 'Connection changed. Enter a key for the current connection.'],
  provider_not_found: ['已保存的渠道不存在，请重新填写连接信息。', 'The saved provider no longer exists. Enter the connection again.'],
  credentials_rejected: ['供应商拒绝了 Key 或模型列表权限，可继续手动填写。', 'The supplier rejected the key or list permission. You can enter a model manually.'],
  catalog_not_supported: ['供应商不支持模型列表，请手动填写模型 ID。', 'The supplier does not support model lists. Enter a model ID manually.'],
  rate_limited: ['供应商暂时限流，请稍后刷新或手动填写。', 'The supplier rate limited this request. Refresh later or enter a model manually.'],
  timeout: ['获取超时，请刷新重试或手动填写。', 'Discovery timed out. Refresh or enter a model manually.'],
  invalid_response: ['供应商返回的模型列表无效，可继续手动填写。', 'The supplier returned an invalid model list. You can enter a model manually.'],
  catalog_limit: ['已达到读取上限，仅展示部分模型。', 'Catalog limit reached. Showing a partial list.'],
  pagination_error: ['供应商分页异常，仅展示已读取的模型。', 'Invalid supplier pagination. Showing models already read.'],
  pagination_unsupported: ['供应商分页方式暂不支持，仅展示已读取的模型。', 'Unsupported pagination. Showing models already read.'],
  response_too_large: ['供应商列表超过大小限制，可继续手动填写。', 'The supplier response exceeded the size limit. You can enter a model manually.'],
};

export function discoveryIssue(error, locale = 'zh') {
  const code = error?.response?.data?.detail?.code || error?.code;
  const text = messages[code] || ['暂时无法获取模型，请刷新重试或手动填写。', 'Could not fetch models. Refresh or enter a model manually.'];
  return text[locale === 'zh' ? 0 : 1];
}

const emptyState = () => ({ models: [], loading: false, loaded: false, ready: false, incomplete: null, error: null,
  complete: true, warning: null, fetchedAt: '', source: null, stale: false });

/** Drawer-local request lifecycle; injected clocks/promises make races testable. */
export function createModelDiscovery({ fetchModels, delay = 300, schedule = setTimeout, cancel = clearTimeout }) {
  let state = emptyState();
  let active = false;
  let connection = null;
  let version = 0;
  let attempted = false;
  let timer = null;
  let controller = null;
  const listeners = new Set();
  const publish = (patch) => { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); };
  const invalidate = () => {
    version += 1;
    if (timer !== null) cancel(timer);
    timer = null;
    controller?.abort();
    controller = null;
  };
  const run = async () => {
    if (!active || !connection?.ready) return;
    invalidate();
    const current = version;
    controller = new AbortController();
    const signal = controller.signal;
    attempted = true;
    publish({ loading: true, error: null });
    try {
      const result = await fetchModels(connection.body, signal);
      if (!active || current !== version || signal.aborted) return;
      if (!result?.success || !Array.isArray(result.models) || typeof result.complete !== 'boolean') {
        throw Object.assign(new Error('Invalid catalog'), { code: 'invalid_response' });
      }
      publish({ models: normalizeDiscoveredModels(result.models), loaded: true, complete: result.complete,
        warning: result.warning, fetchedAt: result.fetched_at, source: result.source, stale: false });
    } catch (error) {
      if (active && current === version && !signal.aborted) {
        // Never retain raw upstream/axios errors (they may contain request credentials).
        publish({ error: { code: error?.response?.data?.detail?.code || error?.code || 'connection_error' }, stale: state.loaded });
      }
    } finally {
      if (active && current === version && !signal.aborted) publish({ loading: false });
    }
  };
  const commit = () => {
    if (!active || !connection?.ready || attempted || timer !== null) return;
    timer = schedule(() => { timer = null; void run(); }, delay);
  };
  const update = (next, committed = false) => {
    if (!active) return;
    if (connection?.identity !== next.identity) {
      invalidate();
      attempted = false;
      connection = next;
      publish({ ...emptyState(), ready: next.ready, incomplete: next.incomplete });
    }
    if (committed) commit();
  };
  const close = () => {
    invalidate();
    active = false;
    connection = null;
    attempted = false;
    publish(emptyState());
  };
  return {
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    getSnapshot: () => state,
    open(next) { close(); active = true; update(next, true); },
    update, commit, refresh: run, close,
  };
}
