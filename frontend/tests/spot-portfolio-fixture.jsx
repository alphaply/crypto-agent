import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import DashboardPage from '../src/pages/DashboardPage';
import { PreferencesContext } from '../src/app/preferences-context';
import 'antd/dist/reset.css';
import '../src/index.css';

function Fixture() {
  const [selectedSymbol, setSelectedSymbol] = useState('BTC/USDT');
  return <AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value, selectedSymbol, setSelectedSymbol }}>
    <DashboardPage />
  </PreferencesContext.Provider></AccessibleConfigProvider>;
}

createRoot(document.getElementById('root')).render(<Fixture />);
