import { BrowserRouter } from 'react-router-dom';
import AgentsPage from '../src/pages/AgentsPage';
import { api } from '../src/lib/api';
import fixture from './position-workbench-data.json';
import { createRoot } from 'react-dom/client';
import { theme } from 'antd';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesProvider } from '../src/app/preferences';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';
import '../src/pages/AgentsPage.css';

const params = new URLSearchParams(location.search);
if (params.get('preview') === '1') api.defaults.adapter = async request => {
  const url = request.url;
  const query = request.params || {};
  let data = {};
  if (url === '/public/dashboard') data = fixture.dashboard;
  else if (url.startsWith('/public/workspace/')) data = fixture[query.section] || fixture.context;
  else if (url.startsWith('/stats/task-history/')) data = fixture.statistics;
  else if (url === '/history/positions') {
    const items = fixture.positions.filter(row => (!query.symbol || row.symbol === query.symbol) && (!query.status || row.status === query.status));
    const page = query.page || 1;
    data = { items: items.slice((page - 1) * 20, page * 20), total: items.length, symbols: fixture.dashboard.agent_summaries[0].symbols, as_of: '2026-11-11T09:30:00+08:00', errors: {} };
  } else if (url.startsWith('/history/positions/')) data = { position: { ...fixture.positions.find(row => row.position_id === url.split('/').at(-1)), events: fixture.events } };
  else if (url === '/history/trade-calls') data = fixture.calls;
  return { data, status: 200, statusText: 'OK', headers: {}, config: request };
};
const zh = params.get('locale') !== 'en';
const dark = params.get('theme') === 'dark';
document.documentElement.dataset.theme = dark ? 'dark' : 'light';
localStorage.setItem('crypto-agent-locale', zh ? 'zh' : 'en');
localStorage.setItem('crypto-agent-theme', dark ? 'dark' : 'light');
function Fixture() {
  return <BrowserRouter><main className="page-wrap"><AgentsPage authenticated={params.get('auth') !== '0'} /></main></BrowserRouter>;
}
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider locale={zh?zhCN:enUS} theme={{algorithm:dark?theme.darkAlgorithm:theme.defaultAlgorithm,token:{borderRadius:8,colorPrimary:'#2563eb',colorBgLayout:dark?'#0b0f17':'#f6f8fb',colorBorderSecondary:dark?'#263142':'#e5e7eb',fontFamily:'var(--font-sans)',fontFamilyCode:'var(--font-mono)'}}}><PreferencesProvider><Fixture /></PreferencesProvider></AccessibleConfigProvider>);
