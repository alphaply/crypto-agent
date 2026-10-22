import { test } from 'node:test';
import assert from 'node:assert/strict';
import { receiptSummary, tradeReceiptRows, isTradeReceipt, toolRequest } from '../src/lib/toolReceipt.js';

test('accepted unfilled entry shows requested amount and success without internal trees', () => {
  const raw = { status: 'submitted', results: [{ action: 'open', symbol: 'ETH/USDT:USDT', status: 'submitted', result: {
    side: 'SHORT', requested_amount: .05, amount: .05, filled_amount: 0, price: 2460,
    query: { status: 'partial', errors: ['private internal diagnostics'] },
  } }] };
  const [{ action, symbol, side, amount, price, triggerPrice, result }] = tradeReceiptRows(JSON.stringify(raw));
  assert.deepEqual([{ action, symbol, side, amount, price, triggerPrice, result }], [{ action: 'open', symbol: 'ETH/USDT:USDT', side: 'SHORT', amount: .05,
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


test('spot entry price survives legacy receipts and JSON request arguments', () => {
  const [row] = tradeReceiptRows({ status: 'submitted', results: [{ entry_price: 0.00001234, amount: 12, symbol: 'PEPE/USDT' }] },
    { toolName: 'open_position_spot_dca', request: JSON.stringify({ orders: [{ action: 'BUY_LIMIT', entry_price: .00001234, amount: 12 }] }) });
  assert.equal(row.price, .00001234);
  assert.equal(row.priceFromRequest, false);
  assert.equal(row.action, 'open');
  assert.equal(row.side, 'BUY');
});

test('amendment keeps observed and requested prices separate', () => {
  const [row] = tradeReceiptRows({ action: 'edit', status: 'pending', price: 2400, trigger_price: 2390 },
    { request: { price: 2450, trigger_price: 2440, remaining_amount: .3 } });
  assert.equal(row.price, 2400);
  assert.equal(row.requestedPrice, 2450);
  assert.equal(row.triggerPrice, 2390);
  assert.equal(row.requestedTriggerPrice, 2440);
  assert.equal(row.amountIsRemaining, true);
  assert.equal(row.result, null);
});

test('cancel price is never substituted with a fill average or market quote', () => {
  const [row] = tradeReceiptRows({ action: 'cancel', status: 'completed', average_price: 98, fill: { price: 98 } });
  assert.equal(row.price, undefined);
  assert.equal(row.priceFromRequest, false);
});

test('batched actions match request by original index, not display position', () => {
  const rows = tradeReceiptRows({ results: [{ index: 1, action: 'edit', status: 'failed' }, { index: 0, action: 'open', status: 'submitted' }] },
    { request: { actions: [{ action: 'open', price: 100 }, { action: 'edit', price: 200 }] } });
  assert.deepEqual(rows.map(row => row.price), [200, 100]);
  assert.equal(rows[0].priceFromRequest, true);
});

test('chat and decision tool requests match exact call id including function arguments', () => {
  const tool = { role: 'tool', tool_call_id: 'second' };
  const messages = [{ role: 'assistant', tool_calls: [{ id: 'first', args: { price: 100 } },
    { id: 'second', function: { arguments: '{"price":200}' } }] }, tool];
  assert.deepEqual(toolRequest(messages, tool), { price: 200 });
  assert.deepEqual(toolRequest(messages, { role: 'tool' }), {});
  assert.deepEqual(toolRequest(messages, { role: 'tool', arguments: '{broken' }), {});
});
