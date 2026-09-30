// Keep exchange snapshots safe for Lightweight Charts' ordered, unique time axis.
function orderedRows(rows, fields, candleTimes) {
  const byTime = new Map();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!row || row.time == null) continue;
    const next = { time: Number(row.time) };
    if (!Number.isFinite(next.time) || next.time <= 0 || (candleTimes && !candleTimes.has(next.time))) continue;
    for (const field of fields) next[field] = row[field] == null || row[field] === '' ? NaN : Number(row[field]);
    if (!fields.every(field => Number.isFinite(next[field]))) continue;
    if (fields.includes('open') && (next.low > Math.min(next.open, next.close) || next.high < Math.max(next.open, next.close))) continue;
    if (row.color && /^(?:#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(?:0|1|0?\.\d+)\s*\))$/i.test(row.color)) next.color = row.color;
    byTime.set(next.time, next);
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

export function normalizeKlineData(payload) {
  const candles = orderedRows(payload?.candles, ['open', 'high', 'low', 'close']);
  const times = new Set(candles.map(bar => bar.time));
  return {
    candles,
    volume: orderedRows(payload?.volume, ['value'], times),
    emas: Object.fromEntries(Object.entries(payload?.emas || {})
      .filter(([span]) => /^\d+$/.test(span))
      .map(([span, rows]) => [span, orderedRows(rows, ['value'], times)])),
  };
}

function sameBar(a, b) {
  return a?.time === b?.time && a?.open === b?.open && a?.high === b?.high
    && a?.low === b?.low && a?.close === b?.close && a?.value === b?.value && a?.color === b?.color;
}

// Only the open candle / newly appended candles normally change. Corrections to
// history and rolling windows still need a replacement (especially recalculated EMAs).
export function syncSeriesData(series, previous, next, reset = false) {
  let changed = 0;
  while (changed < Math.min(previous.length, next.length) && sameBar(previous[changed], next[changed])) changed += 1;
  if (!reset && changed === previous.length && changed === next.length) return false;
  if (!reset && previous.length && next.length >= previous.length
      && changed >= previous.length - 1 && next[previous.length - 1]?.time === previous.at(-1).time) {
    for (let index = changed; index < next.length; index += 1) series.update(next[index]);
  } else {
    series.setData(next);
  }
  return true;
}

export function recentKlineRange(length, width) {
  const count = Math.min(length, Math.max(24, Math.min(140, Math.floor((width - 72) / 8))));
  const padding = width < 600 ? 3 : 5;
  return { from: Math.max(0, length - count), to: Math.max(0, length - 1) + padding };
}

export function preserveKlineRange(range, previous, next) {
  if (!range || !previous.length || !next.length) return null;
  const width = range.to - range.from;
  if (range.to >= previous.length - 1) {
    const to = next.length - 1 + (range.to - previous.length + 1);
    return { from: to - width, to };
  }
  // Match actual timestamps: month candles and missing exchange bars are not uniform.
  const times = new Map(next.map((bar, index) => [bar.time, index]));
  const start = Math.max(0, Math.min(previous.length - 1, Math.ceil(range.from)));
  let anchor = previous.findIndex((bar, index) => index >= start && times.has(bar.time));
  if (anchor < 0) anchor = previous.findIndex(bar => times.has(bar.time));
  if (anchor < 0) return null;
  const shift = times.get(previous[anchor].time) - anchor;
  const from = range.from + shift;
  const to = range.to + shift;
  if (to < 0 || from >= next.length) return null;
  return { from, to };
}

export function klinePriceFormat(candles) {
  const value = Math.abs(candles.at(-1)?.close || 0);
  const precision = value >= 100 ? 2 : value >= 1 ? 4 : value > 0 ? Math.min(12, Math.max(4, 3 - Math.floor(Math.log10(value)))) : 2;
  return { type: 'price', precision, minMove: 10 ** -precision };
}
