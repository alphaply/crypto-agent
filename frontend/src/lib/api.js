import axios from 'axios';
import { createSseParser } from './sse';

export const api = axios.create({
  baseURL: '/api',
});

function normalizeApiError(error) {
  const status = error.response?.status;
  const data = error.response?.data;
  let detail = data?.detail || data?.message || error.message || 'Request failed';

  if (Array.isArray(detail)) {
    detail = detail.map((item) => item.msg || JSON.stringify(item)).join('\n');
  } else if (typeof detail === 'object') {
    detail = JSON.stringify(detail);
  }

  const prefix = status ? `Request failed (${status})` : 'Request failed';
  const normalized = new Error(`${prefix}: ${detail}`);
  normalized.response = error.response;
  normalized.code = error.code;
  return normalized;
}

// 全局加载进度条：通过自定义事件与 GlobalLoader 通信
api.interceptors.request.use((config) => {
  if (!config.silent) window.dispatchEvent(new Event('global-loading-start'));
  return config;
});

api.interceptors.response.use(
  (response) => {
    if (!response.config?.silent) window.dispatchEvent(new Event('global-loading-end'));
    return response;
  },
  (error) => {
    if (!error.config?.silent) window.dispatchEvent(new Event('global-loading-end'));
    return Promise.reject(normalizeApiError(error));
  },
);

export function setApiToken(token) {
  if (token) {
    api.defaults.headers.common.Authorization = `Bearer ${token}`;
  } else {
    delete api.defaults.headers.common.Authorization;
  }
}

export async function fullExport(includeSecrets = true) {
  const response = await api.get('/config/full-export', {
    params: { include_secrets: includeSecrets },
    responseType: 'blob',
  });
  return response;
}

export async function fullImport(data, writeEnv = false) {
  const response = await api.post('/config/full-import', { data, write_env: writeEnv });
  return response.data;
}

export async function streamSse(url, token, onEvent, signal, body = null) {
  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    method: body ? 'POST' : 'GET',
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });

  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(text || 'Failed to open stream');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const consume = createSseParser(onEvent);
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      consume(decoder.decode(value, { stream: true }));
    }
    consume(decoder.decode(), true);
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
