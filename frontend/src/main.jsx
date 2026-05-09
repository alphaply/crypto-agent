/* eslint-disable react-refresh/only-export-components */
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App as AntdApp, ConfigProvider, theme as antdTheme } from 'antd';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter } from 'react-router-dom';
import 'antd/dist/reset.css';
import App from './App';
import { PreferencesProvider, usePreferences } from './app/preferences';
import './index.css';

function ConfiguredApp() {
  const { locale, isDark } = usePreferences();

  return (
    <ConfigProvider
      locale={locale === 'zh' ? zhCN : enUS}
      theme={{
        algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          borderRadius: 8,
          colorPrimary: '#2563eb',
          colorBgLayout: isDark ? '#0b0f17' : '#f6f8fb',
          colorBorderSecondary: isDark ? '#263142' : '#e5e7eb',
        },
      }}
    >
      <AntdApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <PreferencesProvider>
      <ConfiguredApp />
    </PreferencesProvider>
  </React.StrictMode>,
);
