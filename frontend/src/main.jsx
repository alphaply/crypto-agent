import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider, theme as antdTheme } from 'antd';
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
          borderRadius: 16,
          colorPrimary: '#0f766e',
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
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
