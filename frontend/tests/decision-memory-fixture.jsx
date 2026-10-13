import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { ShortMemoryPanel, WorkspacePanel } from '../src/components/AgentWorkspace';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

const params = new URLSearchParams(window.location.search);
const locale = params.get('locale') || 'zh';
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider>
  <PreferencesContext.Provider value={{ isDark: false, locale, t: (value) => value }}>
    {params.has('workspace') ? <WorkspacePanel authenticated={params.get('public') !== 'true'}
      timeframe="15m" setTimeframe={() => {}} workspace={{
        agent: { config_id: 'cfg', symbol: 'ETH/USDT', mode: 'STRATEGY' },
        short_memories: { short_memories: [window.__memorySnapshotFixture] },
      }} /> : <ShortMemoryPanel authenticated={params.get('public') !== 'true'}
      dashboard={{ current_symbol: 'ETH/USDT', symbols: ['ETH/USDT'], agent_summaries: [{ config_id: 'cfg' }, { config_id: 'other' }] }} />}
  </PreferencesContext.Provider>
</AccessibleConfigProvider>);
