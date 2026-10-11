export const EXIT_MODES = ['attached_required', 'attached_optional', 'independent_exits'];

export function resolveExitMode(config = {}) {
  if (EXIT_MODES.includes(config.exit_mode)) return config.exit_mode;
  return String(config.mode || '').toUpperCase() === 'REAL' ? 'attached_optional' : 'attached_required';
}

export function exitModeLabel(mode, locale = 'zh') {
  const labels = locale === 'zh'
    ? { attached_required: '必填止盈止损', attached_optional: '可选止盈止损', independent_exits: '独立退出单' }
    : { attached_required: 'Required TP/SL', attached_optional: 'Optional TP/SL', independent_exits: 'Independent exits' };
  return labels[mode] || mode || '—';
}

export function formatExitNumber(value) {
  if (value === undefined || value === null || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('en-US', { maximumFractionDigits: 12 }) : '—';
}

export function exitOrderType(order) {
  return order.order_type || (Number(order.contract_version || 1) === 1 && order.exit_type === 'take_profit_limit' ? 'limit' : order.exit_type);
}

export function exitOrderLabel(order, locale = 'zh') {
  const labels = locale === 'zh'
    ? { market: '市价退出', limit: '限价退出', stop_market: '触发市价止损', stop_limit: '触发限价止损', take_profit_market: '触发市价止盈', take_profit_limit: '触发限价止盈' }
    : { market: 'Market exit', limit: 'Limit exit', stop_market: 'Stop market', stop_limit: 'Stop limit', take_profit_market: 'Take profit market', take_profit_limit: 'Take profit limit' };
  return labels[exitOrderType(order)] || exitOrderType(order) || '—';
}

export function exitQuantityLabel(order, field, locale = 'zh') {
  if (order.quantity_mode === 'all' && (field === 'amount' || field === 'remaining')) return locale === 'zh' ? '全部' : 'All';
  return formatExitNumber(order[field]);
}
