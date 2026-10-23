import React from 'react';
import { Tabs, theme } from 'antd';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import McpSettingsPanel from '../src/components/McpSettingsPanel';
import 'antd/dist/reset.css';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import '../src/index.css';

const params = new URLSearchParams(window.location.search);
const locale = params.get('locale') || 'zh';
const isDark = params.get('theme') === 'dark';
document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }} locale={locale === 'zh' ? zhCN : enUS}><PreferencesContext.Provider value={{ isDark, locale, t: (v) => v }}><div style={{ padding: 16, maxWidth: 1440, margin: '0 auto' }}><Tabs defaultActiveKey={params.get('tab') || 'paper'} items={[{ key: 'mcp', label: 'MCP', children: <McpSettingsPanel /> }, { key: 'paper', label: 'QQBot · Paper', children: <McpSettingsPanel section="paper" /> }]} /></div></PreferencesContext.Provider></AccessibleConfigProvider>);
