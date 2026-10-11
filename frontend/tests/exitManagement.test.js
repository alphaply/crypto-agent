import test from 'node:test';
import assert from 'node:assert/strict';
import { exitOrderLabel, exitQuantityLabel, formatExitNumber } from '../src/lib/exitManagement.js';

test('legacy LIMIT and conditional TP LIMIT have distinct labels', () => {
  assert.equal(exitOrderLabel({ exit_type: 'take_profit_limit' }), '限价退出');
  assert.equal(exitOrderLabel({ contract_version: 2, exit_type: 'take_profit_limit' }), '触发限价止盈');
});
test('dynamic all is not rendered as zero or a frozen estimate', () => {
  const order = { quantity_mode: 'all', amount: null, remaining: null, coverage_estimate: .5 };
  assert.equal(exitQuantityLabel(order, 'remaining'), '全部');
  assert.equal(exitQuantityLabel(order, 'amount'), '全部');
  assert.equal(exitQuantityLabel(order, 'coverage_estimate'), '0.5');
  assert.equal(formatExitNumber(null), '—');
});
