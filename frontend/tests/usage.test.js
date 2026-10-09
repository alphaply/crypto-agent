import test from 'node:test';
import assert from 'node:assert/strict';
import { costEntries, dailyCostSeries } from '../src/lib/usage.js';

test('unknown costs stay unknown and explicit free calls stay zero', () => {
  assert.deepEqual(costEntries({ cost: null }), []);
  assert.deepEqual(costEntries({ cost: 0, currency: 'USD' }), [['USD', 0]]);
});

test('different currencies are never summed and missing daily prices remain null', () => {
  assert.deepEqual(costEntries({ costs_by_currency: { USD: 1, CNY: 7 } }), [['USD', 1], ['CNY', 7]]);
  const series = dailyCostSeries([{ day: '2026-10-08', costs_by_currency: { CNY: 7 } }, { day: '2026-10-07', costs_by_currency: { USD: 1 } }], 'USD');
  assert.deepEqual(series[0].data.map((point) => point.value), [1, null]);
});
