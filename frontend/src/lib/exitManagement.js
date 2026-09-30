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
