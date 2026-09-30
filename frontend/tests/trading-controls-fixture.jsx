import React from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import TradingRulesPanel from '../src/components/TradingRulesPanel';
import { WorkspacePanel } from '../src/pages/DashboardPage';
import { PreferencesContext } from '../src/app/preferences-context';
import '../src/index.css';

const workspace = {
  agent: { config_id: 'fixture', symbol: 'ETH/USDT', mode: 'REAL' },
  position: {
    positions: [{ symbol: 'ETH/USDT:USDT', side: 'LONG', entry_price: 2500, mark_price: 2510, qty: 0.3 }],
    exit_management: {
      mode: 'independent_exits', pending: true, uncovered: { LONG: 0.1, SHORT: 0 },
      exits: [
        { order_id: 'exit-tp', pos_side: 'LONG', exit_type: 'take_profit_limit', price: 2600, amount: 0.1, remaining: 0.1, status: 'open' },
        { order_id: 'exit-sl', pos_side: 'LONG', exit_type: 'stop_market', trigger_price: 2400, amount: 0.2, remaining: 0.2, status: 'open' },
      ],
    },
  },
};
createRoot(document.getElementById('root')).render(<ConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
  <main style={{ padding: 16, minWidth: 0 }}>
    <h1>模拟交易规则与退出单</h1>
    <TradingRulesPanel agents={[{ config_id: 'fixture', symbol: 'ETH/USDT', title: '模拟任务' }]} />
    <WorkspacePanel workspace={workspace} timeframe="15m" setTimeframe={() => {}} authenticated />
  </main>
</PreferencesContext.Provider></ConfigProvider>);
