import React, { useState } from 'react';
import { AppstoreOutlined, BulbOutlined, CommentOutlined, GlobalOutlined, HistoryOutlined, MenuOutlined, SettingOutlined, BarChartOutlined, RobotOutlined } from '@ant-design/icons';
import { Button, Drawer, Grid, Space, Tooltip, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';

const NAV = [
  { key: '/', zh: '总览', en: 'Overview', icon: <AppstoreOutlined /> },
  { key: '/agents', zh: 'Agent 运行', en: 'Agents', icon: <RobotOutlined /> },
  { key: '/console/chat', zh: '任务聊天', en: 'Tasks', icon: <CommentOutlined /> },
  { key: '/history', zh: '运行历史', en: 'History', icon: <HistoryOutlined /> },
  { key: '/usage', zh: '用量统计', en: 'Usage', icon: <BarChartOutlined /> },
  { key: '/console/config', zh: '系统设置', en: 'Settings', icon: <SettingOutlined /> },
];
function PreferenceControls() {
  const { locale, setLocale, theme, setTheme, t } = usePreferences();
  return <Space size={6}>
    <Tooltip title={t('switchLanguage')}><Button size="small" icon={<GlobalOutlined />} onClick={() => setLocale(locale === 'zh' ? 'en' : 'zh')} aria-label={t('switchLanguage')}>{locale === 'zh' ? '中' : 'EN'}</Button></Tooltip>
    <Tooltip title={t('switchTheme')}><Button size="small" icon={<BulbOutlined />} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-label={t('switchTheme')} /></Tooltip>
  </Space>;
}
export default function AppTopBar({ activeKey, onNavigate, actions, extraActions }) {
  const { locale } = usePreferences();
  const screens = Grid.useBreakpoint();
  const [open, setOpen] = useState(false);
  const currentKey = activeKey === '/console/history' ? '/history' : activeKey === '/console/agents' ? '/agents' : activeKey;
  const current = NAV.find((item) => item.key === currentKey) || NAV[0];
  const nav = <nav className="workspace-navigation" aria-label={locale === 'zh' ? '主导航' : 'Main navigation'}>
    {NAV.map((item) => <button type="button" key={item.key} className={currentKey === item.key ? 'is-active' : ''} aria-current={currentKey === item.key ? 'page' : undefined} onClick={() => { setOpen(false); onNavigate(item.key === '/agents' && activeKey?.startsWith('/console') ? '/console/agents' : item.key); }}>{item.icon}<span>{locale === 'zh' ? item.zh : item.en}</span></button>)}
  </nav>;
  return <>
    {screens.lg && <aside className="workspace-sidebar">
      <div className="workspace-brand"><span className="workspace-brand-mark">C</span><div><strong>Crypto Agent</strong><small>{locale === 'zh' ? '交易与研究工作台' : 'Trading & research'}</small></div></div>
      {nav}
      <div className="workspace-sidebar-footer"><span className="signal-dot" />{locale === 'zh' ? '研究 · 决策 · 执行' : 'Research · Decide · Execute'}</div>
    </aside>}
    <header className="app-topbar workspace-topbar">
      <Space className="workspace-page-title">{!screens.lg && <Button icon={<MenuOutlined />} aria-label={locale === 'zh' ? '打开导航' : 'Open navigation'} onClick={() => setOpen(true)} />}<Typography.Text strong>{locale === 'zh' ? current.zh : current.en}</Typography.Text></Space>
      <Space className="workspace-header-actions" wrap>{extraActions}<PreferenceControls />{actions}</Space>
    </header>
    <Drawer placement="left" open={open} onClose={() => setOpen(false)} size={280} title="Crypto Agent">{nav}</Drawer>
  </>;
}
