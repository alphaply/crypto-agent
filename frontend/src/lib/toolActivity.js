const names = {
  open_position: ['开仓', 'Open position'], close_position: ['平仓', 'Close position'],
  edit_order: ['修改挂单', 'Amend order'], cancel_order: ['撤销挂单', 'Cancel order'],
  set_position_protection: ['设置止盈止损', 'Set protection'],
  execute_trade_actions: ['交易操作', 'Trade actions'], get_trade_operation: ['核对执行结果', 'Verify execution'],
  get_account: ['查询账户', 'Read account'], get_market_data: ['查询行情', 'Read market data'],
};

export function toolDisplayName(name, zh) {
  return names[name]?.[zh ? 0 : 1] || name || (zh ? '工具调用' : 'Tool call');
}

// Progress is evidence, not a timer: never infer execution from elapsed time.
export function pendingDecisionTools(calls, messages, progress, { active, streaming }) {
  const results = new Set(messages.filter(item => item.role === 'tool').map(item => item.tool_call_id));
  return calls.filter(call => !call.id || !results.has(call.id)).map(call => ({
    ...call,
    status: active ? progress.find(item => item.id && item.id === call.id)?.status
      || (streaming ? 'preparing' : 'planned') : 'unknown',
  }));
}

export function updateToolActivity(calls = [], event) {
  if (event.type === 'tool_result') return calls.filter(call => call.id !== event.tool_call_id);
  if (event.type === 'model_complete') return calls.map(call => ({ ...call, status: 'planned' }));
  if (event.type === 'tool_start') return [
    ...calls.filter(call => call.id !== event.tool_call_id),
    { id: event.tool_call_id, name: event.tool, status: 'running' },
  ];
  if (event.type !== 'tool_calls') return calls;
  const next = [...calls];
  for (const incoming of event.tool_calls || []) {
    const index = next.findIndex(call => (incoming.id && call.id === incoming.id)
      || (incoming.index != null && call.index === incoming.index));
    if (index < 0) next.push({ ...incoming, status: 'preparing' });
    else next[index] = { ...next[index], ...Object.fromEntries(Object.entries(incoming).filter(([, value]) => value != null && value !== '')) };
  }
  return next;
}
