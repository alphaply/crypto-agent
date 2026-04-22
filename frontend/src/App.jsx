import React, { useEffect, useMemo, useState } from 'react';
import { Button, Layout, Menu, Space, Typography } from 'antd';
import { BarChartOutlined, CommentOutlined, DashboardOutlined, SettingOutlined, UnlockOutlined } from '@ant-design/icons';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import AuthGate from './components/AuthGate';
import DashboardPage from './pages/DashboardPage';
import HistoryPage from './pages/HistoryPage';
import ChatPage from './pages/ChatPage';
import AdminPage from './pages/AdminPage';
import PublicPage from './pages/PublicPage';
import { api, setApiToken } from './lib/api';

const { Header, Content, Sider } = Layout;

const APP_ROUTES = [
  { key: '/', label: 'Dashboard', icon: <DashboardOutlined /> },
  { key: '/history', label: 'History', icon: <BarChartOutlined /> },
  { key: '/chat', label: 'Chat', icon: <CommentOutlined /> },
  { key: '/admin', label: 'Admin', icon: <SettingOutlined /> },
  { key: '/public', label: 'Public', icon: <UnlockOutlined /> },
];

function ProtectedRoutes({ token }) {
  return (
    <Routes>
      <Route path="/" element={<DashboardPage />} />
      <Route path="/history" element={<HistoryPage />} />
      <Route path="/chat" element={<ChatPage token={token} />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function ProtectedShell({ token, bootstrapping, onLogin, onLogout }) {
  const location = useLocation();
  const navigate = useNavigate();

  const selectedKey = useMemo(() => {
    const match = APP_ROUTES.filter((item) => item.key !== '/public').find(
      (item) => location.pathname === item.key || location.pathname.startsWith(`${item.key}/`),
    );
    return match?.key || '/';
  }, [location.pathname]);

  return (
    <AuthGate authenticated={Boolean(token)} loading={bootstrapping} onLogin={onLogin}>
      <Layout className="app-shell">
        <Sider width={240} theme="light" breakpoint="lg" collapsedWidth="0">
          <div style={{ padding: 24 }}>
            <Typography.Title level={4} style={{ margin: 0 }}>
              Crypto Agent
            </Typography.Title>
            <Typography.Text type="secondary">FastAPI + React</Typography.Text>
          </div>
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            items={APP_ROUTES.filter((item) => item.key !== '/public').map((item) => ({
              ...item,
              onClick: () => navigate(item.key),
            }))}
          />
        </Sider>
        <Layout>
          <Header style={{ background: 'transparent', padding: '0 24px' }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Typography.Title level={3} style={{ margin: 0 }}>
                Web Console
              </Typography.Title>
              <Space>
                <Button onClick={() => navigate('/public')}>Public</Button>
                <Button onClick={onLogout}>Logout</Button>
              </Space>
            </Space>
          </Header>
          <Content className="page-wrap">
            <ProtectedRoutes token={token} />
          </Content>
        </Layout>
      </Layout>
    </AuthGate>
  );
}

export default function App() {
  const navigate = useNavigate();
  const [token, setToken] = useState(() => localStorage.getItem('crypto-agent-token') || '');
  const [bootstrapping, setBootstrapping] = useState(true);

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
    async function bootstrap() {
      if (!token) {
        setBootstrapping(false);
        return;
      }
      try {
        await api.get('/auth/me');
      } catch (_) {
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
    navigate('/');
  };

  const handleLogout = () => {
    setToken('');
    navigate('/');
  };

  return (
    <Routes>
      <Route
        path="/public"
        element={
          <div className="page-wrap">
            <PublicPage />
          </div>
        }
      />
      <Route
        path="*"
        element={
          <ProtectedShell
            token={token}
            bootstrapping={bootstrapping}
            onLogin={handleLogin}
            onLogout={handleLogout}
          />
        }
      />
    </Routes>
  );
}
