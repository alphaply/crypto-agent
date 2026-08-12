import assert from 'node:assert/strict';
import test from 'node:test';

import { splitThinkingContent } from '../src/lib/thinking.js';

test('extracts completed think and thinking blocks from answer content', () => {
  const result = splitThinkingContent(
    '<think>first step</think>\n<thinking>second step</thinking>\nFinal answer',
  );

  assert.equal(result.content, 'Final answer');
  assert.equal(result.reasoning, 'first step\n\nsecond step');
  assert.equal(result.thinkingOpen, false);
});

test('treats an unclosed think block as streaming reasoning', () => {
  const result = splitThinkingContent('<think>still working');

  assert.equal(result.content, '');
  assert.equal(result.reasoning, 'still working');
  assert.equal(result.thinkingOpen, true);
});

test('prefers the provider reasoning stream while removing duplicate tags', () => {
  const result = splitThinkingContent('<think>duplicated</think>Answer', 'provider reasoning');

  assert.equal(result.content, 'Answer');
  assert.equal(result.reasoning, 'provider reasoning');
});

test('preserves staged task reasoning markdown', () => {
  const staged = '### 推理阶段 1 · 调用 market_tool\n\ncheck trend\n\n---\n\n### 推理阶段 2\n\nfinalize';
  const result = splitThinkingContent('Final answer', staged);

  assert.equal(result.content, 'Final answer');
  assert.equal(result.reasoning, staged);
  assert.equal(result.thinkingOpen, false);
});
