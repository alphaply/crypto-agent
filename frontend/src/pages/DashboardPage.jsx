import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Select,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import MarkdownBlock from '../components/MarkdownBlock';
import KlineChart from '../components/KlineChart';
import LineChart from '../components/LineChart';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Text, Title, Paragraph } = Typography;

function MetricGrid({ items }) {
  return (
    <div className="metric-grid">
      {items.map((item) => (
        <Card key={item.title} className="panel-card metric-card">
          <Statistic title={item.title} value={item.value} suffix={item.suffix} precision={item.precision} />
        </Card>
      ))}
    </div>
  );
}

function WorkspaceCard({ workspace }) {
  const { t } = usePreferences();
  const agent = workspace?.agent;
  const position = workspace?.position || {};
  const kline = workspace?.kline || {};
  const dailySummaries = workspace?.daily_summaries?.daily_summaries || [];
  const recentOrders = workspace?.orders?.orders || [];
  const pendingOrders = kline?.pending_orders || [];

  if (!agent) {
    return (
      <Card className="panel-card">
        <Spin />
      </Card>
    );
  }

  return (
    <Card
      className="panel-card workspace-card"
      title={
        <Space wrap>
          <Text strong>{agent.display_name}</Text>
          <Tag color={agent.enabled ? 'green' : 'default'}>{agent.mode}</Tag>
          <Tag color="blue">{agent.model}</Tag>
          <Text type="secondary">{agent.timestamp}</Text>
        </Space>
      }
    >
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }} bordered>
          <Descriptions.Item label="Config ID">{agent.config_id}</Descriptions.Item>
          <Descriptions.Item label={t('symbol')}>{agent.symbol}</Descriptions.Item>
          <Descriptions.Item label="Next Run">{agent.next_run || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('symbolFreq')}>{agent.freq || '-'}</Descriptions.Item>
          <Descriptions.Item label="Leverage">{agent.leverage || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('summary')}>{agent.strategy_logic ? 'Markdown' : '-'}</Descriptions.Item>
          <Descriptions.Item label={t('positions')}>
            {position.positions?.length || 0}
          </Descriptions.Item>
          <Descriptions.Item label={t('pendingOrders')}>
            {pendingOrders.length}
          </Descriptions.Item>
        </Descriptions>

        {agent.dca_stats ? (
          <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }} bordered>
            <Descriptions.Item label="Invested">{agent.dca_stats.total_invested}</Descriptions.Item>
            <Descriptions.Item label="Qty">{agent.dca_stats.total_qty}</Descriptions.Item>
            <Descriptions.Item label="Avg Cost">{agent.dca_stats.avg_cost}</Descriptions.Item>
            <Descriptions.Item label="Buy Count">{agent.dca_stats.buy_count}</Descriptions.Item>
          </Descriptions>
        ) : null}

        <Card className="subtle-card" title={t('liveWorkspace')}>
          <div className="chart-wrap">
            <KlineChart payload={kline} />
          </div>
        </Card>

        <div className="workspace-grid">
          <Card className="subtle-card" title={t('analysis')}>
            <MarkdownBlock content={agent.content || ''} />
            {agent.strategy_logic ? (
              <Collapse
                ghost
                items={[
                  {
                    key: 'strategy',
                    label: t('strategyLogic'),
                    children: <MarkdownBlock content={agent.strategy_logic} />,
                  },
                ]}
              />
            ) : null}
          </Card>

          <Card className="subtle-card" title={t('positions')}>
            <Table
              size="small"
              rowKey={(row) => `${row.side}-${row.entry_price}-${row.qty || row.amount || row.contracts}`}
              dataSource={position.positions || []}
              pagination={false}
              scroll={{ x: 720 }}
              locale={{ emptyText: <Empty description={t('noActivePositions')} /> }}
              columns={[
                { title: 'Side', dataIndex: 'side' },
                { title: 'Entry', dataIndex: 'entry_price' },
                { title: 'Qty', dataIndex: 'qty' },
                { title: 'PnL', dataIndex: 'unrealized_pnl' },
                { title: 'ROI %', dataIndex: 'roi_pct' },
                { title: 'Leverage', dataIndex: 'leverage' },
              ]}
            />
            {position.recent_trades?.length ? (
              <>
                <Paragraph className="section-caption">{t('recentTrades')}</Paragraph>
                <Table
                  size="small"
                  rowKey={(row) => `${row.time}-${row.side}-${row.price}`}
                  dataSource={position.recent_trades}
                  pagination={false}
                  scroll={{ x: 720 }}
                  columns={[
                    { title: 'Time', dataIndex: 'time' },
                    { title: 'Side', dataIndex: 'side' },
                    { title: 'Entry', dataIndex: 'entry_price' },
                    { title: 'Exit', dataIndex: 'price' },
                    { title: 'PnL', dataIndex: 'pnl' },
                  ]}
                />
              </>
            ) : null}
          </Card>

          <Card className="subtle-card" title={t('pendingOrders')}>
            <Table
              size="small"
              rowKey={(row) => row.order_id || `${row.side}-${row.price}-${row.amount}`}
              dataSource={pendingOrders}
              pagination={false}
              scroll={{ x: 680 }}
              locale={{ emptyText: <Empty description={t('noOpenOrders')} /> }}
              columns={[
                { title: 'Order ID', dataIndex: 'order_id' },
                { title: 'Side', dataIndex: 'side' },
                { title: 'Type', dataIndex: 'type' },
                { title: 'Price', dataIndex: 'price' },
                { title: 'Amount', dataIndex: 'amount' },
              ]}
            />
          </Card>

          <Card className="subtle-card" title={t('recentOrders')}>
            <Table
              size="small"
              rowKey={(row) => row.id || row.order_id || `${row.timestamp}-${row.side}`}
              dataSource={recentOrders}
              pagination={false}
              scroll={{ x: 760 }}
              columns={[
                { title: 'Time', dataIndex: 'timestamp' },
                { title: 'Side', dataIndex: 'side' },
                { title: 'Entry', dataIndex: 'entry_price' },
                { title: 'Amount', dataIndex: 'amount' },
                { title: 'Status', dataIndex: 'status' },
                { title: 'Reason', dataIndex: 'reason' },
              ]}
            />
          </Card>
        </div>

        <Card className="subtle-card" title={t('dailySummaries')}>
          {dailySummaries.length ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              {dailySummaries.map((summary) => (
                <Card key={`${summary.date || summary.timestamp}-${summary.config_id}`} className="summary-snippet">
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    <Text strong>{summary.date || summary.timestamp}</Text>
                    <MarkdownBlock content={summary.summary || summary.content || ''} />
                  </Space>
                </Card>
              ))}
            </Space>
          ) : (
            <Empty description={t('noSummaries')} />
          )}
        </Card>
      </Space>
    </Card>
  );
}

export default function DashboardPage() {
  const { t } = usePreferences();
  const [symbol, setSymbol] = useState('');
  const [timeframe, setTimeframe] = useState('1h');
  const [dashboard, setDashboard] = useState(null);
  const [usage, setUsage] = useState(null);
  const [compareIds, setCompareIds] = useState([]);
  const [comparePayload, setComparePayload] = useState(null);
  const [workspaceMap, setWorkspaceMap] = useState({});
  const [loading, setLoading] = useState(true);
  const [compareLoading, setCompareLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    async function loadDashboard() {
      setLoading(true);
      setError('');
      try {
        const [dashboardResponse, usageResponse] = await Promise.all([
          api.get('/public/dashboard', { params: symbol ? { symbol } : {} }),
          api.get('/public/usage'),
        ]);
        if (!mounted) {
          return;
        }
        setDashboard(dashboardResponse.data);
        setUsage(usageResponse.data);
        if (!symbol && dashboardResponse.data.current_symbol) {
          setSymbol(dashboardResponse.data.current_symbol);
        }
        setCompareIds((prev) => {
          const allowed = new Set(
            (dashboardResponse.data.compare_candidates || []).map((item) => item.config_id),
          );
          const filtered = prev.filter((item) => allowed.has(item));
          return filtered.length ? filtered : dashboardResponse.data.default_compare_ids || [];
        });
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load dashboard');
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    loadDashboard();
    return () => {
      mounted = false;
    };
  }, [symbol]);

  useEffect(() => {
    let mounted = true;
    async function loadCompare() {
      const currentSymbol = dashboard?.current_symbol || symbol;
      if (!currentSymbol) {
        return;
      }
      setCompareLoading(true);
      try {
        const response = await api.get('/public/compare', {
          params: {
            symbol: currentSymbol,
            config_ids: compareIds.join(','),
          },
        });
        if (mounted) {
          setComparePayload(response.data);
        }
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load compare data');
        }
      } finally {
        if (mounted) {
          setCompareLoading(false);
        }
      }
    }
    loadCompare();
    return () => {
      mounted = false;
    };
  }, [compareIds, dashboard?.current_symbol, symbol]);

  useEffect(() => {
    let mounted = true;
    async function loadWorkspaces() {
      const configs = dashboard?.agent_summaries || [];
      if (!configs.length) {
        setWorkspaceMap({});
        return;
      }

      setWorkspaceLoading(true);
      try {
        const responses = await Promise.all(
          configs.map((item) =>
            api.get(`/public/workspace/${item.config_id}`, {
              params: { timeframe },
            }),
          ),
        );
        if (!mounted) {
          return;
        }
        const nextMap = {};
        responses.forEach((response) => {
          nextMap[response.data.agent.config_id] = response.data;
        });
        setWorkspaceMap(nextMap);
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load workspace data');
        }
      } finally {
        if (mounted) {
          setWorkspaceLoading(false);
        }
      }
    }
    loadWorkspaces();
    return () => {
      mounted = false;
    };
  }, [dashboard?.agent_summaries, timeframe]);

  const summaryCards = useMemo(() => {
    if (!dashboard) {
      return [];
    }
    const usageSummary = usage?.summary || dashboard.usage_summary || {};
    return [
      { title: t('scheduler'), value: dashboard.scheduler_enabled ? 'On' : 'Off' },
      { title: t('symbolMode'), value: dashboard.symbol_mode || '-' },
      { title: t('symbolFreq'), value: dashboard.symbol_freq || '-' },
      { title: t('agents'), value: dashboard.agent_summaries?.length || 0 },
      { title: t('totalTokens'), value: usageSummary.total_tokens_14d || 0 },
      { title: t('totalCost'), value: usageSummary.total_cost || 0, precision: 4 },
    ];
  }, [dashboard, t, usage]);

  const compareSeries = useMemo(() => {
    const series = comparePayload?.series || [];
    return series.map((item) => ({
      name: item.label,
      data: item.points.map((point) => ({ name: point.date, value: point.equity })),
    }));
  }, [comparePayload]);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="hero-card">
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }} wrap>
            <div>
              <Title level={2} style={{ margin: 0 }}>
                {t('publicHeadline')}
              </Title>
              <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                {t('publicSubhead')}
              </Paragraph>
            </div>
            <Space wrap>
              <Select
                style={{ minWidth: 180 }}
                value={dashboard?.current_symbol || symbol || undefined}
                options={(dashboard?.symbols || []).map((item) => ({ label: item, value: item }))}
                onChange={setSymbol}
                placeholder={t('symbol')}
              />
              <Select
                mode="multiple"
                style={{ minWidth: 260 }}
                value={compareIds}
                options={(dashboard?.compare_candidates || []).map((item) => ({
                  label: `${item.display_name} / ${item.model}`,
                  value: item.config_id,
                }))}
                onChange={setCompareIds}
                placeholder={t('compare')}
              />
              <Segmented
                value={timeframe}
                onChange={setTimeframe}
                options={['15m', '1h', '4h', '1d']}
              />
            </Space>
          </Space>

          <MetricGrid items={summaryCards} />

          {error ? <Alert type="error" message={error} showIcon /> : null}
        </Space>
      </Card>

      {loading ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : null}

      {!loading ? (
        <Card className="panel-card" title={t('equityCompare')} extra={compareLoading ? <Spin size="small" /> : null}>
          <div className="chart-wrap">
            {compareSeries.length ? <LineChart series={compareSeries} yName="Equity" /> : <Empty description={t('noData')} />}
          </div>
        </Card>
      ) : null}

      {!loading && !(dashboard?.agent_summaries || []).length ? (
        <Card className="panel-card">
          <Empty description={t('emptyWorkspace')} />
        </Card>
      ) : null}

      {!loading && workspaceLoading && !(dashboard?.agent_summaries || []).length ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : null}

      {!loading &&
        (dashboard?.agent_summaries || []).map((agent) => (
          <WorkspaceCard key={agent.config_id} workspace={workspaceMap[agent.config_id]} />
        ))}
    </Space>
  );
}
