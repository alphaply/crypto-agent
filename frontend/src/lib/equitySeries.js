export function equityPointValue(item, point, mode) {
  const spot = item.data_source?.kind === 'spot_holding' || item.mode === 'SPOT_DCA';
  if (mode === 'invested') return spot ? point.invested ?? (item.data_source?.kind === 'dca_invested' ? point.equity : null) : null;
  if (spot) {
    if (mode === 'return') return point.holding_return_pct ?? null;
    return item.data_source?.kind === 'dca_invested' ? null : point.equity ?? null;
  }
  if (mode !== 'return') return point.equity ?? null;
  if (item.mode === 'REAL' || item.data_source?.kind === 'real_equity') {
    return point.return_status === 'ready' ? point.cashflow_return_pct ?? null : null;
  }
  const base = item.points?.find(p => p.equity != null && Number(p.equity) > 0)?.equity;
  return base && point.equity != null ? (Number(point.equity)/Number(base)-1)*100 : null;
}
