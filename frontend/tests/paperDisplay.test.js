import test from 'node:test';
import assert from 'node:assert/strict';
import { formatPaperAmount, paperValuationReasons } from '../src/lib/paperDisplay.js';

test('valuation explanations use the server reasons and preserve unrecognized codes', () => {
  const account = { completeness: { reason_codes: ['paper_performance_scan_incomplete', 'future_reason'] } };
  assert.deepEqual(paperValuationReasons(account, true), ['历史交易记录尚未扫描完毕。', 'future_reason']);
  assert.deepEqual(paperValuationReasons(account, false), ['The historical trade scan is not complete yet.', 'future_reason']);
  assert.deepEqual(paperValuationReasons({ valuation_status: 'incomplete' }, true), []);
});

test('unknown valuation values stay unavailable rather than appearing as zero', () => {
  for (const value of [null, undefined, '', true, 'unavailable', Infinity]) {
    assert.equal(formatPaperAmount(value, true), '—');
  }
  assert.equal(formatPaperAmount(0, true), '0');
});
