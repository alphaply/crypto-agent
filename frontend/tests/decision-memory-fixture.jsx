import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { ShortMemoryPanel } from '../src/components/AgentWorkspace';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

const params = new URLSearchParams(window.location.search);
const locale = params.get('locale') || 'zh';
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider>
  <PreferencesContext.Provider value={{ isDark: false, locale, t: (value) => value }}>
    <ShortMemoryPanel authenticated={params.get('public') !== 'true'}
      dashboard={{ current_symbol: 'ETH/USDT', symbols: ['ETH/USDT'], agent_summaries: [{ config_id: 'cfg' }, { config_id: 'other' }] }} />
  </PreferencesContext.Provider>
</AccessibleConfigProvider>);
