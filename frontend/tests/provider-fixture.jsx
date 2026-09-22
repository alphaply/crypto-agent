import React from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import AdminPage from '../src/pages/AdminPage';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

localStorage.setItem('crypto-agent-admin-active-tab', 'providers');
createRoot(document.getElementById('root')).render(
  <ConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
    <AdminPage />
  </PreferencesContext.Provider></ConfigProvider>,
);
