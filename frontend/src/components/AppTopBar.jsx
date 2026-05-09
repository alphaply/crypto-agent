import React, { useEffect, useMemo, useState } from 'react';
import { BulbOutlined, GlobalOutlined, MenuOutlined } from '@ant-design/icons';
import { Button, Drawer, Grid, Segmented, Select, Space, Typography } from 'antd';
import { usePreferences } from '../app/preferences';
import { api } from '../lib/api';

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
  const [symbols, setSymbols] = useState([]);
  const { selectedSymbol, setSelectedSymbol, t } = usePreferences();

  useEffect(() => {
    let mounted = true;
    async function loadSymbols() {
      try {
        const response = await api.get('/public/dashboard', { params: selectedSymbol ? { symbol: selectedSymbol } : {} });
        if (!mounted) return;
        const nextSymbols = response.data.symbols || [];
        setSymbols(nextSymbols);
        if (!selectedSymbol && response.data.current_symbol) {
          setSelectedSymbol(response.data.current_symbol);
        }
      } catch {
        if (mounted) setSymbols([]);
      }
    }
    loadSymbols();
    return () => {
      mounted = false;
    };
  }, [selectedSymbol, setSelectedSymbol]);

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
              {symbols.length ? (
                <Select
                  className="topbar-symbol"
                  size="small"
                  value={selectedSymbol || undefined}
                  onChange={setSelectedSymbol}
                  options={symbols.map((item) => ({ label: item, value: item }))}
                  placeholder={t('symbol')}
                />
              ) : null}
              <PreferenceControls />
              {actions}
            </Space>
          </>
        ) : (
          <Space size="small">
            <PreferenceControls compact />
            <Button className="app-topbar__menu" icon={<MenuOutlined />} onClick={() => setDrawerOpen(true)} />
          </Space>
        )}
      </header>

      <Drawer placement="right" open={drawerOpen} onClose={() => setDrawerOpen(false)} width={280} title={t('brand')}>
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
