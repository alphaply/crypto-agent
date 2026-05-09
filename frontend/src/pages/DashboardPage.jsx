import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import dayjs from 'dayjs';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock, { splitThinkingContent } from '../components/ReasoningBlock';
import KlineChart from '../components/KlineChart';
import LineChart from '../components/LineChart';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Text, Title, Paragraph } = Typography;
const { TextArea } = Input;

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

function CopyNumber({ value }) {
  const { t } = usePreferences();
  if (value === null || value === undefined || value === '') return '-';
  return (
    <button
      type="button"
      className="copy-number"
      onClick={() => {
        navigator.clipboard?.writeText(String(value));
        message.success(t('copied'));
      }}
    >
      {value}
    </button>
  );
}

function ComparePanel({ dashboard, compareSeries, loading }) {
  const { t } = usePreferences();
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card" title={t('equityCompare')} extra={loading ? <Spin size="small" /> : null}>
        <div className="chart-wrap">
          {compareSeries.length ? <LineChart series={compareSeries} yName="Equity" /> : <Empty description={t('noData')} />}
        </div>
      </Card>
      <Card className="panel-card" title={t('compareView')}>
        <Table
          size="small"
          rowKey="config_id"
          dataSource={dashboard?.compare_rows || []}
          pagination={false}
          scroll={{ x: 980 }}
          columns={[
            { title: t('configColumn'), dataIndex: 'display_name', render: (value, row) => <Tag color="blue">{value || row.config_id}</Tag> },
            { title: t('modeColumn'), dataIndex: 'mode' },
            { title: t('modelColumn'), dataIndex: 'model' },
            { title: t('longColumn'), dataIndex: 'long_count' },
            { title: t('shortColumn'), dataIndex: 'short_count' },
            { title: t('longShortRatio'), dataIndex: 'long_short_ratio' },
            { title: t('closeColumn'), dataIndex: 'close_count' },
            { title: t('openTotalColumn'), dataIndex: 'total_orders' },
            { title: t('cancelColumn'), dataIndex: 'cancel_count' },
            { title: t('winRate'), dataIndex: 'win_rate', render: (value) => `${value || 0}%` },
            { title: t('totalPnl'), dataIndex: 'total_pnl' },
          ]}
        />
      </Card>
    </Space>
  );
}

function WorkspacePanel({ workspace, timeframe, setTimeframe }) {
  const { t } = usePreferences();
  const agent = workspace?.agent;
  const position = workspace?.position || {};
  const kline = workspace?.kline || {};
  const dailySummaries = workspace?.daily_summaries?.daily_summaries || [];
  const shortMemories = workspace?.short_memories?.short_memories || [];
  const recentOrders = workspace?.orders?.orders || [];
  const pendingOrders = kline?.pending_orders || [];
  const timeframeOptions = workspace?.market_timeframes || [];

  if (!agent) {
    return (
      <Card className="panel-card loading-card">
        <Spin />
      </Card>
    );
  }

  const positionSummary = position.summary || {};

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card">
        <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }} bordered>
          <Descriptions.Item label={t('configIdColumn')}>{agent.config_id}</Descriptions.Item>
          <Descriptions.Item label={t('modeColumn')}>
            <Tag color={agent.enabled ? 'blue' : 'default'}>{agent.mode}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t('modelColumn')}>{agent.model}</Descriptions.Item>
          <Descriptions.Item label={t('executedAt')}>{agent.timestamp || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('nextRun')}>{agent.next_run || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('symbolFreq')}>{agent.freq || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('positions')}>{position.positions?.length || 0}</Descriptions.Item>
          <Descriptions.Item label={t('pendingOrders')}>{pendingOrders.length}</Descriptions.Item>
          <Descriptions.Item label={t('winRate')}>{positionSummary.win_rate ?? 0}%</Descriptions.Item>
          <Descriptions.Item label={t('totalPnl')}>{positionSummary.realized_pnl ?? 0}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card
        className="panel-card"
        title={t('liveWorkspace')}
        extra={<Segmented value={timeframe} onChange={setTimeframe} options={timeframeOptions.length ? timeframeOptions : ['15m', '30m', '1h', '4h', '1d', '1w', '1M']} />}
      >
        <div className="chart-wrap chart-wrap-large">
          <KlineChart payload={kline} />
        </div>
      </Card>

      <div className="analysis-grid">
        <Card className="panel-card" title={t('analysis')}>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label={t('executedAt')}>{agent.timestamp || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('nextRun')}>{agent.next_run || '-'}</Descriptions.Item>
            </Descriptions>
            {(() => {
              const normalized = splitThinkingContent(agent.content || '', agent.reasoning_content || '');
              return (
                <>
                  <MarkdownBlock content={normalized.content || ''} />
                  <ReasoningBlock title={t('reasoning')} content={normalized.reasoning} />
                </>
              );
            })()}
            {agent.strategy_logic ? (
              <div className="strategy-block">
                <Text strong>{t('strategyLogic')}</Text>
                <MarkdownBlock content={agent.strategy_logic} />
              </div>
            ) : null}
          </Space>
        </Card>

        <Card className="panel-card" title={t('recentOrders')}>
          <Table
            size="small"
            rowKey={(row) => row.id || row.order_id || `${row.timestamp}-${row.side}`}
            dataSource={recentOrders}
            pagination={false}
            scroll={{ x: 820 }}
            columns={[
              { title: 'Time', dataIndex: 'timestamp', width: 160 },
              { title: 'Action', dataIndex: 'action_label', render: (value) => <Tag color="blue">{value}</Tag> },
              { title: 'Entry', dataIndex: 'entry_price', render: (value) => <CopyNumber value={value} /> },
              { title: 'Amount', dataIndex: 'amount', render: (value) => <CopyNumber value={value} /> },
              { title: 'TP', dataIndex: 'take_profit', render: (value) => <CopyNumber value={value} /> },
              { title: 'SL', dataIndex: 'stop_loss', render: (value) => <CopyNumber value={value} /> },
              { title: 'Status', dataIndex: 'status' },
              { title: 'Note', dataIndex: 'strategy_note' },
              { title: 'Reason', dataIndex: 'reason', ellipsis: true },
            ]}
          />
        </Card>
      </div>

      <Card className="panel-card" title={t('shortMemories')}>
        {shortMemories.length ? (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {shortMemories.map((memory) => (
              <Card key={`${memory.config_id}-${memory.bucket_start}`} className="summary-snippet">
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Text strong>{memory.bucket_start} - {memory.bucket_end}</Text>
                  <MarkdownBlock content={memory.market_summary || ''} />
                  {memory.position_summary ? <Text type="secondary">{memory.position_summary}</Text> : null}
                </Space>
              </Card>
            ))}
          </Space>
        ) : (
          <Empty description={t('noData')} />
        )}
      </Card>

      <div className="workspace-grid">
        <Card className="panel-card" title={t('positions')}>
          <Table
            size="small"
            rowKey={(row) => `${row.side}-${row.entry_price}-${row.qty || row.amount || row.contracts}`}
            dataSource={position.positions || []}
            pagination={false}
            scroll={{ x: 760 }}
            locale={{ emptyText: <Empty description={t('noActivePositions')} /> }}
            columns={[
              { title: 'Side', dataIndex: 'side' },
              { title: 'Entry', dataIndex: 'entry_price', render: (value) => <CopyNumber value={value} /> },
              { title: 'Mark', dataIndex: 'mark_price', render: (value) => <CopyNumber value={value} /> },
              { title: 'Qty', dataIndex: 'qty', render: (_, row) => <CopyNumber value={row.qty || row.amount || row.contracts} /> },
              { title: 'PnL', dataIndex: 'unrealized_pnl' },
              { title: 'ROI %', dataIndex: 'roi_pct' },
              { title: 'Lev', dataIndex: 'leverage' },
            ]}
          />
        </Card>

        <Card className="panel-card" title={t('pendingOrders')}>
          <Table
            size="small"
            rowKey={(row) => row.order_id || `${row.side}-${row.price}-${row.amount}`}
            dataSource={pendingOrders}
            pagination={false}
            scroll={{ x: 680 }}
            locale={{ emptyText: <Empty description={t('noOpenOrders')} /> }}
            columns={[
              { title: 'Side', dataIndex: 'side' },
              { title: 'Type', dataIndex: 'type' },
              { title: 'Price', dataIndex: 'price', render: (value) => <CopyNumber value={value} /> },
              { title: 'Amount', dataIndex: 'amount', render: (value) => <CopyNumber value={value} /> },
              { title: 'Order ID', dataIndex: 'order_id', ellipsis: true },
            ]}
          />
        </Card>
      </div>

      <Card className="panel-card" title={t('dailySummaries')}>
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
  );
}

function DailySummaryPanel({ dashboard, authenticated }) {
  const { t } = usePreferences();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState({ symbol: '', config_id: 'ALL', days: 30 });
  const [modalOpen, setModalOpen] = useState(false);
  const [editingRow, setEditingRow] = useState(null);
  const [form] = Form.useForm();

  const configOptions = useMemo(
    () => [
      { label: 'ALL', value: 'ALL' },
      ...((dashboard?.agent_summaries || []).map((agent) => ({ label: agent.config_id, value: agent.config_id }))),
    ],
    [dashboard],
  );

  const loadRows = async (nextFilter = filter) => {
    const symbol = nextFilter.symbol || dashboard?.current_symbol;
    if (!symbol) return;
    setLoading(true);
    try {
      const response = await api.get('/public/daily-summaries', {
        params: {
          symbol,
          config_id: nextFilter.config_id || 'ALL',
          days: nextFilter.days || undefined,
          limit: 200,
        },
      });
      setRows(response.data.daily_summaries || []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const next = { ...filter, symbol: filter.symbol || dashboard?.current_symbol || '' };
    const timer = window.setTimeout(() => {
      loadRows(next);
    }, 0);
    return () => window.clearTimeout(timer);
    // Refresh when the selected dashboard symbol changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboard?.current_symbol]);

  const openEditor = (row = null) => {
    setEditingRow(row);
    form.setFieldsValue({
      date: row?.date ? dayjs(row.date) : dayjs(),
      config_id: row?.config_id || (dashboard?.agent_summaries || [])[0]?.config_id || '',
      summary: row?.summary || '',
    });
    setModalOpen(true);
  };

  const saveSummary = async () => {
    const values = await form.validateFields();
    await api.put('/history/daily-summaries', {
      date: values.date.format('YYYY-MM-DD'),
      config_id: values.config_id,
      summary: values.summary || '',
    });
    setModalOpen(false);
    await loadRows();
    message.success(t('saved'));
  };

  const generateSummary = async () => {
    const values = await form.validateFields(['date', 'config_id']);
    await api.post('/history/daily-summaries/generate', {
      date: values.date.format('YYYY-MM-DD'),
      config_id: values.config_id,
    });
    setModalOpen(false);
    await loadRows();
    message.success(t('saved'));
  };

  const deleteSummary = async (row) => {
    await api.delete('/history/daily-summaries', { data: { date: row.date, config_id: row.config_id } });
    await loadRows();
    message.success(t('deleted'));
  };

  return (
    <Card
      className="panel-card"
      title={t('dailySummaries')}
      extra={authenticated ? <Button type="primary" onClick={() => openEditor()}>{t('addDailySummary')}</Button> : null}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space wrap>
          <Select
            style={{ minWidth: 180 }}
            value={filter.symbol || dashboard?.current_symbol || undefined}
            options={(dashboard?.symbols || []).map((item) => ({ label: item, value: item }))}
            onChange={(value) => {
              const next = { ...filter, symbol: value };
              setFilter(next);
              loadRows(next);
            }}
          />
          <Select
            style={{ minWidth: 220 }}
            value={filter.config_id}
            options={configOptions}
            onChange={(value) => {
              const next = { ...filter, config_id: value };
              setFilter(next);
              loadRows(next);
            }}
          />
          <Select
            style={{ minWidth: 120 }}
            value={filter.days}
            options={[7, 30, 90, 180].map((value) => ({ label: `${value}d`, value }))}
            onChange={(value) => {
              const next = { ...filter, days: value };
              setFilter(next);
              loadRows(next);
            }}
          />
          <Button onClick={() => loadRows()} loading={loading}>{t('loading')}</Button>
        </Space>
        <Table
          size="small"
          rowKey={(row) => row.id || `${row.date}-${row.config_id}`}
          dataSource={rows}
          loading={loading}
          scroll={{ x: 980 }}
          locale={{ emptyText: <Empty description={t('noSummaries')} /> }}
          expandable={{ expandedRowRender: (row) => <MarkdownBlock content={row.summary || ''} /> }}
          columns={[
            { title: 'Date', dataIndex: 'date', width: 120 },
            { title: 'Symbol', dataIndex: 'symbol', width: 130 },
            { title: 'Config', dataIndex: 'config_id', width: 180, render: (value) => <Tag>{value}</Tag> },
            { title: 'Sources', dataIndex: 'source_count', width: 90 },
            { title: 'Created', dataIndex: 'created_at', width: 170 },
            authenticated
              ? {
                  title: '',
                  width: 160,
                  render: (_, row) => (
                    <Space>
                      <Button onClick={() => openEditor(row)}>{t('edit')}</Button>
                      <Popconfirm title={t('confirmDelete')} onConfirm={() => deleteSummary(row)}>
                        <Button danger>{t('delete')}</Button>
                      </Popconfirm>
                    </Space>
                  ),
                }
              : {},
          ].filter((item) => item.title !== undefined)}
        />
      </Space>
      <Modal
        title={editingRow ? t('editDailySummary') : t('addDailySummary')}
        open={modalOpen}
        onOk={saveSummary}
        onCancel={() => setModalOpen(false)}
        width={760}
        okText={t('save')}
        footer={[
          <Button key="generate" onClick={generateSummary}>{t('generateDailySummary')}</Button>,
          <Button key="cancel" onClick={() => setModalOpen(false)}>{t('cancel')}</Button>,
          <Button key="save" type="primary" onClick={saveSummary}>{t('save')}</Button>,
        ]}
      >
        <Form form={form} layout="vertical">
          <Form.Item label="Date" name="date" rules={[{ required: true }]}>
            <DatePicker style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="Config" name="config_id" rules={[{ required: true }]}>
            <Select options={configOptions.filter((item) => item.value !== 'ALL')} />
          </Form.Item>
          <Form.Item label={t('summary')} name="summary">
            <TextArea rows={12} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

export default function DashboardPage() {
  const { t, selectedSymbol, setSelectedSymbol } = usePreferences();
  const [timeframe, setTimeframe] = useState('1h');
  const [dashboard, setDashboard] = useState(null);
  const [compareIds, setCompareIds] = useState([]);
  const [comparePayload, setComparePayload] = useState(null);
  const [workspaceMap, setWorkspaceMap] = useState({});
  const [activeTab, setActiveTab] = useState('compare');
  const [loading, setLoading] = useState(true);
  const [compareLoading, setCompareLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [error, setError] = useState('');
  const authenticated = Boolean(localStorage.getItem('crypto-agent-token'));

  useEffect(() => {
    let mounted = true;
    async function loadDashboard() {
      setLoading(true);
      setError('');
      try {
        const response = await api.get('/public/dashboard', { params: selectedSymbol ? { symbol: selectedSymbol } : {} });
        if (!mounted) return;
        setDashboard(response.data);
        if (!selectedSymbol && response.data.current_symbol) {
          setSelectedSymbol(response.data.current_symbol);
        }
        setCompareIds((prev) => {
          const allowed = new Set((response.data.compare_candidates || []).map((item) => item.config_id));
          const filtered = prev.filter((item) => allowed.has(item));
          const next = filtered.length ? filtered : response.data.default_compare_ids || [];
          return next.length === prev.length && next.every((item, index) => item === prev[index]) ? prev : next;
        });
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load dashboard');
      } finally {
        if (mounted) setLoading(false);
      }
    }
    loadDashboard();
    return () => {
      mounted = false;
    };
  }, [selectedSymbol, setSelectedSymbol]);

  useEffect(() => {
    let mounted = true;
    async function loadCompare() {
      const currentSymbol = dashboard?.current_symbol || selectedSymbol;
      if (!currentSymbol) return;
      setCompareLoading(true);
      try {
        const response = await api.get('/public/compare', {
          params: { symbol: currentSymbol, config_ids: compareIds.join(',') },
        });
        if (mounted) setComparePayload(response.data);
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load compare data');
      } finally {
        if (mounted) setCompareLoading(false);
      }
    }
    loadCompare();
    return () => {
      mounted = false;
    };
  }, [compareIds, dashboard?.current_symbol, selectedSymbol]);

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
          configs.map((item) => api.get(`/public/workspace/${item.config_id}`, { params: { timeframe } })),
        );
        if (!mounted) return;
        const nextMap = {};
        responses.forEach((response) => {
          nextMap[response.data.agent.config_id] = { ...response.data, market_timeframes: dashboard?.market_timeframes || [] };
        });
        setWorkspaceMap(nextMap);
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load workspace data');
      } finally {
        if (mounted) setWorkspaceLoading(false);
      }
    }
    loadWorkspaces();
    return () => {
      mounted = false;
    };
  }, [dashboard?.agent_summaries, dashboard?.market_timeframes, timeframe]);

  const summaryCards = useMemo(() => {
    const metrics = dashboard?.overview_metrics || {};
    return [
      { title: t('agents'), value: metrics.agent_count || dashboard?.agent_summaries?.length || 0 },
      { title: t('winRate'), value: metrics.win_rate || 0, suffix: '%' },
      { title: t('totalTrades'), value: metrics.total_trades || 0 },
      { title: t('totalPnl'), value: metrics.total_pnl || 0, precision: 4 },
      { title: t('totalTokens'), value: metrics.total_tokens || 0 },
      { title: t('totalCost'), value: metrics.total_cost || 0, precision: 4 },
    ];
  }, [dashboard, t]);

  const compareSeries = useMemo(() => {
    const series = comparePayload?.series || [];
    return series.map((item) => ({
      name: item.label,
      data: item.points.map((point) => ({ name: point.date, value: point.equity })),
    }));
  }, [comparePayload]);

  const tabItems = useMemo(() => {
    const items = [
      {
        key: 'compare',
        label: t('compareView'),
        children: <ComparePanel dashboard={dashboard} compareSeries={compareSeries} loading={compareLoading} />,
      },
    ];
    (dashboard?.agent_summaries || []).forEach((agent) => {
      items.push({
        key: agent.config_id,
        label: agent.config_id,
        children: <WorkspacePanel workspace={workspaceMap[agent.config_id]} timeframe={timeframe} setTimeframe={setTimeframe} />,
      });
    });
    return items;
  }, [compareLoading, compareSeries, dashboard, t, timeframe, workspaceMap]);

  return (
    <div className="boxed-page dashboard-page">
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="admin-hero dashboard-hero">
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }} wrap>
            <div>
              <Title level={2} style={{ margin: 0 }}>
                {dashboard?.current_symbol || selectedSymbol || t('publicHeadline')}
              </Title>
              <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                {t('publicSubhead')}
              </Paragraph>
            </div>
            <Space wrap>
              <Select
                style={{ minWidth: 180 }}
                value={dashboard?.current_symbol || selectedSymbol || undefined}
                options={(dashboard?.symbols || []).map((item) => ({ label: item, value: item }))}
                onChange={setSelectedSymbol}
                placeholder={t('symbol')}
              />
              <Select
                mode="multiple"
                style={{ minWidth: 280 }}
                value={compareIds}
                options={(dashboard?.compare_candidates || []).map((item) => ({
                  label: `${item.config_id} / ${item.model}`,
                  value: item.config_id,
                }))}
                onChange={setCompareIds}
                placeholder={t('compare')}
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

      {!loading && !(dashboard?.agent_summaries || []).length ? (
        <Card className="panel-card">
          <Empty description={t('emptyWorkspace')} />
        </Card>
      ) : null}

      {!loading && (dashboard?.agent_summaries || []).length ? (
        <>
          <DailySummaryPanel dashboard={dashboard} authenticated={authenticated} />
          <Card className="panel-card dashboard-tabs-card" extra={workspaceLoading ? <Spin size="small" /> : null}>
            <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
          </Card>
        </>
      ) : null}
      </Space>
    </div>
  );
}
