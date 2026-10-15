import React from 'react';
import { createRoot } from 'react-dom/client';
import { Card, Typography } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import DecisionMessages from '../src/components/DecisionMessages';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';

const zh = new URLSearchParams(location.search).get('locale') !== 'en';
const receipt = { receipt_version: 1, contract_version: 2, operation_id: 'offline-close', status: 'submitted',
  order_status: 'waiting_trigger', requested_amount: .03, filled_amount: 0, order_id: 'algo:offline-original',
  verification: { state: 'background' } };
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: zh ? 'zh' : 'en', t: (value) => value }}>
  <main style={{ padding: 20, maxWidth: 1000, margin: 'auto' }}>
    <Typography.Title level={2}>{zh ? '单次交易调用 · 离线验收' : 'Single financial call · offline verification'}</Typography.Title>
    <Card><DecisionMessages zh={zh} messages={[
      { role: 'assistant', content: zh ? '已受理条件退出，后续状态由系统自动更新。' : 'Conditional exit accepted. The system updates subsequent state automatically.',
        tool_calls: [{ id: 'offline-close', name: 'execute_trade_actions', args: { contract_version: 3, actions: [{ action: 'close', side: 'SHORT', amount: .03, order_type: 'take_profit_market', trigger_price: 2380, reason: 'offline fixture' }] } }] },
      { role: 'tool', tool: 'execute_trade_actions', tool_call_id: 'offline-close', status: 'submitted', result: receipt,
        audit: { url: '/trade-receipts/fixture-task/offline-close/audit' } },
    ]} /></Card>
  </main>
</PreferencesContext.Provider></AccessibleConfigProvider>);
