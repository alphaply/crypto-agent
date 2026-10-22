import React from 'react';
import { createRoot } from 'react-dom/client';
import { Card, Space, theme } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import { ToolReceipt } from '../src/components/DecisionMessages';
import ToolResultContent from '../src/components/ToolResultContent';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';

const zh = new URLSearchParams(location.search).get('locale') !== 'en';
const dark = new URLSearchParams(location.search).get('theme') === 'dark';
document.documentElement.dataset.theme = dark ? 'dark' : 'light';
const receipt = {
  status: 'submitted',
  results: [{ index: 0, action: 'open', symbol: 'ETH/USDT:USDT', status: 'submitted',
    result: { order_id: 'regular:8389766296353383276', side: 'SHORT', price: 2403, requested_amount: .13, filled_amount: 0,
      order_status: 'open', query: { status: 'partial', errors: ['MCP budget exhausted at account_rate_limit; request=background-receipt'] } } }],
  query: { status: 'partial', errors: [{ index: 0, errors: ['核验超时，保留上次已确认的订单结果。'] }] },
  account_summary: { balances: { 'ETH/USDT:USDT': 473.46 }, positions: [], verified: false, missing_value: null },
  receipt_version: 1, contract_version: 3,
};
const batch = {
  contract_version: 3, receipt_version: 1, operation_id: 'call_00_Ze0GorUV7ZrxOyEEcmN35320', status: 'failed',
  execution_policy: 'independent', partial_execution: true, rejected_by_user: false,
  results: [
    { index: 0, action: 'open', symbol: 'MUB/USDT', side: 'LONG', status: 'not_executed',
      reason_code: 'budget_exhausted', reason: '所有标的共享本轮预算 30，本轮剩余 5.99984，申请 6.00003' },
    { index: 1, action: 'open', symbol: 'BTC/USDT', side: 'LONG', status: 'not_executed', reason_code: 'budget_exhausted' },
    { index: 2, action: 'protect', symbol: 'BTC/USDT', side: 'LONG', status: 'skipped', reason_code: 'dependency_not_completed' },
  ],
  summary: { completed: 0, submitted: 0, failed: 0, unknown: 0, pending: 0, not_executed: 2, skipped: 1 },
  account_summary: { status: 'local_projection', equity: 1000, observed_at: 1792155645.5 },
  details_omitted: true,
  audit_reference: { operation_id: 'call_00_Ze0GorUV7ZrxOyEEcmN35320' },
  receipt_updated_at: 1792155645.548852,
};
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
  <PreferencesContext.Provider value={{ locale: zh ? 'zh' : 'en', isDark: dark, t: value => value }}>
    <main style={{ maxWidth: 1000, margin: 'auto', padding: 16 }}><Space orientation="vertical" style={{ width: '100%' }}>
      <Card title="工具回执 · 离线合成数据"><ToolReceipt zh={zh} item={{ tool: 'execute_trade_actions', result: receipt }} /></Card>
      <Card title="批量失败回执 · 离线合成数据"><ToolReceipt zh={zh} item={{ tool: 'execute_trade_actions', status: 'failed', result: batch }} /></Card>
      <Card title={zh ? '改单与撤单 · 离线合成数据' : 'Amend & cancel · Synthetic data'}>
        <ToolReceipt zh={zh} item={{ tool: 'execute_trade_actions', args: { actions: [{ action: 'edit', remaining_amount: .25, price: 2450 }, { action: 'cancel' }] }, result: { results: [
          { index: 0, action: 'edit', symbol: 'ETH/USDT', status: 'pending', result: { price: 2400, side: 'BUY' } },
          { index: 1, action: 'cancel', symbol: 'BTC/USDT', status: 'completed', result: { price: 68500, amount: .002, side: 'BUY' } },
        ] } }} />
      </Card>
      <Card title={zh ? '现货与条件单 · 离线合成数据' : 'Spot & conditional orders · Synthetic data'}>
        <ToolResultContent zh={zh} toolName="open_position_spot_dca" content={{ status: 'submitted', results: [{ symbol: 'PEPE/USDT', entry_price: .00001234, amount: 500000 }] }} />
        <ToolResultContent zh={zh} toolName="close" content={{ action: 'close', symbol: 'ETH/USDT:USDT', status: 'submitted', order_type: 'stop_limit', price: 2200, trigger_price: 2210, amount: .1, side: 'LONG' }} />
        <ToolResultContent zh={zh} toolName="close" content={{ action: 'close', symbol: 'ETH/USDT', status: 'completed', order_type: 'market', amount: .05 }} />
        <ToolResultContent zh={zh} toolName="cancel" content={{ action: 'cancel', symbol: 'BTC/USDT', status: 'completed' }} />
      </Card>
      <Card title="聊天工具结果"><ToolResultContent zh={zh} toolName="execute_trade_actions" content={JSON.stringify(receipt)} /></Card>
      <Card title="历史文本回执"><ToolResultContent zh={zh} toolName="execute_trade_actions" content={'**已提交挂单**，等待成交。'} /></Card>
    </Space></main>
  </PreferencesContext.Provider>
</AccessibleConfigProvider>);
