import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';

export function useDashboardSnapshot() {
  const [dashboard, setDashboard] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [marketRefresh, setMarketRefresh] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    window.addEventListener('crypto-agent-dashboard-refresh', refresh);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', refresh);
  }, [refresh]);

  useEffect(() => {
    let active = true;
    let pending = false;
    let polls = 0;
    const controller = new AbortController();
    async function load(initial = false) {
      if (pending || (!initial && document.hidden)) return;
      pending = true;
      if (initial) setLoading(true);
      try {
        const { data } = await api.get('/public/dashboard', { signal: controller.signal, timeout: 25000, silent: !initial });
        if (!active) return;
        setDashboard(data);
        setError('');
        if (!initial && ++polls % 4 === 0) setMarketRefresh((value) => value + 1);
      } catch (err) {
        if (active) setError(err.message || 'Failed to refresh dashboard');
      } finally {
        pending = false;
        if (active && initial) setLoading(false);
      }
    }
    load(true);
    const timer = window.setInterval(() => load(), 8000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [revision]);

  return { dashboard, loading, error, refresh, revision, marketRefresh };
}
