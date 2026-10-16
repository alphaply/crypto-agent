import { test } from 'node:test';
import assert from 'node:assert/strict';
import { receiptSummary } from '../src/lib/toolReceipt.js';

test('submission does not become a fill and identifiers stay in disclosure', () => {
  const result = receiptSummary({ status: 'submitted', results: [{ symbol: 'BTC/USDT', result: { filled_amount: 0, order_id: 'hidden-id' } }] }, true);
  assert.equal(result.status, 'submitted');
  assert.ok(result.text.includes('已成交 0'));
  assert.ok(!result.text.includes('hidden-id'));
});
test('later failures remain visible in a multi-action summary', () => {
  assert.ok(receiptSummary({ status: 'failed', results: [{ status: 'completed' }, { status: 'failed', error: 'Insufficient cash' }] }).text.includes('Insufficient cash'));
});
test('legacy text is bounded in the summary but preserved in full', () => {
  const text = 'Long response '.repeat(100);
  const result = receiptSummary(text);
  assert.ok(result.text.length <= 180);
  assert.equal(result.value, text);
});
test('pending verification explains nested errors without exposing an object dump', () => {
  const result = receiptSummary({ status: 'pending', symbol: 'ETH/USDT', query: { errors: [{ message: 'Exchange response unavailable' }] } });
  assert.equal(result.status, 'pending');
  assert.ok(result.text.includes('Exchange response unavailable'));
  assert.ok(!result.text.includes('[object Object]'));
});
