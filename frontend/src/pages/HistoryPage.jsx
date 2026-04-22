import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Empty, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import { api } from '../lib/api';
import LineChart from '../components/LineChart';

export default function HistoryPage() {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [symbol, setSymbol] = useState('');
  const [configId, setConfigId] = useState('ALL');
  const [page, setPage] = useState(1);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const bootstrap = await api.get('/dashboard/overview');
        const nextSymbol = symbol || bootstrap.data.current_symbol;
        const response = await api.get('/history', {
          params: { symbol: nextSymbol, config_id: configId, page },
        });
        if (!mounted) {
          return;
        }
        setSymbol(nextSymbol);
        setPayload({
          symbols: bootstrap.data.symbols,
          history: response.data,
        });
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load history');
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
  }, [symbol, configId, page]);

  const compareSeries = useMemo(() => {
    const series = payload?.history?.history_compare_series || [];
    return series.map((item) => ({
      name: item.label,
      data: item.points.map((point) => ({ name: point.date, value: point.equity })),
    }));
  }, [payload]);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="glass-card">
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>
              History
            </Typography.Title>
            <Typography.Text type="secondary">查看分析历史、盈亏和权益对比。</Typography.Text>
          </div>
          <Space wrap>
            <Select
              style={{ minWidth: 180 }}
              value={symbol || undefined}
              options={(payload?.symbols || []).map((item) => ({ label: item, value: item }))}
              onChange={(value) => {
                setPage(1);
                setSymbol(value);
              }}
            />
            <Select
              style={{ minWidth: 180 }}
              value={configId}
              options={[
                { label: 'ALL', value: 'ALL' },
                ...((payload?.history?.active_agents || []).map((item) => ({ label: item, value: item })) || []),
              ]}
              onChange={(value) => {
                setPage(1);
                setConfigId(value);
              }}
            />
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="glass-card">
          <Spin />
        </Card>
      ) : null}

      {!loading && payload?.history ? (
        <>
          <div className="metric-grid">
            <Card className="glass-card">
              <Statistic title="Total Trades" value={payload.history.pnl_stats?.total_trades || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Total PnL" value={payload.history.pnl_stats?.total_pnl || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Win Rate" suffix="%" value={payload.history.pnl_stats?.win_rate || 0} />
            </Card>
            <Card className="glass-card">
              <Statistic title="Mode" value={payload.history.agent_mode || '-'} />
            </Card>
          </div>

          <Card className="glass-card" title="Equity Compare">
            <div className="chart-wrap">
              {compareSeries.length ? <LineChart series={compareSeries} yName="Equity" /> : <Empty description="No chart data" />}
            </div>
          </Card>

          <Card className="glass-card" title="Summaries">
            <Table
              size="small"
              rowKey={(row) => row.id}
              dataSource={payload.history.summaries || []}
              locale={{ emptyText: <Empty description="No summaries" /> }}
              columns={[
                { title: 'Time', dataIndex: 'timestamp', width: 160 },
                {
                  title: 'Config',
                  dataIndex: 'config_id',
                  render: (value) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Content',
                  dataIndex: 'content',
                  render: (value) => <Typography.Paragraph style={{ marginBottom: 0 }}>{value}</Typography.Paragraph>,
                },
              ]}
              pagination={{
                current: payload.history.current_page,
                total: payload.history.total_count,
                pageSize: 20,
                onChange: setPage,
                showSizeChanger: false,
              }}
            />
          </Card>
        </>
      ) : null}
    </Space>
  );
}
