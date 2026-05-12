import axios from 'axios';

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
  return new Error(detail && detail !== error.message ? `${prefix}: ${detail}` : `${prefix}: ${detail}`);
}

// 全局加载进度条：通过自定义事件与 GlobalLoader 通信
api.interceptors.request.use((config) => {
  window.dispatchEvent(new Event('global-loading-start'));
  return config;
});

api.interceptors.response.use(
  (response) => {
    window.dispatchEvent(new Event('global-loading-end'));
    return response;
  },
  (error) => {
    window.dispatchEvent(new Event('global-loading-end'));
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

export async function streamSse(url, token, onEvent) {
  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
  });

  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(text || 'Failed to open stream');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      const lines = part.split('\n').filter((line) => line.startsWith('data:'));
      for (const line of lines) {
        const payload = line.slice(5).trim();
        if (!payload) {
          continue;
        }
        onEvent(JSON.parse(payload));
      }
    }
  }
}
