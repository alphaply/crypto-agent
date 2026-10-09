import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';

import { isCompactPlainCode, prepareMarkdown, markdownRemarkPlugins } from '../src/lib/markdown.js';

function render(source, streaming = false) {
  return renderToStaticMarkup(React.createElement(ReactMarkdown, { remarkPlugins: markdownRemarkPlugins }, prepareMarkdown(source, streaming)));
}

for (const streaming of [false, true]) {
  test(`price ranges, entities and standard deletion render correctly (streaming=${streaming})`, () => {
    const html = render('&#x20;2500~2507 和 2519 受阻，跌破 2472 会回测 2464~2467。\n\n2500-2507；~~过时判断~~；`2500~2507`', streaming);
    assert.match(html, /2500~2507 和 2519/);
    assert.match(html, /2464~2467/);
    assert.match(html, /2500-2507/);
    assert.match(html, /<del>过时判断<\/del>/);
    assert.match(html, /<code>2500~2507<\/code>/);
    assert.doesNotMatch(html, /&#x20;|25002507|24642467/);
  });
}

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
