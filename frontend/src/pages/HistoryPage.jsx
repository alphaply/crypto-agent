import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Empty, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import LineChart from '../components/LineChart';
import MarkdownBlock from '../components/MarkdownBlock';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Title, Paragraph } = Typography;

export default function HistoryPage() {
  const { t } = usePreferences();
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [symbol, setSymbol] = useState('');
  const [configId, setConfigId] = useState('ALL');
  const [compareIds, setCompareIds] = useState([]);
  const [page, setPage] = useState(1);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const bootstrap = await api.get('/dashboard/overview', { params: symbol ? { symbol } : {} });
        const nextSymbol = symbol || bootstrap.data.current_symbol;
        const response = await api.get('/history', {
          params: {
            symbol: nextSymbol,
            config_id: configId,
            page,
            compare_ids: compareIds.join(','),
          },
        });
        if (!mounted) {
          return;
        }
        setSymbol(nextSymbol);
        setPayload({
          symbols: bootstrap.data.symbols,
          history: response.data,
        });
        setCompareIds((prev) => {
          const allowed = new Set(response.data.active_agents || []);
          return prev.filter((item) => allowed.has(item));
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
  }, [symbol, configId, compareIds, page]);

  const compareSeries = useMemo(() => {
    const series = payload?.history?.history_compare_series || [];
    return series.map((item) => ({
      name: item.label,
      data: item.points.map((point) => ({ name: point.date, value: point.equity })),
    }));
  }, [payload]);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="hero-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Title level={2} style={{ margin: 0 }}>
              {t('history')}
            </Title>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              Compare equity, review markdown summaries, and inspect symbol-level trade history.
            </Paragraph>
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
            <Select
              mode="multiple"
              style={{ minWidth: 260 }}
              value={compareIds}
              options={((payload?.history?.active_agents || []).map((item) => ({ label: item, value: item })) || [])}
              onChange={setCompareIds}
              placeholder={t('compare')}
            />
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : null}

      {!loading && payload?.history ? (
        <>
          <div className="metric-grid">
            <Card className="panel-card metric-card">
              <Statistic title={t('totalTrades')} value={payload.history.pnl_stats?.total_trades || 0} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('totalPnl')} value={payload.history.pnl_stats?.total_pnl || 0} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('winRate')} suffix="%" value={payload.history.pnl_stats?.win_rate || 0} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('symbolMode')} value={payload.history.agent_mode || '-'} />
            </Card>
          </div>

          <Card className="panel-card" title={t('equityCompare')}>
            <div className="chart-wrap">
              {compareSeries.length ? <LineChart series={compareSeries} yName="Equity" /> : <Empty description={t('noData')} />}
            </div>
          </Card>

          <Card className="panel-card" title={t('dailySummaries')}>
            <Table
              size="small"
              rowKey={(row) => row.id}
              dataSource={payload.history.summaries || []}
              locale={{ emptyText: <Empty description={t('emptyHistory')} /> }}
              pagination={{
                current: payload.history.current_page,
                total: payload.history.total_count,
                pageSize: 20,
                onChange: setPage,
                showSizeChanger: false,
              }}
              expandable={{
                expandedRowRender: (record) => <MarkdownBlock content={record.content || ''} />,
              }}
              scroll={{ x: 960 }}
              columns={[
                { title: 'Time', dataIndex: 'timestamp', width: 180 },
                {
                  title: 'Config',
                  dataIndex: 'config_id',
                  render: (value) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Content',
                  dataIndex: 'content',
                  render: (value) => (
                    <div className="history-snippet">
                      <MarkdownBlock content={String(value || '').slice(0, 280)} />
                    </div>
                  ),
                },
              ]}
            />
          </Card>
        </>
      ) : null}
    </Space>
  );
}
