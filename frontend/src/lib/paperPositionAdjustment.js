export function paperPositionPayload(action, leverage, amount, maxLeverage) {
  if (action === 'set_leverage') {
    const value = Number(leverage);
    if (leverage == null || leverage === '' || !Number.isInteger(value) || value < 1 || (maxLeverage != null && value > maxLeverage)) {
      return { error: 'leverage' };
    }
    return { payload: { action, leverage: value } };
  }
  if (!['add_margin', 'reduce_margin'].includes(action)) return { error: 'action' };
  const text = String(amount ?? '');
  if (!/^\d+(\.\d{1,8})?$/.test(text) || !Number.isFinite(Number(text)) || Number(text) <= 0) {
    return { error: 'amount' };
  }
  return { payload: { action, amount: Number(text) } };
}

export function paperPositionIntent(position, payload) {
  return JSON.stringify([position.namespace, position.user_id, position.resource_id, payload]);
}

export function newPaperPositionOperationId() {
  const random = globalThis.crypto.getRandomValues(new Uint32Array(4));
  return 'paper-position-' + Array.from(random, (value) => value.toString(16).padStart(8, '0')).join('');
}
