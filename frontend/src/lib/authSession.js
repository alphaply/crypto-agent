const TOKEN_KEY = 'crypto-agent-token';
const SESSION_EVENT = 'crypto-agent-session-change';

export function readAuthToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function writeAuthToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function subscribeAuthToken(listener) {
  const onStorage = (event) => {
    if (event.storageArea === localStorage && (event.key === TOKEN_KEY || event.key === null)) listener();
  };
  window.addEventListener('storage', onStorage);
  window.addEventListener(SESSION_EVENT, listener);
  return () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener(SESSION_EVENT, listener);
  };
}
