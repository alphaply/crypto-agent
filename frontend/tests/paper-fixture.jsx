import React from 'react';
import { Tabs } from 'antd';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import McpSettingsPanel from '../src/components/McpSettingsPanel';
import 'antd/dist/reset.css';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import '../src/index.css';

const locale = new URLSearchParams(window.location.search).get('locale') || 'zh';
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider locale={locale === 'zh' ? zhCN : enUS}><PreferencesContext.Provider value={{ isDark: false, locale, t: (v) => v }}><div style={{ padding: 16 }}><Tabs items={[{ key: 'mcp', label: 'MCP', children: <McpSettingsPanel /> }, { key: 'paper', label: 'QQBot · Paper', children: <McpSettingsPanel section="paper" /> }]} /></div></PreferencesContext.Provider></AccessibleConfigProvider>);
