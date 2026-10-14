import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button, Card, Space } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import PositionManagementFields from '../src/components/PositionManagementFields';
import ManagementStatus, { ManagedResource } from '../src/components/ManagementStatus';
import ManagementLifecyclePanel from '../src/components/ManagementLifecyclePanel';
import PerformancePanel from '../src/components/PerformancePanel';
import MemoryExecutionSnapshot from '../src/components/MemoryExecutionSnapshot';
import MarkdownBlock from '../src/components/MarkdownBlock';
import McpSettingsPanel from '../src/components/McpSettingsPanel';
import AdminPage from '../src/pages/AdminPage';
import { WorkspacePanel } from '../src/components/AgentWorkspace';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/pages/DashboardPage.css';

const locale = new URLSearchParams(window.location.search).get('locale') || 'zh';
const tasks = new URLSearchParams(window.location.search).get('surface') === 'tasks';
const partial = new URLSearchParams(window.location.search).get('surface') === 'partial';
if (tasks) localStorage.setItem('crypto-agent-admin-active-tab', 'tasks');
function Fixture() {
  const [config, setConfig] = useState({});
  const [changed, setChanged] = useState(false);
  const scopes = [{ symbol: 'ETH/USDT:USDT', side: 'LONG', owner: 'fixture', source: 'baseline', sync_status: 'verified', observed_at: '2026-10-14T04:31:10Z', changes: changed ? ['External reduction: 0.02 → 0.01'] : [] }];
  return <AccessibleConfigProvider><PreferencesContext.Provider value={{ locale, t: (key) => key, isDark: false }}>
    {tasks ? <AdminPage /> : partial ? <main style={{ padding: 16 }}><WorkspacePanel compact authenticated={false} timeframe="1h" setTimeframe={() => {}} workspace={{
      agent: { config_id: 'fixture', mode: 'REAL', symbol: 'ETH/USDT:USDT', exit_mode: 'independent_exits' },
      position: { mode: 'REAL', positions: [], errors: ['fetch_positions_failed:offline'], management: { policy: 'exclusive_scope', scopes: [] } },
      kline: { symbol: 'ETH/USDT:USDT', candles: [], pending_orders: [], orders_complete: false },
      orders: { orders: [] }, short_memories: { short_memories: [] },
    }} /></main> :
    <main style={{ maxWidth: 1200, margin: 'auto', padding: 16 }}><Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title={locale === 'zh' ? '任务管理范围' : 'Task management scope'}><PositionManagementFields value={config} onChange={(patch) => setConfig((old) => ({ ...old, ...patch }))} supported symbols={['ETH/USDT:USDT']} locale={locale} /></Card>
      <Card title="ETH · LONG"><ManagementStatus management={{ policy: 'visible_first_claim', scopes }} /><p>{changed ? '0.01' : '0.02'} @ 2415</p><ManagedResource resource={{ order_id: 'algo:external-tp', owner: 'fixture', source: 'external', managed: true, supported_actions: ['edit', 'cancel'], observed_at: '2026-10-14T04:31:10Z' }} /><p>TP {changed ? '2580' : '2565'} · SL 2458 · Limit 2385</p><Button onClick={() => setChanged(true)}>{locale === 'zh' ? '模拟人工减仓及改单' : 'Simulate external reduction and edit'}</Button></Card>
      <Card><ManagementLifecyclePanel configId="fixture" /></Card>
      <PerformancePanel configId="fixture" />
      <Card title={locale === 'zh' ? '策略记忆' : 'Strategy memory'}><MarkdownBlock content="Wait for the 4h close or CPI reaction. Key levels: 2385 / 2565. Avoid raising leverage after losses." /><MemoryExecutionSnapshot memory={{ id: 12, config_id: 'fixture', bucket_start: '2026-10-14 12:00:00' }} authenticated locale={locale} /></Card>
      <McpSettingsPanel profiles={[{ profile_id: 'saved', name: 'Saved Binance', exchange: 'binance', market_type: 'swap' }]} />
    </Space></main>}
  </PreferencesContext.Provider></AccessibleConfigProvider>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
