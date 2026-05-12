import React, { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { ReloadOutlined } from '@ant-design/icons';
import { Button, Space, Spin } from 'antd';
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { usePreferences } from './app/preferences';
import AppTopBar from './components/AppTopBar';
import AuthGate from './components/AuthGate';
import GlobalLoader from './components/GlobalLoader';
import { api, setApiToken } from './lib/api';

const DashboardPage = lazy(() => import('./pages/DashboardPage'));
const PublicUsagePage = lazy(() => import('./pages/PublicPage'));
const HistoryPage = lazy(() => import('./pages/HistoryPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const AdminPage = lazy(() => import('./pages/AdminPage'));
const SetupPage = lazy(() => import('./pages/SetupPage'));

function SuspenseFallback() {
  return (
    <div className="page-wrap">
      <div className="panel-card loading-card">
        <Spin />
      </div>
    </div>
  );
}

function ShellFrame({ children }) {
  return (
    <div className="app-shell">
      {children}
    </div>
  );
}

function PublicShell({ authenticated, onLogout }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = usePreferences();

  const items = useMemo(
    () => [
      { key: '/', label: t('publicDashboard') },
      { key: '/history', label: t('history') },
      { key: '/usage', label: t('usage') },
    ],
    [t],
  );

  const activeKey = location.pathname === '/usage' ? '/usage' : location.pathname === '/history' ? '/history' : '/';

  const dashboardRefresh = activeKey === '/' ? (
    <Button
      size="small"
      icon={<ReloadOutlined />}
      onClick={() => window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'))}
    />
  ) : null;

  const actions = (
    <Space wrap>
      <Button onClick={() => navigate('/console/chat')}>{t('openConsole')}</Button>
      {authenticated ? <Button onClick={onLogout}>{t('logout')}</Button> : null}
    </Space>
  );

  return (
    <ShellFrame>
      <AppTopBar items={items} activeKey={activeKey} onNavigate={navigate} actions={actions} extraActions={dashboardRefresh} />
      <main className="page-wrap">
        <Suspense fallback={<SuspenseFallback />}>
          <Outlet />
        </Suspense>
      </main>
    </ShellFrame>
  );
}

function ConsoleShell({ token, bootstrapping, onLogin, onLogout }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = usePreferences();

  const items = useMemo(
    () => [
      { key: '/console/chat', label: t('chat') },
      { key: '/console/config', label: t('config') },
      { key: '/console/history', label: t('history') },
    ],
    [t],
  );

  const activeKey = useMemo(() => {
    const match = items.find((item) => location.pathname.startsWith(item.key));
    return match?.key || '/console/chat';
  }, [items, location.pathname]);

  const actions = (
    <Space wrap>
      <Button onClick={() => navigate('/')}>{t('backToDashboard')}</Button>
      <Button onClick={onLogout}>{t('logout')}</Button>
    </Space>
  );

  return (
    <AuthGate authenticated={Boolean(token)} loading={bootstrapping} onLogin={onLogin}>
      <ShellFrame>
        <AppTopBar items={items} activeKey={activeKey} onNavigate={navigate} actions={actions} />
        <main className="page-wrap">
          <Suspense fallback={<SuspenseFallback />}>
            <Outlet />
          </Suspense>
        </main>
      </ShellFrame>
    </AuthGate>
  );
}

export default function App() {
  const navigate = useNavigate();
  const [token, setToken] = useState(() => localStorage.getItem('crypto-agent-token') || '');
  const [bootstrapping, setBootstrapping] = useState(true);
  const [setupStatus, setSetupStatus] = useState(null);
  const [setupLoading, setSetupLoading] = useState(true);

  useEffect(() => {
    setApiToken(token);
    if (token) {
      localStorage.setItem('crypto-agent-token', token);
    } else {
      localStorage.removeItem('crypto-agent-token');
    }
  }, [token]);

  useEffect(() => {
    let mounted = true;
    async function loadSetupStatus() {
      try {
        const response = await api.get('/setup/status');
        if (mounted) setSetupStatus(response.data);
      } catch {
        if (mounted) setSetupStatus({ required: false });
      } finally {
        if (mounted) setSetupLoading(false);
      }
    }
    loadSetupStatus();
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    let mounted = true;
    async function bootstrap() {
      if (!token) {
        setBootstrapping(false);
        return;
      }
      try {
        await api.get('/auth/me');
      } catch {
        if (mounted) {
          setToken('');
        }
      } finally {
        if (mounted) {
          setBootstrapping(false);
        }
      }
    }
    bootstrap();
    return () => {
      mounted = false;
    };
  }, [token]);

  const handleLogin = async (password) => {
    const response = await api.post('/auth/login', { password });
    setToken(response.data.token);
    navigate('/console/chat');
  };

  const handleLogout = () => {
    setToken('');
    navigate('/');
  };

  if (setupLoading) {
    return <SuspenseFallback />;
  }

  if (setupStatus?.required) {
    return (
      <Suspense fallback={<SuspenseFallback />}>
        <SetupPage status={setupStatus} onComplete={(next) => setSetupStatus((prev) => ({ ...prev, ...next }))} />
      </Suspense>
    );
  }

  return (
    <>
      <GlobalLoader />
      <Routes>
      <Route path="/public" element={<Navigate to="/" replace />} />
      <Route element={<PublicShell authenticated={Boolean(token)} onLogout={handleLogout} />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/history" element={<HistoryPage />} />
        <Route path="/usage" element={<PublicUsagePage />} />
      </Route>
      <Route
        path="/console"
        element={
          <ConsoleShell
            token={token}
            bootstrapping={bootstrapping}
            onLogin={handleLogin}
            onLogout={handleLogout}
          />
        }
      >
        <Route index element={<Navigate to="/console/chat" replace />} />
        <Route path="chat" element={<ChatPage token={token} />} />
        <Route path="config" element={<AdminPage />} />
        <Route path="history" element={<HistoryPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}
