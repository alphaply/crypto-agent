import React, { useMemo, useState } from 'react';
import { BulbOutlined, GlobalOutlined, MenuOutlined } from '@ant-design/icons';
import { Button, Drawer, Grid, Segmented, Space, Typography } from 'antd';
import { usePreferences } from '../app/preferences';

const { useBreakpoint } = Grid;

function PreferenceControls({ compact = false }) {
  const { locale, setLocale, theme, setTheme, t } = usePreferences();

  return (
    <Space size={compact ? 'small' : 'middle'} wrap>
      <Space size={6} className="topbar-control">
        <GlobalOutlined />
        <Segmented
          size="small"
          value={locale}
          onChange={setLocale}
          options={[
            { label: 'EN', value: 'en', title: t('localeEnHint') },
            { label: '中', value: 'zh', title: t('localeZhHint') },
          ]}
        />
      </Space>
      <Space size={6} className="topbar-control">
        <BulbOutlined />
        <Segmented
          size="small"
          value={theme}
          onChange={setTheme}
          options={[
            { label: t('light'), value: 'light', title: t('themeLightHint') },
            { label: t('dark'), value: 'dark', title: t('themeDarkHint') },
          ]}
        />
      </Space>
    </Space>
  );
}

export default function AppTopBar({ items, activeKey, onNavigate, actions }) {
  const screens = useBreakpoint();
  const isMobile = !screens.lg;
  const [drawerOpen, setDrawerOpen] = useState(false);

  const navButtons = useMemo(
    () =>
      items.map((item) => (
        <Button
          key={item.key}
          type={activeKey === item.key ? 'primary' : 'text'}
          onClick={() => {
            setDrawerOpen(false);
            onNavigate(item.key);
          }}
        >
          {item.label}
        </Button>
      )),
    [activeKey, items, onNavigate],
  );

  return (
    <>
      <header className="app-topbar">
        <div className="app-topbar__brand">
          <Typography.Text strong className="app-topbar__logo">
            Crypto Agent
          </Typography.Text>
        </div>

        {!isMobile ? (
          <>
            <Space className="app-topbar__nav" wrap>
              {navButtons}
            </Space>
            <Space className="app-topbar__actions" wrap size="middle">
              <PreferenceControls />
              {actions}
            </Space>
          </>
        ) : (
          <Space size="small">
            <PreferenceControls compact />
            <Button
              className="app-topbar__menu"
              icon={<MenuOutlined />}
              onClick={() => setDrawerOpen(true)}
            />
          </Space>
        )}
      </header>

      <Drawer
        placement="right"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={280}
        title="Crypto Agent"
      >
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space direction="vertical" style={{ width: '100%' }}>
            {items.map((item) => (
              <Button
                key={item.key}
                type={activeKey === item.key ? 'primary' : 'default'}
                block
                onClick={() => {
                  setDrawerOpen(false);
                  onNavigate(item.key);
                }}
              >
                {item.label}
              </Button>
            ))}
          </Space>
          <PreferenceControls />
          <div>{actions}</div>
        </Space>
      </Drawer>
    </>
  );
}
