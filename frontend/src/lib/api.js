import axios from 'axios';

export const api = axios.create({
  baseURL: '/api',
});

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
