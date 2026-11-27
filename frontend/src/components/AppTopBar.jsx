import React, { useState } from 'react';
import {
  AppstoreOutlined,
  BarChartOutlined,
  CommentOutlined,
  GlobalOutlined,
  HistoryOutlined,
  MenuOutlined,
  MoonOutlined,
  MoreOutlined,
  RobotOutlined,
  SettingOutlined,
  SunOutlined,
} from '@ant-design/icons';
import { Button, Drawer, Grid, Popover, Space, Tooltip, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';

const NAV_GROUPS = [
  {
    id: 'frontstage',
    zh: '交易工作台',
    en: 'Workspace',
    items: [
      { key: '/', zh: '总览', en: 'Overview', icon: <AppstoreOutlined /> },
      { key: '/agents', zh: 'Agent 运行', en: 'Agents', icon: <RobotOutlined /> },
      { key: '/console/chat', zh: '任务聊天', en: 'Task Chat', icon: <CommentOutlined /> },
      { key: '/history', zh: '运行历史', en: 'History', icon: <HistoryOutlined /> },
      { key: '/usage', zh: '用量统计', en: 'Usage', icon: <BarChartOutlined /> },
    ],
  },
  {
    id: 'backstage',
    zh: '后台配置',
    en: 'Administration',
    items: [
      { key: '/console/config', zh: '系统设置', en: 'Settings', icon: <SettingOutlined /> },
    ],
  },
];

const ALL_NAV_ITEMS = NAV_GROUPS.flatMap((group) =>
  group.items.map((item) => ({ ...item, groupId: group.id })),
);

function BrandMark() {
  return (
    <span className="workspace-brand-mark" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
        <rect x="4" y="10" width="3.2" height="9" rx="1.6" fill="currentColor" opacity="0.65" />
        <rect x="10.4" y="5" width="3.2" height="14" rx="1.6" fill="currentColor" />
        <rect x="16.8" y="8" width="3.2" height="11" rx="1.6" fill="currentColor" opacity="0.85" />
        <circle cx="12" cy="3.2" r="1.4" fill="currentColor" />
      </svg>
    </span>
  );
}

function PreferenceControls({ compact = false }) {
  const { locale, setLocale, theme, setTheme, t } = usePreferences();
  const isDark = theme === 'dark';

  return (
    <div className={`workspace-pref-group${compact ? ' is-compact' : ''}`}>
      <Tooltip title={t('switchLanguage')}>
        <Button
          size="small"
          className="workspace-pref-btn"
          icon={<GlobalOutlined />}
          onClick={() => setLocale(locale === 'zh' ? 'en' : 'zh')}
          aria-label={t('switchLanguage')}
        >
          {locale === 'zh' ? '中' : 'EN'}
        </Button>
      </Tooltip>
      <Tooltip title={t('switchTheme')}>
        <Button
          size="small"
          className="workspace-pref-btn"
          icon={isDark ? <SunOutlined /> : <MoonOutlined />}
          onClick={() => setTheme(isDark ? 'light' : 'dark')}
          aria-label={t('switchTheme')}
        />
      </Tooltip>
    </div>
  );
}

export default function AppTopBar({ activeKey, onNavigate, actions, extraActions }) {
  const { locale } = usePreferences();
  const screens = Grid.useBreakpoint();
  const [open, setOpen] = useState(false);

  const currentKey =
    activeKey === '/console/history'
      ? '/history'
      : activeKey === '/console/agents'
        ? '/agents'
        : activeKey;
  const current = ALL_NAV_ITEMS.find((item) => item.key === currentKey) || ALL_NAV_ITEMS[0];
  const isBackstage = current.groupId === 'backstage';

  const handleNavClick = (itemKey) => {
    setOpen(false);
    const target = itemKey === '/agents' && activeKey?.startsWith('/console') ? '/console/agents' : itemKey;
    onNavigate(target);
  };

  const navContent = (
    <nav className="workspace-navigation" aria-label={locale === 'zh' ? '主导航' : 'Main navigation'}>
      {NAV_GROUPS.map((group) => (
        <div key={group.id} className="workspace-nav-group">
          <div className="workspace-nav-group-label">{locale === 'zh' ? group.zh : group.en}</div>
          <div className="workspace-nav-group-items">
            {group.items.map((item) => {
              const active = currentKey === item.key;
              return (
                <button
                  type="button"
                  key={item.key}
                  className={`workspace-nav-item${active ? ' is-active' : ''}`}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => handleNavClick(item.key)}
                >
                  <span className="workspace-nav-icon">{item.icon}</span>
                  <span className="workspace-nav-label">{locale === 'zh' ? item.zh : item.en}</span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );

  const brandBlock = (
    <button
      type="button"
      className="workspace-brand workspace-brand-trigger"
      onClick={() => handleNavClick('/')}
    >
      <BrandMark />
      <div className="workspace-brand-copy">
        <strong>Crypto Agent</strong>
        <small>{locale === 'zh' ? 'AI 交易工作台' : 'AI Trading Desk'}</small>
      </div>
    </button>
  );

  return (
    <>
      {screens.lg && (
        <aside className="workspace-sidebar">
          {brandBlock}
          {navContent}
          <div className="workspace-sidebar-footer">
            <span className="signal-dot" />
            <span>{locale === 'zh' ? '研究 · 决策 · 执行' : 'Research · Decide · Execute'}</span>
          </div>
        </aside>
      )}

      <header className="app-topbar workspace-topbar">
        <div className="workspace-page-title">
          {!screens.lg && (
            <Button
              size="small"
              className="workspace-menu-btn"
              icon={<MenuOutlined />}
              aria-label={locale === 'zh' ? '打开导航' : 'Open navigation'}
              onClick={() => setOpen(true)}
            />
          )}
          <span className="workspace-title-icon" aria-hidden="true">
            {current.icon}
          </span>
          <Typography.Text strong className="workspace-title-text">
            {locale === 'zh' ? current.zh : current.en}
          </Typography.Text>
          {screens.sm && (
            <span className={`workspace-context-badge${isBackstage ? ' is-backstage' : ''}`}>
              {isBackstage
                ? locale === 'zh'
                  ? '后台配置'
                  : 'Admin'
                : locale === 'zh'
                  ? '工作台'
                  : 'Desk'}
            </span>
          )}
        </div>

        <Space size={8} className="workspace-header-actions">
          {extraActions}
          {screens.md ? (
            <>
              {extraActions ? <span className="workspace-divider" aria-hidden="true" /> : null}
              <PreferenceControls />
              {actions ? <span className="workspace-divider" aria-hidden="true" /> : null}
              {actions}
            </>
          ) : (
            <Popover
              trigger="click"
              placement="bottomRight"
              content={
                <div className="workspace-mobile-actions">
                  <div className="workspace-mobile-section">
                    <span className="workspace-mobile-label">
                      {locale === 'zh' ? '偏好设置' : 'Preferences'}
                    </span>
                    <PreferenceControls compact />
                  </div>
                  {actions ? <div className="workspace-mobile-section">{actions}</div> : null}
                </div>
              }
            >
              <Button
                size="small"
                icon={<MoreOutlined />}
                aria-label={locale === 'zh' ? '更多操作与偏好设置' : 'More actions and preferences'}
              />
            </Popover>
          )}
        </Space>
      </header>

      <Drawer
        placement="left"
        open={open}
        onClose={() => setOpen(false)}
        size={280}
        className="workspace-drawer"
        title={
          <div className="workspace-drawer-brand">
            <BrandMark />
            <div>
              <strong>Crypto Agent</strong>
              <small>{locale === 'zh' ? 'AI 交易工作台' : 'AI Trading Desk'}</small>
            </div>
          </div>
        }
      >
        <div className="workspace-drawer-body">
          {navContent}
          <div className="workspace-sidebar-footer">
            <span className="signal-dot" />
            <span>{locale === 'zh' ? '研究 · 决策 · 执行' : 'Research · Decide · Execute'}</span>
          </div>
        </div>
      </Drawer>
    </>
  );
}
