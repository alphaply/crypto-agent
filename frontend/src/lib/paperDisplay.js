export function formatPaperAmount(value, zh) {
  if (value == null || value === '' || typeof value === 'boolean') return '—';
  const amount = Number(value);
  return Number.isFinite(amount)
    ? amount.toLocaleString(zh ? 'zh-CN' : 'en-US', { maximumFractionDigits: 8 })
    : '—';
}

export function paperFilterCount(filter) {
  if (!filter) return null;
  return ['excluded_resources', 'unresolved_resources'].reduce((total, kind) => (
    total + Object.values(filter[kind] || {}).reduce((sum, value) => sum + (Number(value) || 0), 0)
  ), 0);
}

export function paperValuationIncomplete(account) {
  return account.valuation_status === 'incomplete' || account.completeness?.valuation === 'incomplete';
}
