import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import AdminPage from '../src/pages/AdminPage';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

localStorage.setItem('crypto-agent-admin-active-tab', 'tasks');
createRoot(document.getElementById('root')).render(
  <AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
    <main style={{ padding: 16, minWidth: 0 }}><AdminPage /></main>
  </PreferencesContext.Provider></AccessibleConfigProvider>,
);
