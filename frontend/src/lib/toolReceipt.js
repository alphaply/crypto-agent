function reasonText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(reasonText).filter(Boolean).slice(0, 2).join('; ');
  if (value && typeof value === 'object') return reasonText(value.message || value.error || value.reason || value.error_code);
  return '';
}

const tradeActions = new Set(['open', 'close', 'edit', 'cancel', 'protect', 'amend_entry', 'amend_exit', 'update_protection']);
const singleTradeTools = {
  open_position: 'open', close_position: 'close', edit_order: 'edit',
  cancel_order: 'cancel', set_position_protection: 'protect',
};
export function isTradingTool(name = '') {
  return Object.hasOwn(singleTradeTools, name) || tradeActions.has(name) || /^(execute_trade_actions|execute_trade|open_position_|close_position_|cancel_orders_|update_entry_order_|update_exit_order|update_position_protection_)/.test(name);
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

// A preflight rejection belongs to the batch. Showing it as the first action
// would attach the error to the wrong trade when a later item is invalid.
export function tradeValidationMessage(content, request, zh, toolName) {
  const receipt = parseReceipt(content);
  const preflight = receipt?.status === 'failed' && receipt.phase === 'preflight' && receipt.submitted === false
    && ['invalid_request', 'position_mode_unavailable'].includes(receipt.error_code) && !receipt.partial_execution;
  const message = preflight ? receipt.error : receipt;
  if (typeof message !== 'string') return '';
  const text = message.replace(/^Error:\s*/, '');
  if (Object.hasOwn(singleTradeTools, toolName)) {
    if (!preflight && !text.startsWith('交易参数校验失败：')) return '';
    // The shared backend stores a one-item v3 request, but this result belongs
    // only to this tool call, never to other calls in the same model response.
    const detail = text.replace(/第\s*1\s*项\s*|actions\[0\]:\s*/g, '')
      .replace(/本批未提交。/g, zh ? '本次操作未提交。' : 'This operation was not submitted. ');
    return detail.startsWith('交易参数校验失败：') || detail.includes('本次') ? detail
      : (zh ? '本次操作未提交。' : 'This operation was not submitted. ') + detail;
  }
  if (text.startsWith('交易参数校验失败：') || text.startsWith('本批未提交。')) return text;
  const actions = objectValue(request).actions;
  // Older decision records sometimes contain only one formatted line per action.
  const lines = text.trim().split('\n');
  const legacyMarketError = /actions\[(\d+)\]: unsupported_combination: live entry supports limit only/;
  const marketError = legacyMarketError.exec(text);
  if (marketError && (preflight || (actions?.length === lines.length && lines.every((line, index) =>
    line.startsWith(`${index + 1}. `) && line.includes('，未执行。原因：' + marketError[0]))))) {
    const item = Number(marketError[1]) + 1;
    return zh ? `第 ${item} 项开仓只支持限价单。填 order_type=limit 并提供委托价格 price。本批未提交。`
      : `Item ${item}: entries require order_type=limit and a limit price (price). No actions were submitted.`;
  }
  if (!/^\d+ validation errors? for (?:Unified)?TradeActionsSchema\b/i.test(text)) {
    return preflight ? (zh ? '本批未提交。' : 'No actions were submitted. ') + text : '';
  }

  // Old Pydantic receipts used the same sentence for a missing and an invalid side.
  const legacy = /^1 validation error for (?:Unified)?TradeActionsSchema\s+actions\.(\d+)\s+Value error, close\.side is required/i.exec(text);
  if (!legacy) return text;
  const index = Number(legacy[1]);
  const item = actions?.[index];
  const missing = item?.action === 'close' && !Object.hasOwn(item, 'side');
  const reason = zh ? `第 ${index + 1} 项平仓${missing ? '缺少 side' : '的 side 无效或缺失'}。合约平多填 LONG，平空填 SHORT。现货卖出填 LONG。`
    : `Item ${index + 1} (close): ${missing ? 'missing side' : 'missing or invalid side'}. Use LONG to close a long, SHORT to close a short, and LONG for a spot sale.`;
  return preflight ? reason + (zh ? '本批未提交。' : ' No actions were submitted.') : reason;
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
  if (Object.hasOwn(singleTradeTools, toolName)) return singleTradeTools[toolName];
  if (/^(open_position_|BUY_LIMIT|SELL_LIMIT)/.test(value || toolName)) return 'open';
  if (/^close_position_/.test(value || toolName)) return 'close';
  if (/^cancel_orders_/.test(value || toolName)) return 'cancel';
  if (/^update_(entry|exit)_order/.test(value || toolName)) return 'edit';
  if (/^update_position_protection_/.test(value || toolName)) return 'protect';
  return tradeActions.has(toolName) ? toolName : undefined;
}

export function tradeReceiptRows(content, { request = {}, fallbackStatus, toolName } = {}) {
  let root = parseReceipt(content);
  if (typeof root === 'string') {
    const lines = root.trim().split('\n').filter(line => line.trim());
    const parsed = lines.map(line => {
      const match = /^\s*(\d+)\.\s*(open|close|edit|cancel|protect)：?\s*:?\s*([^\s，]+)\s+(LONG|SHORT|BUY|SELL)\s+数量([\d.eE+-]+)\s+价格([\d.eE+-]+)，(失败|未执行|已提交|已成交|部分成交|待核验)[。.]?(?:\s*原因[：:](.*))?$/.exec(line);
      if (!match) return null;
      const [, index, action, symbol, side, amount, price, state, error] = match;
      if (!Number.isFinite(Number(amount)) || !Number.isFinite(Number(price))) return null;
      return { index: Number(index) - 1, action, symbol, side, amount: Number(amount), requested_price: Number(price),
        status: ({ 失败: 'failed', 未执行: 'not_executed', 已提交: 'submitted', 已成交: 'filled', 部分成交: 'partially_filled', 待核验: 'unknown' })[state], error };
    });
    if (!parsed.length || parsed.some(row => !row)) return null;
    root = { results: parsed };
  }
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
    let status = String(row.status ?? detail.status ?? (rows.length === 1 ? root.status ?? fallbackStatus ?? '' : '')).toLowerCase();
    const orderStatus = String(detail.order_status || '').toLowerCase();
    if (['submitted', 'success', 'completed'].includes(status) && ['filled', 'partially_filled', 'canceled', 'cancelled'].includes(orderStatus)) status = orderStatus;
    const rejected = ['failed', 'rejected', 'not_executed', 'skipped', 'not_requested'].includes(status);
    const accepted = ['submitted', 'completed', 'success', 'open', 'closed', 'filled', 'canceled', 'cancelled', 'waiting_trigger'].includes(status)
      || detail.submission_state === 'submitted' || detail.submitted === true;
    const price = detail.price ?? detail.entry_price ?? row.price ?? row.entry_price;
    const triggerPrice = detail.trigger_price ?? row.trigger_price;
    const requestedPrice = detail.requested_price ?? args.price ?? args.entry_price;
    const requestedTriggerPrice = detail.requested_trigger_price ?? args.trigger_price;
    return {
      action,
      originalReason: typeof args.reason === 'string' ? args.reason : undefined,
      symbol: detail.symbol ?? row.symbol ?? intent.symbol ?? envelope.symbol,
      side: detail.pos_side ?? detail.position_side ?? detail.side ?? row.pos_side ?? row.position_side ?? row.side ?? args.side ?? args.pos_side ?? ({ BUY_LIMIT: 'BUY', SELL_LIMIT: 'SELL' }[args.action]),
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
      orderType: detail.order_type ?? detail.exit_type ?? row.order_type ?? row.exit_type ?? args.order_type,
      orderKind: detail.kind ?? row.kind,
      error: rejected || ['pending', 'unknown'].includes(status) ? reasonText(detail.error || row.error || detail.reason || row.reason) : '',
      status,
      filledAmount: detail.filled_amount ?? detail.fill?.amount,
      averagePrice: detail.average_price ?? detail.fill?.price,
      result: rejected ? 'failed' : accepted ? 'success' : null,
    };
  });
}

export function friendlyTradeError(error, zh) {
  if (/\b(?:MIN_)?NOTIONAL\b/.test(error || '')) return zh ? '订单金额不符合交易所限制' : 'Order value does not meet exchange limits';
  if (/Filter failure: LOT_SIZE/.test(error || '')) return zh ? '订单数量不符合交易所限制' : 'Order quantity does not meet exchange limits';
  return error;
}

const actionLabels = {
  open: ['开仓', 'Open'], close: ['平仓', 'Close'], edit: ['改单', 'Amend'], cancel: ['撤单', 'Cancel'],
  protect: ['保护', 'Protect'], amend_entry: ['入场改单', 'Amend entry'],
  amend_exit: ['退出改单', 'Amend exit'], update_protection: ['保护调整', 'Update protection'],
};

export function tradeReceiptLabels(row, zh = false) {
  const language = zh ? 0 : 1;
  const type = String(row.orderType || '').toLowerCase();
  const stop = ['stop_market', 'stop_limit', 'stop_loss', 'stop_loss_limit'].includes(type);
  const profit = ['take_profit_market', 'take_profit_limit', 'take_profit'].includes(type);
  const editing = row.action === 'edit' || row.action?.startsWith('amend_');
  const entry = row.action === 'open' || row.action === 'amend_entry' || row.orderKind === 'entry';
  const exit = !entry && (row.action === 'close' || row.action === 'amend_exit' || row.orderKind === 'exit'
    || stop || profit || type === 'oco');
  let action = (actionLabels[row.action] || ['交易操作', 'Trade'])[language];
  if (editing && exit) {
    if (stop) action = ['修改止损', 'Amend stop loss'][language];
    else if (profit) action = ['修改止盈', 'Amend take profit'][language];
    else if (type === 'oco') action = ['修改止盈止损', 'Amend TP/SL'][language];
    else action = ['退出改单', 'Amend exit'][language];
  }
  const side = String(row.side || '').toUpperCase();
  // A position direction alone cannot establish whether an amendment opens
  // or closes it. Historical receipts without order evidence stay neutral.
  const positions = exit
    ? { LONG: ['平多', 'Close long'], SHORT: ['平空', 'Close short'] }
    : entry
      ? { LONG: ['做多', 'Open long'], SHORT: ['做空', 'Open short'] }
      : { LONG: ['多头仓位', 'Long position'], SHORT: ['空头仓位', 'Short position'] };
  return { action, side: (positions[side] || { BUY: ['买入', 'Buy'], SELL: ['卖出', 'Sell'] }[side])?.[language] || side };
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
