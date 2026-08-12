import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatReasoningElapsed,
  formatReasoningSize,
  parseReasoningTimestamp,
} from '../src/lib/reasoning.js';

test('formats a live reasoning timer in seconds and minutes', () => {
  assert.equal(formatReasoningElapsed(12_900, 'zh'), '12 秒');
  assert.equal(formatReasoningElapsed(68_000, 'zh'), '1 分 08 秒');
  assert.equal(formatReasoningElapsed(68_000, 'en'), '1m 08s');
});

test('prefers reasoning tokens and falls back to visible characters', () => {
  assert.equal(formatReasoningSize(3872, 14402, 'zh'), '3,872 tokens');
  assert.equal(formatReasoningSize(0, 14402, 'zh'), '14,402 字符');
  assert.equal(formatReasoningSize(0, 14402, 'en'), '14,402 characters');
});

test('parses dashboard timestamps without resetting the timer on polling', () => {
  assert.equal(parseReasoningTimestamp(1234), 1234);
  assert.equal(parseReasoningTimestamp('2026-08-13 12:00:00'), new Date('2026-08-13T12:00:00').getTime());
});
