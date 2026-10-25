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

const valuationReasons = {
  excluded_venue: ['存在 Binance 范围以外的历史资产或委托。', 'Historical assets or orders are outside the Binance scope.'],
  unresolved_origin: ['部分资产或委托的来源尚未核实。', 'The origin of some assets or orders has not been verified.'],
  paper_capital_unverified: ['用于计算收益的本金尚未核实。', 'The capital used to calculate returns has not been verified.'],
  paper_performance_scan_incomplete: ['历史交易记录尚未扫描完毕。', 'The historical trade scan is not complete yet.'],
  paper_historical_performance_unverified: ['部分历史交易收益尚不能确认为 Binance 范围内的收益。', 'Some historical trading returns cannot be verified within the Binance scope.'],
};

export function paperValuationReasons(account, zh) {
  const codes = account.completeness?.reason_codes;
  return Array.isArray(codes) ? codes.map((code) => valuationReasons[code]?.[zh ? 0 : 1] || String(code)) : [];
}
