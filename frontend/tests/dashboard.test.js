import test from 'node:test';
import assert from 'node:assert/strict';
import { workspaceSignature, selectDashboardTab } from '../src/lib/dashboard.js';

test('streaming progress and next-run ticks do not reload every workspace', () => {
  const agent = { config_id: 'eth', timestamp: '2026-09-19 10:00', market_timeframes: ['1h'], execution: { reasoning_content: 'one' } };
  const next = { ...agent, next_run: '10:30', execution: { reasoning_content: 'one two' } };
  assert.equal(workspaceSignature([agent]), workspaceSignature([next]));
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...next, timestamp: '2026-09-19 10:20' }]));
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...next, config_id: 'btc' }]));
});

test('single strategy opens directly after switching symbols or deleting an old selection', () => {
  assert.equal(selectDashboardTab([{ config_id: 'btc' }], 'eth'), 'btc');
  assert.equal(selectDashboardTab([{ config_id: 'btc' }], null), 'btc');
  assert.equal(selectDashboardTab([{ config_id: 'btc' }], 'compare'), 'compare');
  assert.equal(selectDashboardTab([{ config_id: 'btc' }, { config_id: 'eth' }], 'eth'), 'eth');
  assert.equal(selectDashboardTab([], 'eth'), 'compare');
});
