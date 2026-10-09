import test from 'node:test';
import assert from 'node:assert/strict';
import {
  workspaceSignature,
  activityRecordKey,
  selectDashboardTab,
  selectAgentId,
  filterAgents,
  CHART_TIMEFRAME_STORAGE_KEY,
  CHART_TIMEFRAMES,
  readChartTimeframe,
  saveChartTimeframe,
  chartTimeframeOptions,
} from '../src/lib/dashboard.js';

test('Agent deep links survive disabled tasks and fall back when a task is deleted', () => {
  const agents = [{ config_id: 'paused', enabled: false }, { config_id: 'live', enabled: true }];
  assert.equal(selectAgentId(agents, 'paused'), 'paused');
  assert.equal(selectAgentId(agents, 'deleted'), 'live');
  assert.equal(selectAgentId(agents, 'compare'), 'live');
  assert.equal(selectAgentId([], 'live'), '');
});

test('Agent directory combines search across portfolio markets with execution filters', () => {
  const agents = [
    { config_id: 'basket', title: 'Portfolio', symbols: ['BTC/USDT', 'ETH/USDT'], execution: { status: 'RUNNING' } },
    { config_id: 'paused', enabled: false, model: 'DeepSeek', symbol: 'SOL/USDT', execution: { status: 'QUEUED' } },
    { config_id: 'scheduled', enabled: true, symbol: 'ETH/USDT', execution: { status: 'FINISHED' } },
  ];
  assert.deepEqual(filterAgents(agents, ' eth ', 'active').map((agent) => agent.config_id), ['basket']);
  assert.deepEqual(filterAgents(agents, 'deepseek', 'all').map((agent) => agent.config_id), ['paused']);
  assert.deepEqual(filterAgents(agents, '', 'enabled').map((agent) => agent.config_id), ['basket', 'scheduled']);
  assert.deepEqual(filterAgents(agents, '', 'active').map((agent) => agent.config_id), ['basket', 'paused']);
});
import { resolveExitMode, formatExitNumber } from '../src/lib/exitManagement.js';

test('exchange IDs are scoped by symbol and task for activity cards', () => {
  const fill = { config_id: 'spot', symbol: 'BTC/USDT', trade_id: '7', activity_type: 'trade' };
  assert.notEqual(activityRecordKey(fill), activityRecordKey({ ...fill, symbol: 'ETH/USDT' }));
  assert.notEqual(activityRecordKey(fill), activityRecordKey({ ...fill, config_id: 'other' }));
});

test('streaming progress and next-run ticks do not reload every workspace', () => {
  const agent = { config_id: 'eth', timestamp: '2026-09-19 10:00', market_timeframes: ['1h'], execution: { reasoning_content: 'one' } };
  const next = { ...agent, next_run: '10:30', execution: { reasoning_content: 'one two' } };
  assert.equal(workspaceSignature([agent]), workspaceSignature([next]));
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...next, timestamp: '2026-09-19 10:20' }]));
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...next, config_id: 'btc' }]));
});

test('adding or removing portfolio symbols refreshes workspace data', () => {
  const agent = { config_id: 'portfolio', symbol: 'BTC/USDT', symbols: ['BTC/USDT'], market_timeframes: ['4h', '1d', '1w'] };
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...agent, symbols: ['BTC/USDT', 'ETH/USDT'] }]));
  assert.equal(workspaceSignature([agent]), workspaceSignature([{ ...agent, execution: { reasoning_content: 'updated' } }]));
});

test('completed memory update refreshes the workspace without a new decision', () => {
  const agent = { config_id: 'eth', timestamp: '2026-10-09 18:00', memory_update: { status: 'pending' } };
  assert.notEqual(workspaceSignature([agent]), workspaceSignature([{ ...agent, memory_update: { status: 'completed' } }]));
  assert.equal(workspaceSignature([agent]), workspaceSignature([{ ...agent, memory_update: { status: 'pending', attempts: 1 } }]));
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

test('chart interval restores the last valid choice without confusing months with minutes', () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  assert.equal(readChartTimeframe(storage), '1h');
  for (const interval of CHART_TIMEFRAMES) {
    assert.equal(saveChartTimeframe(interval, storage), true);
    assert.equal(values.get(CHART_TIMEFRAME_STORAGE_KEY), interval);
    assert.equal(readChartTimeframe(storage), interval);
  }
  assert.equal(saveChartTimeframe('1H', storage), false);
  assert.equal(readChartTimeframe(storage), '1M');
  for (const invalid of ['1H', '2h', 'undefined', '"4h"', '', null]) {
    values.set(CHART_TIMEFRAME_STORAGE_KEY, invalid);
    assert.equal(readChartTimeframe(storage), '1h');
  }
});

test('unavailable local storage does not prevent selecting or opening a chart', () => {
  const blocked = {
    getItem() { throw new Error('Storage access denied'); },
    setItem() { throw new Error('Storage quota exceeded'); },
  };
  assert.equal(readChartTimeframe(blocked), '1h');
  assert.equal(saveChartTimeframe('4h', blocked), false);
  assert.equal(readChartTimeframe(null), '1h');
  assert.equal(saveChartTimeframe('4h', null), false);
});

test('task-specific options include the remembered interval and reject unsupported values', () => {
  assert.deepEqual(chartTimeframeOptions(['4h', '1d'], '15m'), ['15m', '4h', '1d']);
  assert.deepEqual(chartTimeframeOptions(['1M', '1m', '1M', 'bad'], '1M'), ['1m', '1M']);
  assert.deepEqual(chartTimeframeOptions(['1h'], '4h'), ['1h', '4h']);
  assert.deepEqual(chartTimeframeOptions(['unsupported'], '5m'), ['5m', '15m', '30m', '1h', '4h', '1d', '1w', '1M']);
  assert.deepEqual(chartTimeframeOptions(null, 'invalid'), ['15m', '30m', '1h', '4h', '1d', '1w', '1M']);
});

test('global task workspace keeps different symbols and selects the first enabled task', () => {
  const agents = [{ config_id: 'btc', symbol: 'BTC/USDT', enabled: false }, { config_id: 'eth', symbol: 'ETH/USDT', enabled: true }];
  assert.equal(selectDashboardTab(agents, null), 'eth');
  assert.equal(selectDashboardTab(agents, 'btc'), 'btc');
  assert.equal(JSON.parse(workspaceSignature(agents)).length, 2);
});
