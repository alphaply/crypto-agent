import React, { useEffect, useMemo, useState } from 'react';
import { BulbOutlined, GlobalOutlined, MenuOutlined } from '@ant-design/icons';
import { Button, Drawer, Grid, Select, Space, Tooltip, Typography } from 'antd';
import { usePreferences } from '../app/preferences';
import { api } from '../lib/api';

const { useBreakpoint } = Grid;

function PreferenceControls({ compact = false }) {
  const { locale, setLocale, theme, setTheme, t } = usePreferences();
  const nextLocale = locale === 'zh' ? 'en' : 'zh';
  const nextTheme = theme === 'dark' ? 'light' : 'dark';

  return (
    <Space size={compact ? 4 : 8} className="topbar-icon-controls">
      <Tooltip title={nextLocale === 'zh' ? t('localeZhHint') : t('localeEnHint')}>
        <Button
          className="topbar-icon-button"
          size="small"
          icon={<GlobalOutlined />}
          onClick={() => setLocale(nextLocale)}
          aria-label={t('switchLanguage')}
        >
          {locale === 'zh' ? '中' : 'EN'}
        </Button>
      </Tooltip>
      <Tooltip title={nextTheme === 'dark' ? t('themeDarkHint') : t('themeLightHint')}>
        <Button
          className="topbar-icon-button"
          size="small"
          icon={<BulbOutlined />}
          onClick={() => setTheme(nextTheme)}
          aria-label={t('switchTheme')}
        />
      </Tooltip>
    </Space>
  );
}

export default function AppTopBar({ items, activeKey, onNavigate, actions, extraActions }) {
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

  const renderSymbolSelect = () => (
    symbols.length ? (
      <Select
        className="topbar-symbol"
        size="small"
        value={selectedSymbol || undefined}
        onChange={setSelectedSymbol}
        options={symbols.map((item) => ({ label: item, value: item }))}
        placeholder={t('symbol')}
      />
    ) : null
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
              {renderSymbolSelect()}
              {extraActions}
              <PreferenceControls />
              {actions}
            </Space>
          </>
        ) : (
          <Space size={8} className="app-topbar__mobile-actions">
            {extraActions}
            <Button className="app-topbar__menu" icon={<MenuOutlined />} onClick={() => setDrawerOpen(true)} />
          </Space>
        )}
      </header>

      <Drawer placement="right" open={drawerOpen} onClose={() => setDrawerOpen(false)} width={280} title={t('brand')}>
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          {symbols.length ? (
            <Select
              style={{ width: '100%' }}
              size="small"
              value={selectedSymbol || undefined}
              onChange={setSelectedSymbol}
              options={symbols.map((item) => ({ label: item, value: item }))}
              placeholder={t('symbol')}
            />
          ) : null}
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
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <PreferenceControls compact />
            {extraActions}
          </Space>
          <div>{actions}</div>
        </Space>
      </Drawer>
    </>
  );
}
