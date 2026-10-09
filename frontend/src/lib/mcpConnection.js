export function mcpSuggestedOrigin(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    if (url.protocol !== 'https:' || url.username || url.password
      || host === 'localhost' || host.endsWith('.localhost')
      || host === '[::1]' || host === '[::]' || host === '0.0.0.0' || host.startsWith('127.')) return '';
    return url.origin;
  } catch {
    return '';
  }
}

export function mcpChatGptCallback(value) {
  const callback = String(value || '').trim();
  if (/[\s\\]/.test(callback)) return '';
  try {
    const url = new URL(callback);
    if (url.protocol !== 'https:' || !['chatgpt.com', 'chat.openai.com'].includes(url.hostname)
      || url.username || url.password || url.port || url.hash || url.pathname === '/') return '';
    return callback;
  } catch {
    return '';
  }
}

export function mcpConnectionDraftChanged(draft, saved) {
  const normalize = (value) => String(value || '').trim().replace(/\/+$/, '');
  return normalize(draft?.public_url) !== normalize(saved?.public_url)
    || Boolean(draft?.enabled) !== Boolean(saved?.enabled);
}
