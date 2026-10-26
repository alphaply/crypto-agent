/* eslint-disable react-refresh/only-export-components -- Standalone browser test entry. */
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { WorkspacePanel } from '../src/components/AgentWorkspace';
import { PreferencesContext } from '../src/app/preferences-context';
import { api } from '../src/lib/api';
import 'antd/dist/reset.css';
import '../src/index.css';

function Fixture() {
  const [workspace, setWorkspace] = useState(null);
  const [timeframe, setTimeframe] = useState('1h');
  const [chartSymbol, setChartSymbol] = useState('BTC/USDT');
  const params = new URLSearchParams(window.location.search);
  useEffect(() => {
    const refresh = () => api.get('/spot-cost-fixture').then(({ data }) => setWorkspace(data));
    refresh();
    window.addEventListener('crypto-agent-dashboard-refresh', refresh);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', refresh);
  }, []);
  return <AccessibleConfigProvider>
    <PreferencesContext.Provider value={{ isDark: false, locale: params.get('locale') || 'zh', t: value => value }}>
      <div style={{ padding: 12 }}>
        {workspace ? <WorkspacePanel workspace={workspace} timeframe={timeframe} setTimeframe={setTimeframe}
          chartSymbol={chartSymbol} setChartSymbol={setChartSymbol} authenticated={params.get('auth') !== '0'} /> : null}
      </div>
    </PreferencesContext.Provider>
  </AccessibleConfigProvider>;
}

createRoot(document.getElementById('root')).render(<Fixture />);
