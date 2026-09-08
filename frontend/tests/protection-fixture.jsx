import React from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import { WorkspacePanel } from '../src/pages/DashboardPage';
import DatabaseMaintenance from '../src/components/DatabaseMaintenance';
import { PreferencesContext } from '../src/app/preferences-context';
import '../src/index.css';

const workspace = {
  agent: { config_id: 'fixture', symbol: 'ETH/USDT', mode: 'REAL' },
  position: { positions: [{ symbol: 'ETH/USDT:USDT', side: 'LONG', entry_price: 2501.2000000000003, mark_price: 2500,
    qty: .14, take_profit: 2515, stop_loss: 2472, protection_state: 'ACTIVE', protection_revision: 4,
    protection_verified_at: 1788794415 }] },
};
createRoot(document.getElementById('root')).render(<ConfigProvider><PreferencesContext.Provider value={{
  isDark: false, locale: 'zh', t: (value) => value,
}}><main style={{ padding: 16 }}>
  <WorkspacePanel workspace={workspace} timeframe="15m" setTimeframe={() => {}} authenticated />
  <DatabaseMaintenance />
</main></PreferencesContext.Provider></ConfigProvider>);
