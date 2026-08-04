import React, { useEffect, useMemo, useRef, useState } from 'react';
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
import ReasoningBlock from '../components/ReasoningBlock';
import KlineChart from '../components/KlineChart';
import EquityCompareChart from '../components/EquityCompareChart';
import { EditOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';

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
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || String(value).trim() === '') return value;
  const abs = Math.abs(numeric);
  if (abs === 0) return '0.00';
  if (abs >= 1) return numeric.toFixed(2);
  const decimals = abs >= 0.01 ? 4 : abs >= 0.0001 ? 6 : 8;
  return numeric.toFixed(decimals).replace(/\.?0+$/, '');
}

function formatPercentValue(value) {
  if (value === null || value === undefined || value === '') return '-';
  const numeric = Number(value);
  return `${Number.isFinite(numeric) ? numeric.toFixed(2) : '0.00'}%`;
}

function isSpotMode(mode) {
  return String(mode || '').toUpperCase() === 'SPOT_DCA';
}

function buildSpotFacts(t, stats, pendingOrders = []) {
  return [
    { label: t('marketValue'), value: formatPositionValue(stats?.market_value) },
    { label: t('totalInvested'), value: formatPositionValue(stats?.total_invested) },
    { label: t('actualBalance'), value: formatPositionValue(stats?.actual_balance) },
    { label: t('recordedQty'), value: formatPositionValue(stats?.total_qty) },
    { label: t('avgCost'), value: formatPositionValue(stats?.avg_cost) },
    { label: t('mark'), value: formatPositionValue(stats?.current_price) },
    { label: t('unrealizedPnl'), value: formatPositionValue(stats?.unrealized_pnl) },
    { label: t('roiPct'), value: formatPercentValue(stats?.return_pct) },
    { label: t('buyCount'), value: stats?.buy_count ?? 0 },
    { label: t('pendingOrders'), value: pendingOrders.length },
  ];
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

function buildPositionFacts(t, position, pendingOrders, summary) {
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
    <div className="agent-overview-shell">
      <button
        type="button"
        className={`agent-overview-compare-chip ${activeTab === 'compare' ? 'active' : ''}`}
        onClick={() => onSelect('compare')}
      >
        <Text strong>{t('compareView')}</Text>
        <Text type="secondary" className="agent-overview-meta">{agents.length} agents</Text>
      </button>
      <div className="agent-overview-grid">
        {agents.map((agent) => {
          const workspace = workspaceMap?.[agent.config_id];
          const pendingOrders = workspace?.kline?.pending_orders || [];
          const position = getPrimaryPosition(workspace);
          const summary = workspace?.position?.summary || {};
          const spotMode = isSpotMode(agent.mode);
          const workspacePending = loading && !workspace;
          const facts = spotMode
            ? buildSpotFacts(t, workspace?.position?.dca_stats, pendingOrders)
            : buildPositionFacts(t, position, pendingOrders, summary);
          if (!spotMode) {
            facts.unshift({ label: t('marginBalance'), value: formatPositionValue(getMarginBalance(workspace)) });
          }
          const keyFacts = facts.slice(0, 3);

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
                <Skeleton active title={false} paragraph={{ rows: 2 }} className="agent-overview-skeleton" />
              ) : (
                <>
                  <FactGrid items={keyFacts} />
                  <span className="agent-overview-footer">
                    <Text type="secondary">{t('nextRun')}: {agent.next_run || '-'}</Text>
                  </span>
                </>
              )}
            </button>
          );
        })}
      </div>
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

function ComparePanel({ dashboard, compareSeries, compareIds, onCompareIdsChange, loading, workspaceMap }) {
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
        <EquityCompareChart series={compareSeries} selectedIds={compareIds} onSelectedIdsChange={onCompareIdsChange} />
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
      <NewsSnapshotCard snapshot={dashboard?.news_snapshot} />
    </Space>
  );
}

function CopyText({ value, className = '' }) {
  const { t } = usePreferences();
  if (value === null || value === undefined || value === '') return '-';
  return (
    <button
      type="button"
      className={`copy-text ${className}`}
      onClick={() => {
        navigator.clipboard?.writeText(String(value));
        message.success(t('copied'));
      }}
    >
      {value}
    </button>
  );
}

function getOrderTagColor(row) {
  const eventType = String(row.event_type || '').toUpperCase();
  const side = String(row.side || '').toUpperCase();
  if (eventType.includes('CANCEL')) return 'default';
  if (eventType === 'SL_HIT') return 'error';
  if (eventType === 'TP_HIT') return 'success';
  if (eventType.includes('CLOSE')) return 'processing';
  if (eventType === 'ENTRY_FILLED' || eventType === 'TRADE_FILL') return 'green';
  if (side.includes('SELL') || side.includes('SHORT')) return 'red';
  if (side.includes('BUY') || side.includes('LONG')) return 'green';
  return 'blue';
}

function ActivityMetric({ label, value, tone = '' }) {
  return (
    <div className={`activity-metric ${tone}`}>
      <Text type="secondary">{label}</Text>
      <div>{value ?? '-'}</div>
    </div>
  );
}

function OrderRecordCard({ row, t }) {
  const tagColor = getOrderTagColor(row);
  const price = row.price ?? row.entry_price;
  const pnl = Number(row.realized_pnl ?? row.pnl ?? 0);
  const hasPnl = Number.isFinite(pnl) && Math.abs(pnl) > 1e-12;
  const idRows = [
    row.order_id ? { label: t('orderId'), value: row.order_id } : null,
    row.parent_order_id && row.parent_order_id !== row.order_id ? { label: 'Parent ID', value: row.parent_order_id } : null,
    row.trade_id ? { label: 'Trade ID', value: row.trade_id } : null,
  ].filter(Boolean);

  return (
    <Card
      key={row.trade_id || row.id || row.order_id || `${row.timestamp}-${row.side}`}
      size="small"
      className="dashboard-mobile-card order-record-card activity-record-card"
      title={(
        <Space size={8} wrap>
          <Tag color={tagColor}>{row.event_label || row.action_label || row.status || '-'}</Tag>
          {row.is_auto ? <Tag color="cyan">AUTO</Tag> : null}
          <Text type="secondary" className="activity-time">{row.timestamp || '-'}</Text>
        </Space>
      )}
    >
      <div className="activity-metric-grid">
        <ActivityMetric label={t('side')} value={row.direction_label || row.side || '-'} />
        <ActivityMetric label={t('price')} value={<CopyNumber value={price} />} />
        <ActivityMetric label={t('amount')} value={<CopyNumber value={row.amount} />} />
        <ActivityMetric label={t('status')} value={row.status || '-'} />
        <ActivityMetric label="PnL" value={formatPositionValue(pnl)} tone={hasPnl ? (pnl > 0 ? 'positive' : 'negative') : ''} />
        {row.fee !== null && row.fee !== undefined ? (
          <ActivityMetric label="Fee" value={`${formatPositionValue(row.fee)} ${row.fee_currency || ''}`.trim()} />
        ) : null}
      </div>

      {idRows.length ? (
        <div className="activity-id-block">
          {idRows.map((item) => (
            <div className="activity-id-row" key={item.label}>
              <Text type="secondary">{item.label}</Text>
              <CopyText value={item.value} className="activity-id-value" />
            </div>
          ))}
        </div>
      ) : null}

      {(row.take_profit || row.stop_loss || row.strategy_note) ? (
        <div className="activity-risk-row">
          {row.take_profit ? <Tag color="green">TP <CopyNumber value={row.take_profit} /></Tag> : null}
          {row.stop_loss ? <Tag color="red">SL <CopyNumber value={row.stop_loss} /></Tag> : null}
          {row.strategy_note ? <Text type="secondary">{row.strategy_note}</Text> : null}
        </div>
      ) : null}

      {row.reason ? (
        <div className="activity-reason">
          <Text type="secondary">{t('reason')}</Text>
          <div>{row.reason}</div>
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

function NewsSnapshotCard({ snapshot }) {
  const { t, locale } = usePreferences();
  const isZh = locale === 'zh';
  const raw = snapshot?.raw || {};
  const headlines = snapshot?.headlines || raw.headlines || [];
  const items = raw.items || [];
  const nextEvent = (raw.events || []).find((item) => item.scheduled_at && dayjs(item.scheduled_at).isAfter(dayjs().subtract(2, 'hour')));
  const risk = snapshot?.risk_level || raw.risk_level || 'normal';
  const riskColor = risk === 'high' ? 'red' : risk === 'watch' ? 'orange' : 'green';
  const eventDistance = nextEvent ? dayjs(nextEvent.scheduled_at).diff(dayjs(), 'hour', true) : null;
  const eventCountdown = eventDistance === null
    ? ''
    : eventDistance <= 0
      ? (raw.stale ? (isZh ? '缓存' : 'cached') : (isZh ? '已公布' : 'released'))
      : eventDistance < 24
        ? `${Math.max(1, Math.ceil(eventDistance))}h`
        : `${Math.ceil(eventDistance / 24)}d`;
  return (
    <Card className="panel-card" title={t('newsFlow')} extra={<Tag color={riskColor}>{risk}</Tag>}>
      {headlines.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <div className="news-snapshot-meta">
            <Text type="secondary">{snapshot?.timestamp || '-'}</Text>
            <Text type="secondary">{snapshot?.source || '-'}</Text>
          </div>
          {nextEvent ? (
            <div className="macro-next-event">
              <div>
                <Text type="secondary">{isZh ? '下一个宏观事件' : 'Next macro event'}</Text>
                <div><Text strong>{nextEvent.title}</Text></div>
                <Text type="secondary">{dayjs(nextEvent.scheduled_at).format('YYYY-MM-DD HH:mm')}</Text>
              </div>
              <Tag color={eventDistance !== null && eventDistance <= 6 ? 'red' : eventDistance !== null && eventDistance <= 24 ? 'orange' : 'blue'}>{eventCountdown}</Tag>
            </div>
          ) : null}
          <div className="news-headline-list">
            {headlines.slice(0, 6).map((headline, index) => (
              <div className="news-headline-item" key={`${index}-${headline}`}>
                <div className="news-headline-row">
                  {items[index]?.category ? <Tag>{items[index].category.replace('_', ' ')}</Tag> : null}
                  <Text>{headline}</Text>
                </div>
              </div>
            ))}
          </div>
          {raw.stale ? <Text type="warning">{isZh ? '当前使用最近一次成功的缓存情报。' : 'Using the most recent cached intelligence snapshot.'}</Text> : null}
        </Space>
      ) : (
        <Empty description={t('noData')} />
      )}
    </Card>
  );
}

function WorkspacePanel({ workspace, timeframe, setTimeframe, authenticated }) {
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
  const newsSnapshot = workspace?.news_snapshot;
  const spotMode = isSpotMode(agent?.mode || position?.mode);
  const spotStats = position?.dca_stats || {};

  const [editingMemory, setEditingMemory] = useState(null);
  const [memoryEditText, setMemoryEditText] = useState('');
  const [memorySaving, setMemorySaving] = useState(false);
  const [editingDailySummary, setEditingDailySummary] = useState(null);
  const [summaryEditText, setSummaryEditText] = useState('');
  const [summarySaving, setSummarySaving] = useState(false);

  const openMemoryEdit = (memory) => {
    setEditingMemory(memory);
    setMemoryEditText(memory.market_summary || '');
  };

  const saveMemoryEdit = async () => {
    if (!editingMemory) return;
    setMemorySaving(true);
    try {
      await api.put('/history/short-memories', {
        config_id: editingMemory.config_id,
        bucket_start: editingMemory.bucket_start,
        market_summary: memoryEditText,
        position_summary: '',
      });
      setEditingMemory(null);
      message.success(t('saved'));
      window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
    } finally {
      setMemorySaving(false);
    }
  };

  const openSummaryEdit = (summary) => {
    setEditingDailySummary(summary);
    setSummaryEditText(summary.summary || summary.content || '');
  };

  const saveSummaryEdit = async () => {
    if (!editingDailySummary) return;
    setSummarySaving(true);
    try {
      await api.put('/history/daily-summaries', {
        date: editingDailySummary.date || editingDailySummary.timestamp,
        config_id: editingDailySummary.config_id,
        summary: summaryEditText,
      });
      setEditingDailySummary(null);
      message.success(t('saved'));
      window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
    } finally {
      setSummarySaving(false);
    }
  };
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
            <Space size={8} wrap>
              <Text strong>{spotMode ? t('spotAccount') : t('positions')}</Text>
              {spotMode ? <Tag color="gold">SPOT_DCA</Tag> : null}
            </Space>
            <div className="position-balance-row">
              {spotMode ? (
                <>
                  <span><Text type="secondary">{t('marketValue')}: </Text><Text>{formatPositionValue(spotStats.market_value)}</Text></span>
                  <span><Text type="secondary">{t('totalInvested')}: </Text><Text>{formatPositionValue(spotStats.total_invested)}</Text></span>
                </>
              ) : (
                <>
                  <span><Text type="secondary">{t('marginBalance')}: </Text><Text>{formatPositionValue(getMarginBalance(workspace))}</Text></span>
                  <span><Text type="secondary">{t('walletBalance')}: </Text><Text>{formatPositionValue(position.balance)}</Text></span>
                </>
              )}
            </div>
          </div>
          {spotMode ? (
            <div className="spot-account-summary">
              <FactGrid items={buildSpotFacts(t, spotStats, pendingOrders)} />
              {spotStats.last_sync ? (
                <Text type="secondary" className="spot-account-sync">
                  {t('lastSync')}: {spotStats.last_sync}
                </Text>
              ) : null}
            </div>
          ) : activePositions.length === 0 ? (
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

      <NewsSnapshotCard snapshot={newsSnapshot} />

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

      <Card
        className="panel-card"
        title={t('shortMemories')}
        extra={shortMemories[0] && authenticated ? (
          <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openMemoryEdit(shortMemories[0])} />
        ) : null}
      >
        {shortMemories[0] ? (() => {
          const memory = shortMemories[0];
          return (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 12 }}>
                [updated={memory.bucket_start}] sources={memory.source_count ?? 0}
              </Text>
              <MarkdownBlock content={memory.market_summary || ''} />
            </Space>
          );
        })() : (
          <Empty description={t('noData')} />
        )}
        <Modal
          open={Boolean(editingMemory)}
          title={t('shortMemories')}
          onCancel={() => setEditingMemory(null)}
          onOk={saveMemoryEdit}
          confirmLoading={memorySaving}
          okText={t('save')}
          cancelText={t('cancel')}
          width={640}
        >
          {editingMemory ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Text type="secondary">{editingMemory.bucket_start} — {editingMemory.bucket_end}</Text>
              <TextArea rows={10} value={memoryEditText} onChange={(e) => setMemoryEditText(e.target.value)} />
            </Space>
          ) : null}
        </Modal>
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
                      ...(row.take_profit ? [{ label: 'TP', value: <CopyNumber value={row.take_profit} /> }] : []),
                      ...(row.stop_loss ? [{ label: 'SL', value: <CopyNumber value={row.stop_loss} /> }] : []),
                      { label: t('status'), value: row.status || 'OPEN' },
                      { label: t('reason'), value: row.reason || '-' },
                      { label: t('orderId'), value: <CopyText value={row.order_id} className="activity-id-value" /> },
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
              scroll={{ x: 1080 }}
              locale={{ emptyText: <Empty description={t('noOpenOrders')} /> }}
              columns={[
                { title: t('side'), dataIndex: 'side' },
                {
                  title: t('type'),
                  dataIndex: 'type',
                  render: (value, row) => (
                    <Space size={4} wrap>
                      <Tag>{value || '-'}</Tag>
                      {row.raw_type ? <Text type="secondary">{row.raw_type}</Text> : null}
                    </Space>
                  ),
                },
                { title: t('price'), dataIndex: 'price', render: (value) => <CopyNumber value={value} /> },
                {
                  title: t('triggerPrice'),
                  dataIndex: 'trigger_price',
                  render: (value) => (value > 0 ? <CopyNumber value={value} /> : '-'),
                },
                { title: t('amount'), dataIndex: 'amount', render: (value) => <CopyNumber value={value} /> },
                { title: 'TP', dataIndex: 'take_profit', render: (value) => (value ? <CopyNumber value={value} /> : '-') },
                { title: 'SL', dataIndex: 'stop_loss', render: (value) => (value ? <CopyNumber value={value} /> : '-') },
                { title: t('status'), dataIndex: 'status', render: (value) => value || 'OPEN' },
                { title: t('reason'), dataIndex: 'reason', width: 220, render: (value) => value || '-' },
                { title: t('orderId'), dataIndex: 'order_id', render: (value) => <CopyText value={value} className="activity-id-value" /> },
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
          <div style={{ maxHeight: 400, overflowY: 'auto', paddingRight: 4 }}>
            {dailySummaries.map((summary) => (
              <Card
                key={`${summary.date || summary.timestamp}-${summary.config_id}`}
                className="summary-snippet"
                extra={authenticated ? (
                  <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openSummaryEdit(summary)} />
                ) : null}
              >
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Text strong>{summary.date || summary.timestamp}</Text>
                  <MarkdownBlock content={summary.summary || summary.content || ''} />
                </Space>
              </Card>
            ))}
          </div>
        ) : (
          <Empty description={t('noSummaries')} />
        )}
        <Modal
          open={Boolean(editingDailySummary)}
          title={t('editDailySummary')}
          onCancel={() => setEditingDailySummary(null)}
          onOk={saveSummaryEdit}
          confirmLoading={summarySaving}
          okText={t('save')}
          cancelText={t('cancel')}
          width={640}
        >
          {editingDailySummary ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Text type="secondary">{editingDailySummary.date || editingDailySummary.timestamp}</Text>
              <TextArea rows={10} value={summaryEditText} onChange={(e) => setSummaryEditText(e.target.value)} />
            </Space>
          ) : null}
        </Modal>
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
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
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
      setSelectedRowKeys([]);
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
      position_summary: '',
    });
    setModalOpen(false);
    await loadRows();
    message.success(t('saved'));
  };

  const deleteShortMemories = async (payload) => {
    await api.delete('/history/short-memories', { data: payload });
    await loadRows();
    window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
    message.success(t('deleted'));
  };

  const deleteSelected = async () => {
    const selected = rows.filter((row) => selectedRowKeys.includes(row.id || `${row.bucket_start}-${row.config_id}`));
    if (!selected.length) return;
    await deleteShortMemories({
      buckets: selected.map((row) => ({
        config_id: row.config_id,
        bucket_start: row.bucket_start,
      })),
    });
  };

  const deleteFiltered = async () => {
    const symbol = filter.symbol || dashboard?.current_symbol;
    if (!symbol) return;
    await deleteShortMemories({
      symbol,
      config_id: filter.config_id || 'ALL',
    });
  };

  const rowKey = (row) => row.id || `${row.bucket_start}-${row.config_id}`;

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
          {authenticated ? (
            <>
              <Popconfirm title={t('confirmDelete')} onConfirm={deleteSelected} disabled={!selectedRowKeys.length}>
                <Button danger disabled={!selectedRowKeys.length}>{t('deleteSelected')}</Button>
              </Popconfirm>
              <Popconfirm title={t('confirmDelete')} onConfirm={deleteFiltered}>
                <Button danger>{t('deleteFiltered')}</Button>
              </Popconfirm>
            </>
          ) : null}
        </Space>
        <Table
          size="small"
          rowKey={rowKey}
          rowSelection={authenticated ? {
            selectedRowKeys,
            onChange: setSelectedRowKeys,
          } : undefined}
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
  const [requestedActiveTab, setRequestedActiveTab] = useState('compare');
  const [loading, setLoading] = useState(true);
  const [compareLoading, setCompareLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [error, setError] = useState('');
  const lastDashboardLoadRef = useRef({ symbol: null, refreshNonce: -1 });

  useEffect(() => {
    const refresh = () => setRefreshNonce((value) => value + 1);
    window.addEventListener('crypto-agent-dashboard-refresh', refresh);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', refresh);
  }, []);

  useEffect(() => {
    const lastLoad = lastDashboardLoadRef.current;
    if (selectedSymbol && lastLoad.symbol === selectedSymbol && lastLoad.refreshNonce === refreshNonce) {
      return undefined;
    }
    let mounted = true;
    async function loadDashboard() {
      setLoading(true);
      setError('');
      try {
        const response = await api.get('/public/dashboard', { params: selectedSymbol ? { symbol: selectedSymbol } : {} });
        if (!mounted) return;
        lastDashboardLoadRef.current = {
          symbol: response.data.current_symbol || selectedSymbol || null,
          refreshNonce,
        };
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
        const responses = await Promise.allSettled(
          configs.map((item) => api.get(`/public/workspace/${item.config_id}`, { params: { timeframe } })),
        );
        if (!mounted) return;
        setWorkspaceMap((previous) => {
          const nextMap = {};
          responses.forEach((result, index) => {
            const configId = configs[index].config_id;
            if (result.status === 'fulfilled') {
              nextMap[configId] = result.value.data;
            } else if (previous[configId]) {
              nextMap[configId] = previous[configId];
            }
          });
          return nextMap;
        });
        if (responses.every((result) => result.status === 'rejected')) {
          setError('Failed to load workspace data');
        }
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

  const compareSeries = useMemo(() => comparePayload?.series || [], [comparePayload]);
  const activeTab = useMemo(() => {
    if (requestedActiveTab === 'compare') return 'compare';
    return (dashboard?.agent_summaries || []).some((agent) => agent.config_id === requestedActiveTab)
      ? requestedActiveTab
      : 'compare';
  }, [dashboard?.agent_summaries, requestedActiveTab]);

  const overviewMetrics = dashboard?.overview_metrics || {};
  const heroFacts = [
    { label: t('agents'), value: overviewMetrics.agent_count ?? (dashboard?.agent_summaries || []).length },
    { label: t('totalTrades'), value: overviewMetrics.total_trades ?? 0 },
    { label: t('winRate'), value: formatPercentValue(overviewMetrics.win_rate) },
    { label: t('totalPnl'), value: formatPositionValue(overviewMetrics.total_pnl ?? 0) },
  ];

  const authenticated = Boolean(localStorage.getItem('crypto-agent-token'));

  const tabItems = useMemo(() => {
    const items = [
      {
        key: 'compare',
        label: t('compareView'),
        children: (
          <ComparePanel
            dashboard={dashboard}
            compareSeries={compareSeries}
            compareIds={compareIds}
            onCompareIdsChange={setCompareIds}
            loading={compareLoading}
            workspaceMap={workspaceMap}
          />
        ),
      },
    ];
    (dashboard?.agent_summaries || []).forEach((agent) => {
      items.push({
        key: agent.config_id,
        label: agent.config_id,
        children: <WorkspacePanel workspace={workspaceMap[agent.config_id]} timeframe={timeframe} setTimeframe={setTimeframe} authenticated={authenticated} />,
      });
    });
    return items;
  }, [authenticated, compareIds, compareLoading, compareSeries, dashboard, t, timeframe, workspaceMap]);

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

      {loading && !dashboard ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : null}

      {!loading && !(dashboard?.agent_summaries || []).length ? (
        <Card className="panel-card">
          <Empty description={t('emptyWorkspace')} />
        </Card>
      ) : null}

      {(dashboard?.agent_summaries || []).length ? (
        <div className="dashboard-workspace">
          <div className="dashboard-agent-overview-wrap">
            <AgentOverview
              agents={dashboard?.agent_summaries || []}
              activeTab={activeTab}
              onSelect={(configId) => setRequestedActiveTab(configId)}
              workspaceMap={workspaceMap}
              loading={workspaceLoading}
            />
          </div>
          <Tabs activeKey={activeTab} onChange={setRequestedActiveTab} items={tabItems} className="dashboard-main-tabs" renderTabBar={() => null} />
        </div>
      ) : null}
      </Space>
    </div>
  );
}
