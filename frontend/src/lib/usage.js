export function costEntries(row = {}) {
  const entries = Object.entries(row.costs_by_currency || {}).filter(([, value]) => value != null && Number.isFinite(Number(value)));
  if (entries.length) return entries;
  const amount = row.cost ?? row.total_cost;
  return amount != null && Number.isFinite(Number(amount)) && row.currency ? [[row.currency, Number(amount)]] : [];
}

export function dailyCostSeries(rows = [], currency) {
  return [{ name: currency, data: [...rows].reverse().map((row) => ({
    name: row.day,
    value: Object.hasOwn(row.costs_by_currency || {}, currency) ? row.costs_by_currency[currency] : null,
  })) }];
}
