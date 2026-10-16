function reasonText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(reasonText).filter(Boolean).slice(0, 2).join('; ');
  if (value && typeof value === 'object') return reasonText(value.message || value.error || value.reason || value.error_code);
  return '';
}

export function receiptSummary(content, zh = false) {
  let value = content;
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return { text: value.replace(/\s+/g, ' ').slice(0, 180), value: content }; }
  }
  const root = value && typeof value === 'object' ? value : {};
  const nested = root.result && typeof root.result === 'object' ? root.result : root;
  const rows = root.results || nested.results;
  const row = Array.isArray(rows) ? rows.find((item) => ['failed', 'unknown', 'pending'].includes(item.status)) || rows[0] || nested : nested;
  const detail = row.result && typeof row.result === 'object' ? row.result : row;
  const status = root.status || nested.status || detail.status;
  const facts = [detail.symbol || row.symbol, detail.side || row.side];
  const quantity = detail.filled_amount ?? detail.fill?.amount;
  if (quantity != null) facts.push(`${zh ? '已成交' : 'Filled'} ${quantity}`);
  else if (detail.amount ?? detail.requested_amount) facts.push(`${zh ? '数量' : 'Amount'} ${detail.amount ?? detail.requested_amount}`);
  const price = detail.average_price ?? detail.fill?.price ?? detail.price;
  if (price != null) facts.push(`${zh ? '价格' : 'Price'} ${price}`);
  if (Array.isArray(rows)) facts.unshift(`${rows.length} ${zh ? '项操作' : 'actions'}`);
  const error = reasonText(root.error || nested.error || detail.error || row.error || detail.query?.errors || detail.cleanup?.error || detail.reason || row.reason || detail.message || row.message || detail.error_code || row.error_code || detail.reason_code || row.reason_code);
  if (error) facts.push(error);
  const message = detail.summary;
  if (typeof message === 'string') facts.push(message);
  if (!facts.filter(Boolean).length) {
    const collection = Array.isArray(value) ? value : Object.values(root).find(Array.isArray);
    facts.push(collection ? `${collection.length} ${zh ? '条记录' : 'records'}` : (zh ? '结果已返回' : 'Result received'));
  }
  return { status, text: facts.filter(Boolean).join(' · ').slice(0, 180), value };
}
