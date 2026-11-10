import { createRoot } from 'react-dom/client';
import { PreferencesProvider } from '../src/app/preferences';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { TaskStatistics, TradeCallHistory } from '../src/components/TaskHistory';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';

localStorage.setItem('crypto-agent-locale', 'zh');
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider><PreferencesProvider>
  <main style={{ padding: 16, maxWidth: 1400, margin: 'auto', display: 'grid', gap: 16 }}>
    <TaskStatistics configId="fixture" /><TradeCallHistory configId="fixture" />
  </main>
</PreferencesProvider></AccessibleConfigProvider>);
