import { test } from 'node:test';
import assert from 'node:assert/strict';
import { receiptSummary, tradeReceiptRows, isTradeReceipt } from '../src/lib/toolReceipt.js';

test('accepted unfilled entry shows requested amount and success without internal trees', () => {
  const raw = { status: 'submitted', results: [{ action: 'open', symbol: 'ETH/USDT:USDT', status: 'submitted', result: {
    side: 'SHORT', requested_amount: .05, amount: .05, filled_amount: 0, price: 2460,
    query: { status: 'partial', errors: ['private internal diagnostics'] },
  } }] };
  assert.deepEqual(tradeReceiptRows(JSON.stringify(raw)), [{ action: 'open', symbol: 'ETH/USDT:USDT', side: 'SHORT', amount: .05,
    price: 2460, triggerPrice: undefined, result: 'success' }]);
});
test('batch status does not override each action and unknown is never a false failure', () => {
  const rows = tradeReceiptRows({ status: 'failed', results: [
    { action: 'open', status: 'submitted' }, { action: 'open', status: 'failed' },
    { action: 'close', status: 'not_executed' }, { action: 'edit', status: 'unknown' },
  ] });
  assert.deepEqual(rows.map(row => row.result), ['success','failed','failed',null]);
});
test('request context supplies actual declared fields without substituting fills', () => {
  const rows = tradeReceiptRows({ results: [{ index: 0, action: 'edit', status: 'completed', result: { filled_amount: 4 } }] },
    { request: { actions: [{ action: 'edit', remaining_amount: .3, price: 2400, symbol: 'ETH/USDT:USDT' }] } });
  assert.equal(rows[0].amount, .3);
  assert.equal(rows[0].price, 2400);
  assert.equal(rows[0].side, undefined);
});
test('all, conditional double price, generic query and old text', () => {
  const [row] = tradeReceiptRows({ action: 'close', status: 'submitted', quantity_mode: 'all', price: 2000, trigger_price: 2100 });
  assert.equal(row.amount,'all'); assert.equal(row.price,2000); assert.equal(row.triggerPrice,2100);
  assert.equal(isTradeReceipt('old readable receipt','execute_trade_actions'),true);
  assert.equal(tradeReceiptRows('old readable receipt'),null);
  assert.equal(isTradeReceipt({ results: [{ price: 10 }] },'get_market'),false);
  assert.equal(isTradeReceipt({ results: [{ action: 'open' }] },'get_market'),false);
});
test('legacy arrays and absent parameter metadata remain readable', () => {
  const rows = tradeReceiptRows([{ action: 'open', status: 'submitted', amount: .05, price: 2460 }, null], { request: null });
  assert.equal(rows[0].amount,.05);
  assert.equal(rows[0].result,'success');
  assert.equal(rows[1].result,null);
});

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
