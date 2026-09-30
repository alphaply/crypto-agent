import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import KlineChart from '../src/components/KlineChart';
import EquityCompareChart from '../src/components/EquityCompareChart';
import RunScheduleEditor from '../src/components/RunScheduleEditor';
import { WorkspacePanel } from '../src/pages/DashboardPage';
import { PreferencesContext } from '../src/app/preferences-context';
import { useChartTimeframe } from '../src/hooks/useChartTimeframe';
import { chartTimeframeOptions } from '../src/lib/dashboard';
import '../src/index.css';

const FIRST_TIME = 1788710400;
const WORKSPACE_MODE = new URLSearchParams(window.location.search).has('workspace');

function candleAt(index) {
  const open = 60000 + index * 9 + Math.sin(index / 7) * 180;
  const close = open + Math.sin(index * 1.3) * 95;
  return {
    time: FIRST_TIME + index * 3600,
    open,
    high: Math.max(open, close) + 45,
    low: Math.min(open, close) - 45,
    close,
  };
}

export default function Fixture() {
  const [timeframe, setTimeframe] = useChartTimeframe();
  const [displayedTimeframe, setDisplayedTimeframe] = useState(timeframe);
  const [loadState, setLoadState] = useState('ready');
  const [tick, setTick] = useState(0);
  const [dark, setDark] = useState(false);
  const [rules, setRules] = useState([]);
  const [interval, setInterval] = useState(60);
  const [symbol, setSymbol] = useState('BTC');
  const [microPrice, setMicroPrice] = useState(false);
  const [bars, setBars] = useState(() => Array.from({ length: 500 }, (_, index) => candleAt(index)));
  useEffect(() => {
    const reload = () => setLoadState('loading');
    window.addEventListener('crypto-agent-dashboard-refresh', reload);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', reload);
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);
  const payload = useMemo(() => {
    const factor = microPrice ? 1e-10 : 1;
    const candles = bars.map(bar => ({ ...bar,
      open: bar.open * factor, high: bar.high * factor,
      low: bar.low * factor, close: bar.close * factor,
    }));
    return {
      candles,
      volume: candles.map(bar => ({ time: bar.time, value: 100 + (bar.close / factor) % 300, color: bar.close >= bar.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' })),
      emas: Object.fromEntries([20, 50, 100, 200].map(span => [span, candles.slice(span - 1).map(bar => ({ time: bar.time, value: bar.close - span * factor }))])),
      positions: [{ side: 'LONG', entry_price: 62000 * factor }],
      pending_orders: [{ price: 63500 * factor }],
      risk_lines: [{ type: 'take_profit', price: 66000 * factor }, { type: 'stop_loss', price: 61000 * factor }],
    };
  }, [bars, microPrice]);
  const refresh = () => {
    setTick(n => n + 1);
    setBars(previous => previous.map((bar, index) => index === previous.length - 1
      ? { ...bar, close: bar.close + 1, high: Math.max(bar.high, bar.close + 2) }
      : bar));
  };
  const append = (rolling) => setBars(previous => {
    const index = Math.round((previous.at(-1).time - FIRST_TIME) / 3600) + 1;
    return [...(rolling ? previous.slice(1) : previous), candleAt(index)];
  });
  return <ConfigProvider><PreferencesContext.Provider value={{ isDark: dark, locale: 'zh', t: value => ({ liveWorkspace: '实时工作区', noData: '暂无数据' }[value] || value) }}>
    <main className={WORKSPACE_MODE ? 'boxed-page dashboard-page dashboard-v2' : undefined} style={{ padding: 12, minWidth: 0 }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
      <button onClick={refresh}>Refresh</button>
      <button onClick={() => append(false)}>Append</button>
      <button onClick={() => append(true)}>Rolling</button>
      <button onClick={() => setDark(v => !v)}>Theme</button>
      <button onClick={() => setSymbol(v => v === 'BTC' ? 'ETH' : 'BTC')}>Symbol</button>
      <button onClick={() => setMicroPrice(v => !v)}>Micro price</button>
      <label>周期 <select aria-label="Chart timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)}>
        {chartTimeframeOptions(['15m', '1h', '4h', '1d', '1w', '1M'], timeframe).map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <output data-testid="tick">{tick}</output>
      <output data-testid="bars">{bars.length}</output>
      {WORKSPACE_MODE ? <>
        <button onClick={() => { setDisplayedTimeframe(timeframe); setLoadState('ready'); }}>Complete load</button>
        <button onClick={() => setLoadState('error')}>Fail load</button>
        <button onClick={() => setLoadState('loading')}>Background refresh</button>
      </> : null}
      </div>
      {WORKSPACE_MODE ? <WorkspacePanel
        workspace={{ agent: { config_id: 'chart-fixture', symbol: 'BTC/USDT', mode: 'STRATEGY', content: '' },
          timeframe: displayedTimeframe, market_timeframes: ['15m', '1h', '4h', '1d', '1w', '1M'],
          kline: payload, position: { positions: [] } }}
        timeframe={timeframe}
        setTimeframe={next => { setTimeframe(next); setLoadState('loading'); }}
        authenticated={false}
        chartLoading={loadState === 'loading'}
        chartError={loadState === 'error'}
      /> : <>
      <section data-testid="kline" className="chart-wrap chart-wrap-large" style={{ marginBottom: 20 }}>
        <KlineChart payload={payload} chartKey={`${symbol}:${microPrice}:${timeframe}`} timeframe={timeframe} />
      </section>
      <section data-testid="equity">
        <EquityCompareChart series={[{ config_id: 'test-equity', label: 'Test equity', mode: 'STRATEGY',
          points: Array.from({ length: 20 }, (_, i) => ({ date: `2026-08-${String(i + 1).padStart(2, '0')}`, equity: 1000 + i * 10 + tick })),
          data_state: 'ready', point_count: 20,
        }]} />
      </section>
      <RunScheduleEditor value={rules} onChange={setRules} onPreset={next => { setRules(next); setInterval(30); }} />
      <output data-testid="config" style={{ display: 'block', overflowWrap: 'anywhere' }}>{JSON.stringify({ run_interval: interval, run_schedule: rules })}</output>
      </>}
    </main>
  </PreferencesContext.Provider></ConfigProvider>;
}

createRoot(document.getElementById('root')).render(<Fixture />);
