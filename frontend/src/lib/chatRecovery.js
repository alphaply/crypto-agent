const LAST_SESSION_KEY = 'crypto-agent-chat-last-session';
const DRAFT_PREFIX = 'crypto-agent-chat-draft:';

export const browserChatStorage = {
  getItem: (key) => window.localStorage.getItem(key),
  setItem: (key, value) => window.localStorage.setItem(key, value),
  removeItem: (key) => window.localStorage.removeItem(key),
};

// Storage can be unavailable in private browsing or when the browser is full.
// A failed write must never prevent typing or sending a message.
export function readLastSession(storage) {
  try { return storage.getItem(LAST_SESSION_KEY) || ''; } catch { return ''; }
}

export function rememberSession(storage, sessionId) {
  try {
    if (sessionId) storage.setItem(LAST_SESSION_KEY, sessionId);
    else storage.removeItem(LAST_SESSION_KEY);
    return true;
  } catch { return false; }
}

export function readChatDraft(storage, sessionId) {
  try { return storage.getItem(DRAFT_PREFIX + (sessionId || 'new')) || ''; } catch { return ''; }
}

export function writeChatDraft(storage, sessionId, content) {
  try {
    const key = DRAFT_PREFIX + (sessionId || 'new');
    if (content) storage.setItem(key, content);
    else storage.removeItem(key);
    return true;
  } catch { return false; }
}
