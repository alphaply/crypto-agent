import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import AdminPage from '../src/pages/AdminPage';
import ExitManagementPanel from '../src/components/ExitManagementPanel';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

localStorage.setItem('crypto-agent-admin-active-tab', 'tasks');
createRoot(document.getElementById('root')).render(
  <AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
    <main style={{ padding: 16, minWidth: 0 }}><AdminPage />
      <section data-testid="independent-exits-v2" style={{ marginTop: 24 }}>
        <ExitManagementPanel management={{ mode: 'independent_exits', pending: false, quantity_conflict: true, uncovered: { LONG: 0, SHORT: .1 }, exits: [
          { order_id: 'regular:101', contract_version: 2, order_type: 'limit', pos_side: 'LONG', price: 3100, amount: .2, remaining: .2, filled: 0, status: 'open', coverage_estimate: .2 },
          { order_id: 'algo:102', contract_version: 2, order_type: 'stop_limit', pos_side: 'LONG', trigger_price: 2800, price: 2790, amount: .3, remaining: .2, filled: .1, status: 'triggered', coverage_estimate: .2 },
          { order_id: 'algo:103', contract_version: 2, order_type: 'take_profit_market', pos_side: 'LONG', trigger_price: 3300, quantity_mode: 'all', amount: null, remaining: null, filled: 0, status: 'waiting_trigger', coverage_estimate: .5 },
        ] }} />
      </section>
    </main>
  </PreferencesContext.Provider></AccessibleConfigProvider>,
);
