export function managementDraft(config = {}) {
  return { ...config, position_management_policy: 'visible_first_claim', management_scopes: [] };
}

export function managementError(config, { supported = true, symbols = null, zh = false } = {}) {
  if (config.position_management_policy !== 'exclusive_scope') return '';
  if (!supported) return zh ? '独占管理仅支持 Binance/OKX 的 REAL 合约独立退出模式。' : 'Exclusive management requires Binance/OKX REAL perpetuals with independent exits.';
  const scopes = config.management_scopes || [];
  if (!scopes.length || scopes.some((scope) => !scope.symbol || !['LONG', 'SHORT'].includes(scope.side))) return zh ? '请选择明确的交易对及 LONG/SHORT 方向。' : 'Choose explicit symbols and LONG/SHORT sides.';
  if (symbols && scopes.some((scope) => !symbols.includes(scope.symbol))) return zh ? '管理范围必须属于此配置允许的标的。' : 'Management scopes must belong to the configured symbols.';
  if (new Set(scopes.map((scope) => `${scope.symbol}:${scope.side}`)).size !== scopes.length) return zh ? '管理范围不能重复。' : 'Management scopes must be unique.';
  return '';
}

export function observationTime(value, locale = 'zh') {
  if (value == null || value === '') return locale === 'zh' ? '尚未核验' : 'Not verified';
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US');
}

export function sourceLabel(source, locale = 'zh') {
  const values = locale === 'zh' ? { agent: 'Agent', external: '外部', baseline: '接管基线' } : { agent: 'Agent', external: 'External', baseline: 'Enrollment baseline' };
  return values[source] || (locale === 'zh' ? '来源未核验' : 'Source unverified');
}

export function managementLabel(resource = {}, locale = 'zh') {
  const status = resource.management_state || resource.claim_status
    || (resource.managed ? 'owned' : resource.can_claim ? 'eligible' : 'unverified');
  const labels = locale === 'zh'
    ? { owned: '已纳管', eligible: '可自动认领', eligible_unclaimed: '可自动认领', read_only: '只读可见', needs_refresh: '证据待核验', unclaimed: '尚未认领', conflict: '其他管理者占用', released: '已释放', unverified: '待核验' }
    : { owned: 'Managed', eligible: 'Eligible for automatic claim', eligible_unclaimed: 'Eligible for automatic claim', read_only: 'Read only', needs_refresh: 'Evidence requires refresh', unclaimed: 'Unclaimed', conflict: 'Managed elsewhere', released: 'Released', unverified: 'Unverified' };
  return labels[status] || status;
}
