import test from 'node:test';
import assert from 'node:assert/strict';
import { workspaceSignature, selectDashboardTab } from '../src/lib/dashboard.js';
import { resolveExitMode, formatExitNumber } from '../src/lib/exitManagement.js';

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

test('legacy tasks retain their exit defaults and explicit modes take precedence', () => {
  assert.equal(resolveExitMode({ mode: 'REAL' }), 'attached_optional');
  assert.equal(resolveExitMode({ mode: 'STRATEGY' }), 'attached_required');
  assert.equal(resolveExitMode({ mode: 'REAL', exit_mode: 'attached_required' }), 'attached_required');
  assert.equal(resolveExitMode({ mode: 'STRATEGY', exit_mode: 'independent_exits' }), 'independent_exits');
});

test('exit quantities distinguish missing coverage from zero and retain small sizes', () => {
  assert.equal(formatExitNumber(undefined), '—');
  assert.equal(formatExitNumber(null), '—');
  assert.equal(formatExitNumber(''), '—');
  assert.equal(formatExitNumber(0), '0');
  assert.equal(formatExitNumber(0.00000001), '0.00000001');
  assert.equal(formatExitNumber('bad snapshot'), '—');
});
