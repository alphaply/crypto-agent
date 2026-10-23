import test from 'node:test';
import assert from 'node:assert/strict';
import { newPaperPositionOperationId, paperPositionIntent, paperPositionPayload } from '../src/lib/paperPositionAdjustment.js';

test('position adjustments reject fractional or out-of-range leverage and preserve explicit values', () => {
  for (const value of [null, '', 0, -1, 1.5, 126, Infinity]) {
    assert.equal(paperPositionPayload('set_leverage', value, null, 125).error, 'leverage');
  }
  assert.deepEqual(paperPositionPayload('set_leverage', 10, null, 125).payload, { action: 'set_leverage', leverage: 10 });
});

test('margin validation rejects invalid or excess precision without silently rounding', () => {
  for (const value of [null, '', 0, -1, '0.000000001', '1.123456789', Infinity, true]) {
    assert.equal(paperPositionPayload('add_margin', null, value, 125).error, 'amount');
  }
  assert.deepEqual(paperPositionPayload('add_margin', null, '0.00000001', 125).payload, { action: 'add_margin', amount: 0.00000001 });
  assert.deepEqual(paperPositionPayload('reduce_margin', null, '123.45678901', 125).payload, { action: 'reduce_margin', amount: 123.45678901 });
});

test('retry intent is stable for equivalent numeric input and isolated by account, position and action', () => {
  const position = { namespace: 'qqbot', user_id: '123', resource_id: 'position-1' };
  const payload = paperPositionPayload('add_margin', null, '100.00', 125).payload;
  const key = paperPositionIntent(position, payload);
  assert.equal(key, paperPositionIntent(position, paperPositionPayload('add_margin', null, '100', 125).payload));
  for (const changed of [{ ...position, namespace: 'other' }, { ...position, user_id: '456' }, { ...position, resource_id: 'position-2' }]) {
    assert.notEqual(key, paperPositionIntent(changed, payload));
  }
  assert.notEqual(key, paperPositionIntent(position, { action: 'reduce_margin', amount: 100 }));
  assert.notEqual(key, paperPositionIntent(position, { action: 'add_margin', amount: 101 }));
});

test('new adjustment operation identities are unique and satisfy the API identifier contract', () => {
  const first = newPaperPositionOperationId();
  const second = newPaperPositionOperationId();
  assert.match(first, /^[a-zA-Z0-9_.:-]{8,160}$/);
  assert.notEqual(first, second);
});
