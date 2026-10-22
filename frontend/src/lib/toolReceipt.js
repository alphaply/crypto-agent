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

function objectValue(value) {
  const parsed = parseReceipt(value);
  return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
}

export function toolRequest(messages, message) {
  if (message.args != null || message.arguments != null) return objectValue(message.args ?? message.arguments);
  if (!message.tool_call_id) return {};
  const index = messages.indexOf(message);
  const call = messages.slice(0, index < 0 ? messages.length : index)
    .flatMap(item => item.tool_calls || []).find(item => item.id === message.tool_call_id);
  return objectValue(call?.args ?? call?.function?.arguments ?? call?.arguments);
}

function actionName(value, toolName = '') {
  if (tradeActions.has(value)) return value;
  if (/^(open_position_|BUY_LIMIT|SELL_LIMIT)/.test(value || toolName)) return 'open';
  if (/^close_position_/.test(value || toolName)) return 'close';
  if (/^cancel_orders_/.test(value || toolName)) return 'cancel';
  if (/^update_(entry|exit)_order/.test(value || toolName)) return 'edit';
  if (/^update_position_protection_/.test(value || toolName)) return 'protect';
  return tradeActions.has(toolName) ? toolName : undefined;
}

export function tradeReceiptRows(content, { request = {}, fallbackStatus, toolName } = {}) {
  const root = parseReceipt(content);
  if (!root || typeof root !== 'object') return null;
  const rows = Array.isArray(root) ? root : Array.isArray(root.results) ? root.results : [root];
  const envelope = objectValue(request);
  const requests = Array.isArray(envelope.actions) ? envelope.actions : Array.isArray(envelope.orders) ? envelope.orders : [envelope];
  return rows.map((value, index) => {
    const row = objectValue(value);
    const intent = objectValue(requests[row.index ?? index]);
    const detail = row.result && typeof row.result === 'object' ? row.result : row;
    const args = objectValue(intent.order || intent);
    const action = actionName(row.action || root.action || intent.action, toolName);
    const editing = action === 'edit' || action?.startsWith('amend_');
    const status = row.status ?? detail.status ?? (rows.length === 1 ? root.status ?? fallbackStatus : undefined);
    const rejected = ['failed', 'rejected', 'not_executed', 'skipped', 'not_requested'].includes(status);
    const accepted = ['submitted', 'completed', 'success', 'open', 'closed', 'filled', 'canceled', 'cancelled', 'waiting_trigger'].includes(status)
      || detail.submission_state === 'submitted' || detail.submitted === true;
    const price = detail.price ?? detail.entry_price ?? row.price ?? row.entry_price;
    const triggerPrice = detail.trigger_price ?? row.trigger_price;
    const requestedPrice = detail.requested_price ?? args.price ?? args.entry_price;
    const requestedTriggerPrice = detail.requested_trigger_price ?? args.trigger_price;
    return {
      action,
      symbol: detail.symbol ?? row.symbol ?? intent.symbol ?? envelope.symbol,
      side: detail.side ?? detail.pos_side ?? row.side ?? args.side ?? args.pos_side ?? ({ BUY_LIMIT: 'BUY', SELL_LIMIT: 'SELL' }[args.action]),
      amount: detail.quantity_mode === 'all' || args.close_all === true ? 'all'
        : editing
          ? detail.requested_remaining_amount ?? args.remaining_amount ?? detail.requested_amount ?? detail.amount ?? args.amount
          : detail.requested_amount ?? detail.amount ?? row.amount ?? args.amount,
      amountIsRemaining: editing && (detail.requested_remaining_amount != null || args.remaining_amount != null),
      price: price ?? requestedPrice,
      triggerPrice: triggerPrice ?? requestedTriggerPrice,
      priceFromRequest: price == null && requestedPrice != null,
      triggerFromRequest: triggerPrice == null && requestedTriggerPrice != null,
      requestedPrice: editing && price != null && requestedPrice != null && Number(price) !== Number(requestedPrice) ? requestedPrice : undefined,
      requestedTriggerPrice: editing && triggerPrice != null && requestedTriggerPrice != null && Number(triggerPrice) !== Number(requestedTriggerPrice) ? requestedTriggerPrice : undefined,
      stopLoss: detail.stop_loss ?? args.stop_loss,
      takeProfit: detail.take_profit ?? args.take_profit,
      stopLimitPrice: detail.stop_limit_price ?? args.stop_limit_price,
      takeProfitLimitPrice: detail.take_profit_limit_price ?? args.take_profit_limit_price,
      orderType: detail.order_type ?? detail.exit_type ?? args.order_type,
      status,
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
