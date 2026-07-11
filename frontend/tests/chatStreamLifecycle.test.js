import assert from 'node:assert/strict';
import test from 'node:test';

import { createChatStreamLifecycle, reduceChatStreamLifecycle } from '../src/lib/chatStreamLifecycle.js';

test('done completes the stream even when persistence failed', () => {
  const state = reduceChatStreamLifecycle(createChatStreamLifecycle(), { type: 'done', persisted: false });
  assert.deepEqual(state, { completed: true, handledTerminalEvent: true, persisted: false });
});

test('structured errors prevent a second synthetic connection error', () => {
  const state = reduceChatStreamLifecycle(createChatStreamLifecycle(), { type: 'error', phase: 'generation' });
  assert.equal(state.completed, false);
  assert.equal(state.handledTerminalEvent, true);
});
