import assert from 'node:assert/strict';
import test from 'node:test';

import { isCompactPlainCode, prepareMarkdown } from '../src/lib/markdown.js';

test('repairs incomplete inline markdown while the answer is streaming', () => {
  assert.equal(prepareMarkdown('风险为 **中', true), '风险为 **中**');
  assert.equal(prepareMarkdown('风险为 **中', false), '风险为 **中');
});

test('keeps completed markdown identical before and after the stream ends', () => {
  const completed = '- 现价：`930.62`\n\n**风险可控**';
  assert.equal(prepareMarkdown(completed, true), prepareMarkdown(completed, false));
});

test('normalizes line endings before rendering live and stored messages', () => {
  assert.equal(prepareMarkdown('a\r\nb\rc', false), 'a\nb\nc');
});

test('renders short unlabelled code blocks as compact values', () => {
  assert.equal(isCompactPlainCode('930.62\n', ''), true);
  assert.equal(isCompactPlainCode('npm run build\n', 'language-bash'), false);
  assert.equal(isCompactPlainCode('first\nsecond\n', ''), false);
});
