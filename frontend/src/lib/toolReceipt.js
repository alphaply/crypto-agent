function reasonText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(reasonText).filter(Boolean).slice(0, 2).join('; ');
  if (value && typeof value === 'object') return reasonText(value.message || value.error || value.reason || value.error_code);
  return '';
}

const tradeActions = new Set(['open', 'close', 'edit', 'cancel', 'protect', 'amend_entry', 'amend_exit', 'update_protection']);
export function isTradingTool(name = '') {
  return tradeActions.has(name) || /^(execute_trade_actions|execute_trade|open_position_|close_position_|cancel_orders_|update_entry_order_|update_exit_order|update_position_protection_)/.test(name);
}

export function parseReceipt(content) {
  if (typeof content !== 'string') return content;
  try { return JSON.parse(content); } catch { return content; }
}

export function isTradeReceipt(content, toolName) {
  if (toolName) return isTradingTool(toolName);
  const value = parseReceipt(content);
  return Boolean(value && typeof value === 'object' && (tradeActions.has(value.action)
    || Array.isArray(value.results) && value.results.some(row => tradeActions.has(row.action))));
}

export function tradeReceiptRows(content, { request = {}, fallbackStatus, toolName } = {}) {
  const root = parseReceipt(content);
  if (!root || typeof root !== 'object') return null;
  const rows = Array.isArray(root) ? root : Array.isArray(root.results) ? root.results : [root];
  const envelope = request && typeof request === 'object' ? request : {};
  const requests = envelope.actions || envelope.orders || [envelope];
  return rows.map((value, index) => {
    const row = value && typeof value === 'object' ? value : {};
    const intent = requests[row.index ?? index] || {};
    const detail = row.result && typeof row.result === 'object' ? row.result : row;
    const args = intent.order || intent;
    const action = row.action || root.action || intent.action || (tradeActions.has(toolName) ? toolName : undefined);
    const status = row.status ?? detail.status ?? (rows.length === 1 ? root.status ?? fallbackStatus : undefined);
    const rejected = ['failed', 'rejected', 'not_executed', 'skipped', 'not_requested'].includes(status);
    const accepted = ['submitted', 'completed', 'success', 'open', 'closed', 'filled', 'canceled', 'cancelled', 'waiting_trigger'].includes(status)
      || detail.submission_state === 'submitted' || detail.submitted === true;
    return {
      action,
      symbol: detail.symbol ?? row.symbol ?? intent.symbol ?? envelope.symbol,
      side: detail.side ?? detail.pos_side ?? row.side ?? args.side ?? args.pos_side,
      amount: detail.quantity_mode === 'all' || args.close_all === true ? 'all'
        : action === 'edit' || action?.startsWith('amend_')
          ? detail.requested_remaining_amount ?? args.remaining_amount ?? detail.requested_amount ?? detail.amount ?? args.amount
          : detail.requested_amount ?? detail.amount ?? row.amount ?? args.amount,
      price: detail.price ?? row.price ?? args.price ?? args.entry_price,
      triggerPrice: detail.trigger_price ?? args.trigger_price,
      result: rejected ? 'failed' : accepted ? 'success' : null,
    };
  });
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
