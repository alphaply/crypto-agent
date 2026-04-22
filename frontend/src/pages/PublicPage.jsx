import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Empty, Select, Space, Spin, Statistic, Typography } from 'antd';
import { api } from '../lib/api';
import LineChart from '../components/LineChart';

export default function PublicPage() {
  const [symbols, setSymbols] = useState([]);
  const [symbol, setSymbol] = useState('');
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const bootstrap = await api.get('/public/bootstrap');
        const nextSymbol = symbol || bootstrap.data.current_symbol;
        const stats = await api.get('/stats/financial', { params: { symbol: nextSymbol } });
        if (!mounted) {
          return;
        }
        setSymbols(bootstrap.data.symbols || []);
        setSymbol(nextSymbol);
        setPayload(stats.data);
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load public stats');
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      mounted = false;
    };
  }, [symbol]);

  const series = useMemo(
    () => [
      {
        name: 'Daily Equity',
        data: (payload?.daily_equity || []).map((item) => ({ name: item.day, value: item.total_equity })),
      },
    ],
    [payload],
  );

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="glass-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>
              Public Stats
            </Typography.Title>
            <Typography.Text type="secondary">公开查看累计权益与交易统计。</Typography.Text>
          </div>
          <Select
            style={{ minWidth: 220 }}
            value={symbol || undefined}
            options={symbols.map((item) => ({ label: item, value: item }))}
            onChange={setSymbol}
          />
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="glass-card">
          <Spin />
        </Card>
      ) : null}

      {!loading && payload ? (
        <>
          <div className="metric-grid">
            <Card className="glass-card">
              <Statistic title="Total Trades" value={payload.summary?.total_trades || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Total PnL" value={payload.summary?.total_pnl || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Win Rate" suffix="%" value={payload.summary?.win_rate || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Latest Equity" value={payload.summary?.latest_equity || 0} />
            </Card>
          </div>
          <Card className="glass-card" title="Daily Equity Curve">
            <div className="chart-wrap">
              {payload.daily_equity?.length ? <LineChart series={series} yName="Equity" /> : <Empty description="No public data yet" />}
            </div>
          </Card>
        </>
      ) : null}
    </Space>
  );
}
