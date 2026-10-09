import test from 'node:test';
import assert from 'node:assert/strict';
import { mcpChatGptCallback, mcpConnectionDraftChanged, mcpSuggestedOrigin } from '../src/lib/mcpConnection.js';

test('current-origin shortcut uses HTTPS origin without copying the admin page path', () => {
  assert.equal(mcpSuggestedOrigin('https://crypto-agent.example.com/admin?tab=mcp'), 'https://crypto-agent.example.com');
  assert.equal(mcpSuggestedOrigin('https://crypto-agent.example.com:8443/admin'), 'https://crypto-agent.example.com:8443');
  for (const value of ['http://crypto-agent.example.com', 'http://localhost:5173', 'https://localhost', 'https://dev.localhost', 'https://127.0.0.1', 'https://[::1]', 'https://user:secret@example.com', 'invalid']) {
    assert.equal(mcpSuggestedOrigin(value), '', value);
  }
});

test('ChatGPT callbacks preserve the exact path and query while rejecting lookalike or insecure origins', () => {
  const callback = 'https://chatgpt.com/connector/oauth/Iw1Pj37mk7nw';
  assert.equal(mcpChatGptCallback(` ${callback} `), callback);
  assert.equal(mcpChatGptCallback('https://chat.openai.com/aip/callback?connection=123'), 'https://chat.openai.com/aip/callback?connection=123');
  for (const value of ['http://chatgpt.com/connector/oauth/test', 'https://chatgpt.com.evil.test/connector/oauth/test', 'https://chatgpt.com@evil.test/connector/oauth/test', 'https://user@chatgpt.com/connector/oauth/test', 'https://chatgpt.com:8443/connector/oauth/test', 'https://chatgpt.com/connector/oauth/a b', 'https://chatgpt.com/connector/oauth/a\nb', 'https:\\chatgpt.com\\connector/oauth/test', `${callback}#fragment`, 'https://chatgpt.com/', 'javascript:alert(1)', '']) {
    assert.equal(mcpChatGptCallback(value), '', value);
  }
});

test('connection checks use saved settings, allowing only harmless trailing-slash normalization', () => {
  const saved = { enabled: true, public_url: 'https://agent.example.com' };
  assert.equal(mcpConnectionDraftChanged({ ...saved }, saved), false);
  assert.equal(mcpConnectionDraftChanged({ ...saved, public_url: `${saved.public_url}/` }, saved), false);
  assert.equal(mcpConnectionDraftChanged({ ...saved, public_url: 'https://other.example.com' }, saved), true);
  assert.equal(mcpConnectionDraftChanged({ ...saved, enabled: false }, saved), true);
});
