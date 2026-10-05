// Polling execution tokens should not restart market-data requests or charts.
export function workspaceSignature(agents = []) {
  return JSON.stringify(agents.map((agent) => [agent.config_id, agent.timestamp, agent.market_timeframes, agent.symbols || [agent.symbol]]));
}

export function selectDashboardTab(agents = [], requested) {
  if (requested === 'compare') return 'compare';
  if (agents.some((agent) => agent.config_id === requested)) return requested;
  return agents.length === 1 ? agents[0].config_id : 'compare';
}

export function activityRecordKey(row) {
  return JSON.stringify([row.config_id || '', row.symbol || '', row.activity_type || '',
    row.trade_id || row.id || row.order_id || '', row.event_type || '', row.timestamp || '']);
}

// Keep case intact: the API distinguishes one minute (1m) from one month (1M).
export const CHART_TIMEFRAME_STORAGE_KEY = 'crypto-agent-chart-timeframe';
export const CHART_TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1M'];
const DEFAULT_CHART_TIMEFRAMES = ['15m', '30m', '1h', '4h', '1d', '1w', '1M'];

export function isChartTimeframe(value) {
  return CHART_TIMEFRAMES.includes(value);
}

export function readChartTimeframe(storage) {
  try {
    const saved = (storage === undefined ? globalThis.localStorage : storage)?.getItem(CHART_TIMEFRAME_STORAGE_KEY);
    return isChartTimeframe(saved) ? saved : '1h';
  } catch {
    return '1h';
  }
}

export function saveChartTimeframe(value, storage) {
  if (!isChartTimeframe(value)) return false;
  try {
    const target = storage === undefined ? globalThis.localStorage : storage;
    if (!target) return false;
    target.setItem(CHART_TIMEFRAME_STORAGE_KEY, value);
    return true;
  } catch {
    // Private browsing or a full/disabled storage must not prevent chart use.
    return false;
  }
}

export function chartTimeframeOptions(configured, selected) {
  const valid = Array.isArray(configured) ? configured.filter(isChartTimeframe) : [];
  const options = new Set(valid.length ? valid : DEFAULT_CHART_TIMEFRAMES);
  // Market-context settings vary by task; the chart API also accepts a saved
  // viewing interval even when it isn't part of that task's prompt context.
  if (isChartTimeframe(selected)) options.add(selected);
  return CHART_TIMEFRAMES.filter((value) => options.has(value));
}
