import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import KlineChart from '../src/components/KlineChart';
import EquityCompareChart from '../src/components/EquityCompareChart';
import RunScheduleEditor from '../src/components/RunScheduleEditor';
import { PreferencesContext } from '../src/app/preferences-context';
import '../src/index.css';

function Fixture() {
  const [tick, setTick] = useState(0);
  const [dark, setDark] = useState(false);
  const [rules, setRules] = useState([]);
  const [interval, setInterval] = useState(60);
  const [symbol, setSymbol] = useState('BTC');
  const candles = Array.from({ length: 80 }, (_, index) => ({
    time: 1788710400 + index * 3600,
    open: 60000 + index * 10, high: 60100 + index * 10 + tick,
    low: 59900 + index * 10, close: 60050 + index * 10 + tick,
  }));
  const payload = {
    candles, volume: candles.map(bar => ({ time: bar.time, value: 100, color: '#16a34a' })),
    emas: { 20: candles.map(bar => ({ time: bar.time, value: bar.close - 20 })) },
    positions: [{ side: 'LONG', entry_price: 60000 }],
    risk_lines: [{ type: 'take_profit', price: 62000 + tick }, { type: 'stop_loss', price: 59000 }],
  };
  return <ConfigProvider><PreferencesContext.Provider value={{ isDark: dark, locale: 'zh', t: value => value }}>
    <main style={{ padding: 12 }}>
      <button onClick={() => setTick(n => n + 1)}>Refresh</button>
      <button onClick={() => setDark(v => !v)}>Theme</button>
      <button onClick={() => setSymbol(v => v === 'BTC' ? 'ETH' : 'BTC')}>Symbol</button>
      <output data-testid="tick">{tick}</output>
      <div style={{ height: 420, marginBottom: 20 }}><KlineChart payload={payload} chartKey={symbol} /></div>
      <section data-testid="equity">
        <EquityCompareChart series={[{ config_id: 'test-equity', label: 'Test equity', mode: 'STRATEGY',
          points: Array.from({ length: 20 }, (_, i) => ({ date: `2026-08-${String(i + 1).padStart(2, '0')}`, equity: 1000 + i * 10 + tick })),
          data_state: 'ready', point_count: 20,
        }]} />
      </section>
      <RunScheduleEditor value={rules} onChange={setRules} onPreset={next => { setRules(next); setInterval(30); }} />
      <output data-testid="config" style={{ display: 'block', overflowWrap: 'anywhere' }}>{JSON.stringify({ run_interval: interval, run_schedule: rules })}</output>
    </main>
  </PreferencesContext.Provider></ConfigProvider>;
}

createRoot(document.getElementById('root')).render(<Fixture />);
