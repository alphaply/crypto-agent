import React from 'react';
import { createRoot } from 'react-dom/client';
import { Card, Space } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import { ToolReceipt } from '../src/components/DecisionMessages';
import ToolResultContent from '../src/components/ToolResultContent';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';

const zh = new URLSearchParams(location.search).get('locale') !== 'en';
const receipt = {
  status: 'submitted',
  results: [{ index: 0, action: 'open', symbol: 'ETH/USDT:USDT', status: 'submitted',
    result: { order_id: 'regular:8389766296353383276', side: 'SHORT', price: 2403, requested_amount: .13, filled_amount: 0,
      order_status: 'open', query: { status: 'partial', errors: ['MCP budget exhausted at account_rate_limit; request=background-receipt'] } } }],
  query: { status: 'partial', errors: [{ index: 0, errors: ['核验超时，保留上次已确认的订单结果。'] }] },
  account_summary: { balances: { 'ETH/USDT:USDT': 473.46 }, positions: [], verified: false, missing_value: null },
  receipt_version: 1, contract_version: 3,
};
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider>
  <PreferencesContext.Provider value={{ locale: zh ? 'zh' : 'en', isDark: false, t: value => value }}>
    <main style={{ maxWidth: 1000, margin: 'auto', padding: 16 }}><Space orientation="vertical" style={{ width: '100%' }}>
      <Card title="工具回执 · 离线合成数据"><ToolReceipt zh={zh} item={{ tool: 'execute_trade_actions', result: receipt }} /></Card>
      <Card title="聊天 JSON 回执"><ToolResultContent zh={zh} content={JSON.stringify(receipt)} /></Card>
      <Card title="历史文本回执"><ToolResultContent zh={zh} content={'**已提交挂单**，等待成交。'} /></Card>
    </Space></main>
  </PreferencesContext.Provider>
</AccessibleConfigProvider>);
