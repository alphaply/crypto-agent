import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Grid,
  Input,
  Modal,
  Pagination,
  Popconfirm,
  Select,
  Segmented,
  Skeleton,
  Space,
  Spin,
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
const { useBreakpoint } = Grid;

function FactGrid({ items }) {
  return (
    <div className="dashboard-fact-grid">
      {(items || []).map((item) => (
        <div key={item.label} className="dashboard-fact-item">
          <Text type="secondary">{item.label}</Text>
          <div className="dashboard-fact-value">{item.value ?? '-'}</div>
        </div>
      ))}
    </div>
  );
}

function MobileRecordList({ items, emptyText, renderItem }) {
  if (!items?.length) {
    return <Empty description={emptyText} />;
  }

  return <div className="dashboard-mobile-list">{items.map(renderItem)}</div>;
}

function formatPositionValue(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (typeof value === 'number') return value.toFixed(2);
  const numeric = Number(value);
  if (Number.isFinite(numeric) && String(value).trim() !== '') return numeric.toFixed(2);
  return value;
}

function formatPercentValue(value) {
  const numeric = Number(value || 0);
  return `${Number.isFinite(numeric) ? numeric.toFixed(2) : '0.00'}%`;
}

function getPrimaryPosition(workspace) {
  const positions = workspace?.position?.positions || workspace?.kline?.positions || [];
  return positions[0] || workspace?.kline?.position || null;
}

function getMarginBalance(workspace) {
  const stats = workspace?.position || {};
  if (stats.margin_balance !== null && stats.margin_balance !== undefined) {
    return stats.margin_balance;
  }
  const wallet = Number(stats.balance || 0);
  const unrealized = Number(stats.unrealized_pnl || 0);
  return wallet || unrealized ? wallet + unrealized : null;
}

function buildPositionFacts(t, position, pendingOrders, summary, dcaStats) {
  if (dcaStats) {
    return [
      { label: t('avgCost'), value: formatPositionValue(dcaStats.avg_cost) },
      { label: t('qty'), value: formatPositionValue(dcaStats.total_qty) },
      { label: t('pendingOrders'), value: pendingOrders.length },
      { label: t('totalCost'), value: formatPositionValue(dcaStats.total_cost) },
    ];
  }

  if (!position) {
    return [
      { label: t('positions'), value: t('noActivePositions') },
      { label: t('pendingOrders'), value: pendingOrders.length },
      { label: t('winRate'), value: formatPercentValue(summary?.win_rate) },
      { label: t('totalPnl'), value: formatPositionValue(summary?.realized_pnl ?? 0) },
    ];
  }

  return [
    { label: t('side'), value: position.side || '-' },
    { label: t('entry'), value: formatPositionValue(position.entry_price) },
    { label: t('mark'), value: formatPositionValue(position.mark_price) },
    { label: t('qty'), value: formatPositionValue(position.qty || position.amount || position.contracts) },
    { label: t('unrealizedPnl'), value: formatPositionValue(position.unrealized_pnl) },
    { label: t('roiPct'), value: formatPositionValue(position.roi_pct) },
    { label: t('pendingOrders'), value: pendingOrders.length },
    { label: t('winRate'), value: formatPercentValue(summary?.win_rate) },
  ];
}

function AgentOverview({ agents, activeTab, onSelect, workspaceMap, loading }) {
  const { t } = usePreferences();
  if (!agents?.length) return null;

  return (
    <div className="agent-overview-grid">
      {agents.map((agent) => {
        const workspace = workspaceMap?.[agent.config_id];
        const pendingOrders = workspace?.kline?.pending_orders || [];
        const position = getPrimaryPosition(workspace);
        const summary = workspace?.position?.summary || {};
        const dcaStats = agent.mode === 'SPOT_DCA' ? agent.dca_stats : null;
        const workspacePending = loading && !workspace;
        const facts = buildPositionFacts(t, position, pendingOrders, summary, dcaStats);
        if (!dcaStats) {
          facts.unshift({ label: t('marginBalance'), value: formatPositionValue(getMarginBalance(workspace)) });
        }
        const keyFacts = facts.slice(0, 4);

        return (
          <button
            type="button"
            key={agent.config_id}
            className={`agent-overview-card ${activeTab === agent.config_id ? 'active' : ''}`}
            onClick={() => onSelect(agent.config_id)}
          >
            <span className="agent-overview-main">
              <span className="agent-overview-title">
                <Text strong>{agent.title || agent.config_id}</Text>
                <Text type="secondary" className="agent-overview-meta">{agent.config_id}</Text>
              </span>
              <span className="agent-overview-tags">
                <Tag color={agent.enabled ? 'green' : 'default'}>{agent.enabled ? 'ON' : 'OFF'}</Tag>
                <Tag color="blue">{agent.mode}</Tag>
              </span>
            </span>
            {workspacePending ? (
              <Skeleton active title={false} paragraph={{ rows: 4 }} className="agent-overview-skeleton" />
            ) : (
              <>
                <FactGrid items={keyFacts} />
                <span className="agent-overview-footer">
                  <Text type="secondary">{t('nextRun')}: {agent.next_run || '-'}</Text>
                  <Text type="secondary">{agent.freq || '-'}</Text>
                </span>
              </>
            )}
          </button>
        );
      })}
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

function ComparePanel({ dashboard, compareSeries, loading, workspaceMap }) {
  const { t } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const agentMap = useMemo(
    () => Object.fromEntries((dashboard?.agent_summaries || []).map((agent) => [agent.config_id, agent])),
    [dashboard],
  );
  const rows = useMemo(
    () => (dashboard?.compare_rows || []).map((row) => {
      const agent = agentMap[row.config_id] || {};
      const workspace = workspaceMap?.[row.config_id];
      return {
        ...row,
        executed_at: agent.timestamp,
        next_run: agent.next_run,
        freq: agent.freq,
        margin_balance: getMarginBalance(workspace),
      };
    }),
    [agentMap, dashboard, workspaceMap],
  );
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card" title={t('equityCompare')} extra={loading ? <Spin size="small" /> : null}>
        <div className="chart-wrap">
          {compareSeries.length ? <LineChart series={compareSeries} yName={t('equity')} /> : <Empty description={t('noData')} />}
        </div>
      </Card>
      <Card className="panel-card" title={t('compareView')}>
        <div className="compare-cards-scroll">
          <MobileRecordList
            items={rows}
            emptyText={t('noData')}
            renderItem={(row) => (
              <Card
                key={row.config_id}
                size="small"
                className="dashboard-mobile-card compare-agent-card"
                title={row.display_name || row.config_id}
                extra={<Tag color="blue">{row.mode}</Tag>}
              >
                <FactGrid
                  items={[
                    { label: t('modelColumn'), value: row.model || '-' },
                    { label: t('marginBalance'), value: formatPositionValue(row.margin_balance) },
                    { label: t('winRate'), value: formatPercentValue(row.win_rate) },
                    { label: t('totalPnl'), value: formatPositionValue(row.total_pnl ?? 0) },
                    { label: t('longShortRatio'), value: row.long_short_ratio ?? '-' },
                    { label: t('openTotalColumn'), value: row.total_orders ?? 0 },
                    { label: t('executedAt'), value: row.executed_at || '-' },
                    { label: t('nextRun'), value: row.next_run || '-' },
                  ]}
                />
              </Card>
            )}
          />
        </div>
        {!isMobile && rows.length ? (
          <Table
            className="compare-compact-table"
            size="small"
            rowKey="config_id"
            dataSource={rows}
            pagination={false}
            scroll={{ x: 920 }}
            columns={[
              { title: t('configColumn'), dataIndex: 'display_name', width: 180, render: (value, row) => <Tag color="blue">{value || row.config_id}</Tag> },
              { title: t('modeColumn'), dataIndex: 'mode', width: 110 },
              { title: t('longColumn'), dataIndex: 'long_count', width: 90 },
              { title: t('shortColumn'), dataIndex: 'short_count', width: 90 },
              { title: t('closeColumn'), dataIndex: 'close_count', width: 90 },
              { title: t('cancelColumn'), dataIndex: 'cancel_count', width: 90 },
              { title: t('winRate'), dataIndex: 'win_rate', width: 100, render: (value) => formatPercentValue(value) },
              { title: t('totalPnl'), dataIndex: 'total_pnl', width: 120, render: (value) => formatPositionValue(value) },
            ]}
          />
        ) : null}
      </Card>
    </Space>
  );
}

function getOrderTagColor(row) {
  const label = ((row.action_label || row.side || '')).toUpperCase();
  if (label.includes('CANCEL') || label.includes('撤单')) return 'default';
  if (label.includes('OPEN_LONG') || label === 'BUY' || label === 'LONG' || label.includes('开多')) return 'success';
  if (label.includes('OPEN_SHORT') || label === 'SELL' || label === 'SHORT' || label.includes('开空')) return 'error';
  if (label.includes('CLOSE_LONG') || label.includes('平多')) return 'processing';
  if (label.includes('CLOSE_SHORT') || label.includes('平空')) return 'warning';
  if (row.activity_type === 'trade') {
    const side = (row.side || '').toUpperCase();
    if (side.includes('BUY') || side.includes('LONG')) return 'success';
    if (side.includes('SELL') || side.includes('SHORT')) return 'error';
    return 'success';
  }
  return 'blue';
}

function OrderRecordCard({ row, t }) {
  const isTrade = row.activity_type === 'trade';
  const sideUp = (row.side || '').toUpperCase();
  const isCancel = sideUp.includes('CANCEL');

  // 标签颜色
  const tagColor = getOrderTagColor(row);

  let facts;
  if (isTrade) {
    facts = [
      { label: t('price'), value: <CopyNumber value={row.price ?? row.entry_price} /> },
      { label: t('amount'), value: <CopyNumber value={row.amount} /> },
      { label: 'PnL', value: formatPositionValue(row.realized_pnl ?? 0) },
      { label: 'Fee', value: row.fee !== null && row.fee !== undefined ? `${formatPositionValue(row.fee)} ${row.fee_currency || ''}`.trim() : '-' },
      { label: t('side'), value: row.side || '-' },
      { label: t('status'), value: row.status || '-' },
    ];
  } else if (isCancel) {
    facts = [
      ...(row.entry_price ? [{ label: t('entry'), value: <CopyNumber value={row.entry_price} /> }] : []),
      ...(row.amount ? [{ label: t('amount'), value: <CopyNumber value={row.amount} /> }] : []),
      ...(row.take_profit ? [{ label: 'TP', value: <CopyNumber value={row.take_profit} /> }] : []),
      ...(row.stop_loss ? [{ label: 'SL', value: <CopyNumber value={row.stop_loss} /> }] : []),
      { label: t('status'), value: row.status || 'CANCELLED' },
      ...(row.strategy_note ? [{ label: t('note'), value: row.strategy_note }] : []),
      ...(row.order_id ? [{ label: t('orderId'), value: row.order_id }] : []),
    ];
  } else {
    facts = [
      ...(row.entry_price ? [{ label: t('entry'), value: <CopyNumber value={row.entry_price} /> }] : []),
      ...(row.amount ? [{ label: t('amount'), value: <CopyNumber value={row.amount} /> }] : []),
      ...(row.take_profit ? [{ label: 'TP', value: <CopyNumber value={row.take_profit} /> }] : []),
      ...(row.stop_loss ? [{ label: 'SL', value: <CopyNumber value={row.stop_loss} /> }] : []),
      { label: t('status'), value: row.status || '-' },
      ...(row.strategy_note ? [{ label: t('note'), value: row.strategy_note }] : []),
      ...(row.order_id ? [{ label: t('orderId'), value: row.order_id }] : []),
    ];
  }

  return (
    <Card
      key={row.trade_id || row.id || row.order_id || `${row.timestamp}-${row.side}`}
      size="small"
      className="dashboard-mobile-card order-record-card"
      title={row.timestamp || '-'}
      extra={<Tag color={tagColor}>{row.action_label || row.status || '-'}</Tag>}
    >
      <FactGrid items={facts} />
      {isTrade && (row.order_id || row.trade_id || row.cost) ? (
        <div className="order-record-meta">
          {row.order_id ? <Text type="secondary">{t('orderId')}: {row.order_id}</Text> : null}
          {row.trade_id ? <Text type="secondary">Trade ID: {row.trade_id}</Text> : null}
          {row.cost ? <Text type="secondary">Cost: {formatPositionValue(row.cost)}</Text> : null}
        </div>
      ) : null}
      {row.reason ? (
        <div className="dashboard-mobile-card__footer">
          <Text type="secondary">{t('reason')}</Text>
          <div className="dashboard-fact-value">{row.reason}</div>
        </div>
      ) : null}
    </Card>
  );
}

const ORDERS_PER_PAGE = 5;

function PaginatedOrderList({ orders, t }) {
  const [currentPage, setCurrentPage] = useState(1);
  if (!orders?.length) return <Empty description={t('noData')} />;
  const totalPages = Math.ceil(orders.length / ORDERS_PER_PAGE);
  const pageOrders = orders.slice((currentPage - 1) * ORDERS_PER_PAGE, currentPage * ORDERS_PER_PAGE);
  return (
    <div className="paginated-order-list">
      <div className="paginated-order-cards">
        {pageOrders.map((row) => <OrderRecordCard key={row.trade_id || row.id || row.order_id || `${row.timestamp}-${row.side}`} row={row} t={t} />)}
      </div>
      {totalPages > 1 && (
        <div className="order-pagination">
          <Pagination
            size="small"
            current={currentPage}
            total={orders.length}
            pageSize={ORDERS_PER_PAGE}
            onChange={setCurrentPage}
            showSizeChanger={false}
            hideOnSinglePage={false}
          />
        </div>
      )}
    </div>
  );
}

function WorkspacePanel({ workspace, timeframe, setTimeframe }) {
  const { t } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
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

  const activePositions = workspace?.position?.positions || workspace?.kline?.positions || [];
  const hasDualPosition = activePositions.length > 1;

  const buildSinglePositionFacts = (pos) => [
    { label: t('side'), value: pos.side || '-' },
    { label: t('entry'), value: <CopyNumber value={pos.entry_price} /> },
    { label: t('mark'), value: <CopyNumber value={pos.mark_price} /> },
    { label: t('qty'), value: <CopyNumber value={pos.qty || pos.amount || pos.contracts} /> },
    { label: t('unrealizedPnl'), value: formatPositionValue(pos.unrealized_pnl ?? 0) },
    { label: t('roiPct'), value: formatPositionValue(pos.roi_pct ?? 0) },
    { label: t('leverage'), value: pos.leverage ? `${pos.leverage}x` : '-' },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card">
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div className="position-header-row">
            <Text strong>{t('positions')}</Text>
            <div className="position-balance-row">
              <span><Text type="secondary">{t('marginBalance')}: </Text><Text>{formatPositionValue(getMarginBalance(workspace))}</Text></span>
              <span><Text type="secondary">{t('walletBalance')}: </Text><Text>{formatPositionValue(position.balance)}</Text></span>
            </div>
          </div>
          {activePositions.length === 0 ? (
            <Text type="secondary">{t('noActivePositions')}</Text>
          ) : hasDualPosition ? (
            <div className="dual-position-grid">
              {activePositions.map((pos, idx) => (
                <div
                  key={idx}
                  className={`position-card-inner ${pos.side === 'SHORT' ? 'position-card-short' : 'position-card-long'}`}
                >
                  <div className="position-card-badge">
                    <Tag color={pos.side === 'SHORT' ? 'red' : 'green'}>{pos.side}</Tag>
                  </div>
                  <FactGrid items={buildSinglePositionFacts(pos)} />
                </div>
              ))}
            </div>
          ) : (
            <FactGrid items={buildSinglePositionFacts(activePositions[0])} />
          )}
        </Space>
      </Card>

      <Card
        className="panel-card"
        title={t('liveWorkspace')}
        extra={isMobile ? (
          <Select
            value={timeframe}
            options={(timeframeOptions.length ? timeframeOptions : ['15m', '30m', '1h', '4h', '1d', '1w', '1M']).map((value) => ({ label: value, value }))}
            onChange={setTimeframe}
            style={{ minWidth: 120 }}
          />
        ) : (
          <Segmented value={timeframe} onChange={setTimeframe} options={timeframeOptions.length ? timeframeOptions : ['15m', '30m', '1h', '4h', '1d', '1w', '1M']} />
        )}
      >
        <div className="chart-wrap chart-wrap-large">
          <KlineChart payload={kline} />
        </div>
      </Card>

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

      <Card className="panel-card" title={t('shortMemories')}>
        {shortMemories.length ? (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {shortMemories.map((memory) => (
              <Card key={`${memory.config_id}-${memory.bucket_start}`} className="summary-snippet">
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Text strong>{memory.bucket_start} - {memory.bucket_end}</Text>
                  <MarkdownBlock content={memory.market_summary || ''} />
                </Space>
              </Card>
            ))}
          </Space>
        ) : (
          <Empty description={t('noData')} />
        )}
      </Card>

      <div className="workspace-grid">
        <Card className="panel-card" title={t('pendingOrders')}>
          {isMobile ? (
            <MobileRecordList
              items={pendingOrders}
              emptyText={t('noOpenOrders')}
              renderItem={(row) => (
                <Card
                  key={row.order_id || `${row.side}-${row.price}-${row.amount}`}
                  size="small"
                  className="dashboard-mobile-card summary-snippet"
                  title={row.side || '-'}
                  extra={<Tag>{row.type || '-'}</Tag>}
                >
                  <FactGrid
                    items={[
                      { label: t('price'), value: <CopyNumber value={row.price} /> },
                      ...(row.trigger_price > 0 ? [{ label: t('triggerPrice'), value: <CopyNumber value={row.trigger_price} /> }] : []),
                      { label: t('amount'), value: <CopyNumber value={row.amount} /> },
                      { label: t('rawType'), value: row.raw_type || '-' },
                      { label: t('posSide'), value: row.pos_side || '-' },
                      { label: t('orderId'), value: row.order_id || '-' },
                    ]}
                  />
                </Card>
              )}
            />
          ) : (
            <Table
              size="small"
              rowKey={(row) => row.order_id || `${row.side}-${row.price}-${row.amount}`}
              dataSource={pendingOrders}
              pagination={false}
              scroll={{ x: 920 }}
              locale={{ emptyText: <Empty description={t('noOpenOrders')} /> }}
              columns={[
                { title: t('side'), dataIndex: 'side' },
                { title: t('type'), dataIndex: 'type' },
                { title: t('rawType'), dataIndex: 'raw_type' },
                { title: t('posSide'), dataIndex: 'pos_side' },
                { title: t('price'), dataIndex: 'price', render: (value) => <CopyNumber value={value} /> },
                {
                  title: t('triggerPrice'),
                  dataIndex: 'trigger_price',
                  render: (value) => (value > 0 ? <CopyNumber value={value} /> : '-'),
                },
                { title: t('amount'), dataIndex: 'amount', render: (value) => <CopyNumber value={value} /> },
                { title: t('orderId'), dataIndex: 'order_id', ellipsis: true },
              ]}
            />
          )}
        </Card>
      </div>

      <Card className="panel-card" title={t('recentOrders')}>
        <PaginatedOrderList orders={recentOrders} t={t} />
      </Card>

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

export function DailySummaryPanel({ dashboard, authenticated, embedded = false }) {
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
      className={embedded ? 'memory-inner-card' : 'panel-card'}
      title={embedded ? null : t('dailySummaries')}
      extra={!embedded && authenticated ? <Button type="primary" onClick={() => openEditor()}>{t('addDailySummary')}</Button> : null}
      bordered={!embedded}
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
            { title: t('date'), dataIndex: 'date', width: 120 },
            { title: t('symbol'), dataIndex: 'symbol', width: 130 },
            { title: t('configColumn'), dataIndex: 'config_id', width: 180, render: (value) => <Tag>{value}</Tag> },
            { title: t('sources'), dataIndex: 'source_count', width: 90 },
            { title: t('created'), dataIndex: 'created_at', width: 170 },
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
          <Form.Item label={t('date')} name="date" rules={[{ required: true }]}>
            <DatePicker style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label={t('configColumn')} name="config_id" rules={[{ required: true }]}>
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

export function ShortMemoryPanel({ dashboard, authenticated, embedded = false }) {
  const { t } = usePreferences();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState({ symbol: '', config_id: 'ALL', limit: 100 });
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
      const response = await api.get('/public/short-memories', {
        params: {
          symbol,
          config_id: nextFilter.config_id || 'ALL',
          limit: nextFilter.limit || 100,
        },
      });
      setRows(response.data.short_memories || []);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboard?.current_symbol]);

  const openEditor = (row) => {
    setEditingRow(row);
    form.setFieldsValue({
      market_summary: row?.market_summary || '',
      position_summary: row?.position_summary || '',
    });
    setModalOpen(true);
  };

  const saveMemory = async () => {
    if (!editingRow) return;
    const values = await form.validateFields();
    await api.put('/history/short-memories', {
      config_id: editingRow.config_id,
      bucket_start: editingRow.bucket_start,
      market_summary: values.market_summary || '',
      position_summary: values.position_summary || '',
    });
    setModalOpen(false);
    await loadRows();
    message.success(t('saved'));
  };

  return (
    <Card className={embedded ? 'memory-inner-card' : 'panel-card'} title={embedded ? null : t('shortMemories')} bordered={!embedded}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space className="memory-toolbar" wrap>
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
            value={filter.limit}
            options={[20, 50, 100, 200].map((value) => ({ label: `${value}`, value }))}
            onChange={(value) => {
              const next = { ...filter, limit: value };
              setFilter(next);
              loadRows(next);
            }}
          />
          <Button onClick={() => loadRows()} loading={loading}>{t('loading')}</Button>
        </Space>
        <Table
          size="small"
          rowKey={(row) => row.id || `${row.bucket_start}-${row.config_id}`}
          dataSource={rows}
          loading={loading}
          scroll={{ x: 1040 }}
          locale={{ emptyText: <Empty description={t('noData')} /> }}
          expandable={{
            expandedRowRender: (row) => (
              <Space direction="vertical" style={{ width: '100%' }}>
                <MarkdownBlock content={row.market_summary || ''} />
              </Space>
            ),
          }}
          columns={[
            { title: t('bucket'), dataIndex: 'bucket_start', width: 170 },
            { title: t('end'), dataIndex: 'bucket_end', width: 170, responsive: ['lg'] },
            { title: t('symbol'), dataIndex: 'symbol', width: 130, responsive: ['md'] },
            { title: t('configColumn'), dataIndex: 'config_id', width: 180, render: (value) => <Tag>{value}</Tag> },
            { title: t('sources'), dataIndex: 'source_count', width: 90, responsive: ['lg'] },
            { title: t('created'), dataIndex: 'created_at', width: 170, responsive: ['lg'] },
            authenticated
              ? {
                  title: '',
                  width: 90,
                  render: (_, row) => <Button onClick={() => openEditor(row)}>{t('edit')}</Button>,
                }
              : {},
          ].filter((item) => item.title !== undefined)}
        />
      </Space>
      <Modal
        title={t('shortMemories')}
        open={modalOpen}
        onOk={saveMemory}
        onCancel={() => setModalOpen(false)}
        width={760}
        okText={t('save')}
      >
        <Form form={form} layout="vertical">
          <Form.Item label={t('marketDecision')} name="market_summary">
            <TextArea rows={10} />
          </Form.Item>
          <Form.Item label={t('position')} name="position_summary">
            <TextArea rows={5} />
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
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [error, setError] = useState('');

  useEffect(() => {
    const refresh = () => setRefreshNonce((value) => value + 1);
    window.addEventListener('crypto-agent-dashboard-refresh', refresh);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', refresh);
  }, []);

  useEffect(() => {
    setActiveTab('compare');
  }, [selectedSymbol]);

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
  }, [selectedSymbol, setSelectedSymbol, refreshNonce]);

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
  }, [compareIds, dashboard?.current_symbol, selectedSymbol, refreshNonce]);

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
          nextMap[response.data.agent.config_id] = response.data;
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
  }, [dashboard?.agent_summaries, dashboard?.market_timeframes, timeframe, refreshNonce]);

  const compareSeries = useMemo(() => {
    const series = comparePayload?.series || [];
    return series.map((item) => ({
      name: item.label,
      data: item.points.map((point) => ({ name: point.date, value: point.equity })),
    }));
  }, [comparePayload]);

  const overviewMetrics = dashboard?.overview_metrics || {};
  const heroFacts = [
    { label: t('agents'), value: overviewMetrics.agent_count ?? (dashboard?.agent_summaries || []).length },
    { label: t('totalTrades'), value: overviewMetrics.total_trades ?? 0 },
    { label: t('winRate'), value: formatPercentValue(overviewMetrics.win_rate) },
    { label: t('totalPnl'), value: formatPositionValue(overviewMetrics.total_pnl ?? 0) },
  ];

  const tabItems = useMemo(() => {
    const items = [
      {
        key: 'compare',
        label: t('compareView'),
        children: <ComparePanel dashboard={dashboard} compareSeries={compareSeries} loading={compareLoading} workspaceMap={workspaceMap} />,
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
          <div className="dashboard-hero-layout">
            <div className="dashboard-hero-copy">
              <Title level={2} style={{ margin: 0 }}>
                {dashboard?.current_symbol || selectedSymbol || t('publicHeadline')}
              </Title>
              <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                {t('publicSubhead')}
              </Paragraph>
            </div>
            <FactGrid items={heroFacts} />
          </div>

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
        <div className="dashboard-workspace">
          <div className="dashboard-agent-overview-wrap">
            <AgentOverview
              agents={dashboard?.agent_summaries || []}
              activeTab={activeTab}
              onSelect={(configId) => setActiveTab(configId)}
              workspaceMap={workspaceMap}
              loading={workspaceLoading}
            />
          </div>
          <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} className="dashboard-main-tabs" />
        </div>
      ) : null}
      </Space>
    </div>
  );
}
