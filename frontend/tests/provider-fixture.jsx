import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { Select } from 'antd';
import AdminPage from '../src/pages/AdminPage';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

localStorage.setItem('crypto-agent-admin-active-tab', 'providers');
createRoot(document.getElementById('root')).render(
  <AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
    <Select aria-label="Trading symbol" defaultValue="ETH/USDT" style={{ width: 220 }}
      options={['ETH/USDT', 'BTC/USDT'].map((value) => ({ value, label: value }))} />
    <AdminPage />
  </PreferencesContext.Provider></AccessibleConfigProvider>,
);
