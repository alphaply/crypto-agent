import React from 'react';
import { createRoot } from 'react-dom/client';
import { PreferencesContext } from '../src/app/preferences-context';
import DecisionMessages from '../src/components/DecisionMessages';
import 'antd/dist/reset.css';
import '../src/index.css';
const locale = new URLSearchParams(location.search).get('locale') || 'zh';
const messages = [
  { role: 'tool', tool: 'execute_trade_actions', result: { status: 'submitted', results: [{ action: 'open', status: 'submitted', symbol: 'BTC/USDT', result: { side: 'LONG', amount: 0.01, price: 68000, filled_amount: 0, order_id: 'private-order-detail-only', nested: { receipt_version: 3 } } }] } },
  { role: 'tool', tool: 'execute_trade_actions', result: { status: 'failed', results: [{ status: 'completed', symbol: 'ETH/USDT' }, { status: 'failed', error: 'Insufficient available cash' }] } },
  { role: 'tool', tool: 'get_news', result: 'News returned. '.repeat(100) },
];
createRoot(document.getElementById('root')).render(<PreferencesContext.Provider value={{ locale, isDark: false, t: (v) => v }}><main style={{ padding: 20, maxWidth: 800, margin: 'auto' }}><DecisionMessages messages={messages} zh={locale === 'zh'} /></main></PreferencesContext.Provider>);
