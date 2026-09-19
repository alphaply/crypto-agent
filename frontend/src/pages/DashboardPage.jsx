import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Grid,
  Input,
  InputNumber,
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
import PolymarketPanel from '../components/PolymarketPanel';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock from '../components/ReasoningBlock';
import KlineChart from '../components/KlineChart';
import PositionCycleHistory from '../components/PositionCycleHistory';
import EquityCompareChart from '../components/EquityCompareChart';
import { EditOutlined, ReloadOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { workspaceSignature as getWorkspaceSignature, selectDashboardTab } from '../lib/dashboard';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';
import ScheduleDetails from '../components/ScheduleDetails';
import './DashboardPage.css';

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

function TaskExecutionPanel({ execution, locale }) {
  const status = String(execution?.status || '').toUpperCase();
  if (!execution || !['QUEUED', 'RUNNING', 'FINISHED', 'FAILED'].includes(status)) {
    return null;
  }
  const active = ['QUEUED', 'RUNNING'].includes(status);
  const completed = status === 'FINISHED';
  const failed = status === 'FAILED';
  const toolCalls = Array.isArray(execution.tool_calls) ? execution.tool_calls : [];
  const title = active
    ? (locale === 'zh' ? '任务正在执行' : 'Task in progress')
    : completed
      ? (locale === 'zh' ? '最近任务已完成' : 'Latest task completed')
      : (locale === 'zh' ? '最近任务执行失败' : 'Latest task failed');
  const alertType = failed ? 'error' : completed ? 'success' : 'info';
  const statusColor = failed ? 'error' : completed ? 'success' : 'processing';

  return (
    <Alert
      className={`task-execution-panel ${active ? 'is-active' : completed ? 'is-complete' : 'is-failed'}`}
      type={alertType}
      showIcon
      message={
        <Space size={8} wrap>
          <span>{title}</span>
          <Tag color={statusColor}>{execution.phase || execution.status}</Tag>
          {execution.finished_at || execution.updated_at ? (
            <Text type="secondary">{execution.finished_at || execution.updated_at}</Text>
          ) : null}
        </Space>
      }
      description={
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Text>{execution.progress_message || execution.error || ''}</Text>
          {toolCalls.length ? (
            <div className="task-execution-tools">
              {toolCalls.map((call, index) => (
                <Tag key={call.id || `${call.name}-${index}`} color={call.status === 'failed' ? 'error' : call.status === 'completed' ? 'success' : 'blue'}>
                  {call.name || 'tool'} · {call.status || 'pending'}
                </Tag>
              ))}
            </div>
          ) : null}
          <ReasoningBlock
            title={active
              ? (locale === 'zh' ? '实时推理' : 'Live reasoning')
              : (locale === 'zh' ? '最近任务推理' : 'Latest task reasoning')}
            content={execution.reasoning_content || ''}
            streaming={active}
            reasoningTokens={execution.reasoning_tokens || 0}
            startedAt={execution.started_at_iso || execution.started_at || execution.created_at || execution.scheduled_at}
          />
        </Space>
      }
    />
  );
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
    <div className="agent-overview-shell" role="tablist" aria-label={t('agents')}>
      <button
        type="button"
        role="tab"
        className={`agent-overview-compare-chip ${activeTab === 'compare' ? 'active' : ''}`}
        onClick={() => onSelect('compare')}
        aria-selected={activeTab === 'compare'}
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
              role="tab"
              key={agent.config_id}
              className={`agent-overview-card ${activeTab === agent.config_id ? 'active' : ''}`}
              onClick={() => onSelect(agent.config_id)}
              aria-selected={activeTab === agent.config_id}
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
                  {workspace ? <FactGrid items={keyFacts} /> : <Text type="secondary">{t('loading')} / —</Text>}
                  <span className="agent-overview-footer">
                    <Text type="secondary">{t('nextRun')}: {agent.schedule?.state === 'paused' ? '—' : agent.next_run || '-'} · {agent.freq} · {agent.schedule?.timezone || ''}</Text>
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
  const numeric = Number(value);
  const display = Number.isFinite(numeric) ? String(Number(numeric.toPrecision(12))) : String(value);
  return (
    <button
      type="button"
      className="copy-number"
      onClick={() => {
        navigator.clipboard?.writeText(display);
        message.success(t('copied'));
      }}
    >
      {display}
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
        {isMobile ? (
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
        ) : rows.length ? (
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
        ) : (
          <Empty description={t('noData')} />
        )}
      </Card>
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
  const safePage = Math.min(currentPage, totalPages);
  const pageOrders = orders.slice((safePage - 1) * ORDERS_PER_PAGE, safePage * ORDERS_PER_PAGE);
  return (
    <div className="paginated-order-list">
      <div className="paginated-order-cards">
        {pageOrders.map((row) => <OrderRecordCard key={row.trade_id || row.id || row.order_id || `${row.timestamp}-${row.side}`} row={row} t={t} />)}
      </div>
      {totalPages > 1 && (
        <div className="order-pagination">
          <Pagination
            size="small"
            current={safePage}
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
  const digest = raw.digest || '';
  const nextEvent = (raw.events || []).find((item) => item.scheduled_at && dayjs(item.scheduled_at).isAfter(dayjs().subtract(2, 'hour')));
  const eventDistance = nextEvent ? dayjs(nextEvent.scheduled_at).diff(dayjs(), 'hour', true) : null;
  const eventCountdown = eventDistance === null
    ? ''
    : eventDistance <= 0
      ? (raw.stale ? (isZh ? '缓存' : 'cached') : (isZh ? '已公布' : 'released'))
      : eventDistance < 24
        ? `${Math.max(1, Math.ceil(eventDistance))}h`
        : `${Math.ceil(eventDistance / 24)}d`;
  return (
    <Card className="panel-card intelligence-panel" title={t('newsFlow')}>
      {headlines.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <div className="news-snapshot-meta">
            <Text type="secondary">{snapshot?.timestamp || '-'}</Text>
            <Tag>{isZh ? '宏观 / 政策 / 加密' : 'Macro / Policy / Crypto'}</Tag>
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
          {digest ? (
            <div className="news-intelligence-digest">
              <Text type="secondary">{isZh ? '消息压缩研判' : 'Compressed intelligence'}</Text>
              <div><Text style={{ whiteSpace: 'pre-line' }}>{digest}</Text></div>
            </div>
          ) : null}
          <div className="news-headline-list">
            {headlines.slice(0, 10).map((headline, index) => (
              <div className="news-headline-item" key={`${index}-${headline}`}>
                <div className="news-headline-row">
                  {items[index]?.category ? <Tag>{items[index].category.replace('_', ' ')}</Tag> : null}
                  {items[index]?.url?.startsWith('https://') ? <a href={items[index].url} target="_blank" rel="noreferrer">{headline}</a> : <Text>{headline}</Text>}
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

export function WorkspacePanel({ workspace, timeframe, setTimeframe, authenticated }) {
  const { t, locale } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const agent = workspace?.agent;
  const position = workspace?.position || {};
  const kline = workspace?.kline || {};
  const dailySummaries = workspace?.daily_summaries?.daily_summaries || [];
  const shortMemories = workspace?.short_memories?.short_memories || [];
  const recentOrders = workspace?.orders?.orders || [];
  const pendingOrders = kline?.pending_orders || [];
  const spotMode = isSpotMode(agent?.mode || position?.mode);
  const spotStats = position?.dca_stats || {};

  const [editingMemory, setEditingMemory] = useState(null);
  const [memoryEditText, setMemoryEditText] = useState('');
  const [memorySaving, setMemorySaving] = useState(false);
  const [editingDailySummary, setEditingDailySummary] = useState(null);
  const [summaryEditText, setSummaryEditText] = useState('');
  const [summarySaving, setSummarySaving] = useState(false);
  const [editingProtection, setEditingProtection] = useState(null);
  const [protectionTp, setProtectionTp] = useState(null);
  const [protectionSl, setProtectionSl] = useState(null);
  const [clearTp, setClearTp] = useState(false);
  const [clearSl, setClearSl] = useState(false);
  const [protectionSaving, setProtectionSaving] = useState(false);

  const openProtectionModal = (pos) => {
    setEditingProtection(pos);
    setProtectionTp(pos.take_profit ?? null);
    setProtectionSl(pos.stop_loss ?? null);
    setClearTp(false);
    setClearSl(false);
  };

  const saveProtection = async () => {
    if (!editingProtection) return;
    setProtectionSaving(true);
    try {
      const tpCleared = clearTp;
      const slCleared = clearSl;
      const response = await api.post('/stats/position/protection', {
        config_id: agent.config_id,
        symbol: editingProtection.symbol || agent.symbol,
        side: editingProtection.side,
        stop_loss: slCleared ? null : (protectionSl !== null && protectionSl !== undefined && protectionSl !== '' ? Number(protectionSl) : null),
        take_profit: tpCleared ? null : (protectionTp !== null && protectionTp !== undefined && protectionTp !== '' ? Number(protectionTp) : null),
        clear_stop_loss: slCleared,
        clear_take_profit: tpCleared,
        expected_revision: editingProtection.protection_revision,
      });
      if (!response.data.success || response.data.error) {
        throw new Error(response.data.error || `保护尚未生效：${response.data.state}`);
      }
      setEditingProtection(null);
      message.success(response.data.state === 'WAITING' ? '保护计划已保存，等待入场成交' : '保护已核验并生效');
      window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
    } catch (err) {
      message.error(err.response?.data?.detail || err.message || 'Failed to update protection');
    } finally {
      setProtectionSaving(false);
    }
  };

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
        position_summary: editingMemory.position_summary || '',
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

  const normalizedAnalysis = splitThinkingContent(agent.content || '', agent.reasoning_content || '');
  const executionReasoning = splitThinkingContent('', agent.execution?.reasoning_content || '').reasoning;
  const historyReasoningDuplicated = Boolean(
    executionReasoning
    && normalizedAnalysis.reasoning
    && executionReasoning.trim() === normalizedAnalysis.reasoning.trim()
  );
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
    { label: t('takeProfit'), value: pos.take_profit ? <CopyNumber value={pos.take_profit} /> : '-' },
    { label: t('stopLoss'), value: pos.stop_loss ? <CopyNumber value={pos.stop_loss} /> : '-' },
    { label: locale === 'zh' ? '保护状态' : 'Protection', value: <Tag color={pos.protection_error ? 'red' : pos.protection_state === 'ACTIVE' ? 'green' : 'orange'}>{pos.protection_error ? (locale === 'zh' ? '待核验' : 'Unverified') : pos.protection_state || (locale === 'zh' ? '未设置' : 'Not set')}</Tag> },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card">
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div className="position-header-row">
            <Space size={8} wrap>
              <Text strong>{spotMode ? t('spotAccount') : t('positions')}</Text>
              {spotMode ? <Tag color="gold">SPOT_DCA</Tag> : null}
              {!spotMode && authenticated && activePositions.length === 1 ? (
                <Button size="small" icon={<EditOutlined />} onClick={() => openProtectionModal(activePositions[0])}>
                  {t('adjustTpSl')}
                </Button>
              ) : null}
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
                    <Space size={8}>
                      <Tag color={pos.side === 'SHORT' ? 'red' : 'green'}>{pos.side}</Tag>
                      {authenticated ? (
                        <Button size="small" icon={<EditOutlined />} onClick={() => openProtectionModal(pos)}>
                          {t('adjustTpSl')}
                        </Button>
                      ) : null}
                    </Space>
                  </div>
                  <FactGrid items={buildSinglePositionFacts(pos)} />
                </div>
              ))}
            </div>
          ) : (
            <FactGrid items={buildSinglePositionFacts(activePositions[0])} />
          )}
        </Space>
        <Modal
          open={Boolean(editingProtection)}
          title={`${t('adjustTpSl')} - ${editingProtection?.side || ''} (${editingProtection?.symbol || agent?.symbol || ''})`}
          onCancel={() => setEditingProtection(null)}
          onOk={saveProtection}
          confirmLoading={protectionSaving}
          okText={t('save')}
          cancelText={t('cancel')}
          destroyOnClose
        >
          {editingProtection ? (
            <Space direction="vertical" size="middle" style={{ width: '100%', marginTop: 12 }}>
              <Alert type="info" showIcon message={t('tpSlNotice')} />
              <Text type="secondary">作用于同方向整个仓位（含后续加仓）。留空保留原值；仅勾选取消才移除保护。</Text>
              {editingProtection.protection_error ? <Alert type="error" showIcon message={editingProtection.protection_error} /> : null}
              <Text type="secondary">最近核验：{editingProtection.protection_verified_at ? new Date(editingProtection.protection_verified_at * 1000).toLocaleString() : '尚未核验'} · {editingProtection.protection_state || '未设置'}</Text>
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label={t('side')}>
                  <Tag color={editingProtection.side === 'SHORT' ? 'red' : 'green'}>{editingProtection.side}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('entry')}>
                  {formatPositionValue(editingProtection.entry_price)}
                </Descriptions.Item>
                <Descriptions.Item label={t('mark')}>
                  {formatPositionValue(editingProtection.mark_price)}
                </Descriptions.Item>
                <Descriptions.Item label={t('qty')}>
                  {formatPositionValue(editingProtection.qty || editingProtection.amount || editingProtection.contracts)}
                </Descriptions.Item>
              </Descriptions>

              <Form layout="vertical">
                <Form.Item label={t('takeProfitPrice')}>
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <InputNumber
                      style={{ width: '100%' }}
                      placeholder={t('takeProfitPrice')}
                      value={clearTp ? null : protectionTp}
                      onChange={(val) => setProtectionTp(val)}
                      disabled={clearTp}
                      min={0.00000001}
                    />
                    <Checkbox checked={clearTp} onChange={(e) => setClearTp(e.target.checked)}>
                      {t('clearTp')}
                    </Checkbox>
                  </Space>
                </Form.Item>

                <Form.Item label={t('stopLossPrice')}>
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <InputNumber
                      style={{ width: '100%' }}
                      placeholder={t('stopLossPrice')}
                      value={clearSl ? null : protectionSl}
                      onChange={(val) => setProtectionSl(val)}
                      disabled={clearSl}
                      min={0.00000001}
                    />
                    <Checkbox checked={clearSl} onChange={(e) => setClearSl(e.target.checked)}>
                      {t('clearSl')}
                    </Checkbox>
                  </Space>
                </Form.Item>
              </Form>
            </Space>
          ) : null}
        </Modal>
      </Card>

      {authenticated && !spotMode ? <PositionCycleHistory key={agent.config_id} configId={agent.config_id} /> : null}
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
          <KlineChart payload={kline} chartKey={`${workspace?.agent?.config_id}:${agent?.symbol || ""}:${workspace?.timeframe || timeframe}`} />
        </div>
      </Card>


      <Card className="panel-card" title={t('analysis')}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <TaskExecutionPanel execution={agent.execution} locale={locale} />
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label={t('executedAt')}>{agent.timestamp || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('nextRun')}>{agent.next_run || '-'} · {agent.schedule?.timezone || ''}</Descriptions.Item>
          </Descriptions>
          <MarkdownBlock content={normalizedAnalysis.content || ''} />
          {!historyReasoningDuplicated ? (
            <ReasoningBlock
              title={t('reasoning')}
              content={normalizedAnalysis.reasoning}
              reasoningTokens={agent.reasoning_tokens || 0}
            />
          ) : null}
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
                [window={memory.bucket_start} → {memory.bucket_end}] updated={memory.created_at} sources={memory.source_count ?? 0}
              </Text>
              <MarkdownBlock content={memory.market_summary || ''} />
              {memory.position_summary ? <details><summary>本轮成交事实快照</summary><MarkdownBlock content={memory.position_summary} /></details> : null}
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
  const requestIdRef = useRef(0);
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
    const requestId = ++requestIdRef.current;
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
      if (requestId !== requestIdRef.current) return;
      setRows(response.data.daily_summaries || []);
    } catch (err) {
      if (requestId === requestIdRef.current) message.error(err.message);
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    const next = { ...filter, symbol: dashboard?.current_symbol || '', config_id: 'ALL' };
    const timer = window.setTimeout(() => {
      setFilter(next);
      setRows([]);
      loadRows(next);
    }, 0);
    return () => { window.clearTimeout(timer); requestIdRef.current += 1; };
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
          <Button onClick={() => loadRows()} loading={loading}>{t('refresh')}</Button>
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
  const requestIdRef = useRef(0);
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
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      const response = await api.get('/public/short-memories', {
        params: {
          symbol,
          config_id: nextFilter.config_id || 'ALL',
          limit: nextFilter.limit || 100,
        },
      });
      if (requestId !== requestIdRef.current) return;
      setRows(response.data.short_memories || []);
      setSelectedRowKeys([]);
    } catch (err) {
      if (requestId === requestIdRef.current) message.error(err.message);
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    const next = { ...filter, symbol: dashboard?.current_symbol || '', config_id: 'ALL' };
    const timer = window.setTimeout(() => {
      setFilter(next);
      setRows([]);
      loadRows(next);
    }, 0);
    return () => { window.clearTimeout(timer); requestIdRef.current += 1; };
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
      position_summary: editingRow?.position_summary || '',
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
          <Button onClick={() => loadRows()} loading={loading}>{t('refresh')}</Button>
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
                {row.position_summary ? <details><summary>本轮成交事实快照</summary><MarkdownBlock content={row.position_summary} /></details> : null}
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
  const { t, locale, selectedSymbol, setSelectedSymbol } = usePreferences();
  const [timeframe, setTimeframe] = useState('1h');
  const [dashboard, setDashboard] = useState(null);
  const [compareIds, setCompareIds] = useState([]);
  const [comparePayload, setComparePayload] = useState(null);
  const [workspaceMap, setWorkspaceMap] = useState({});
  const [requestedActiveTab, setRequestedActiveTab] = useState(null);
  const [loading, setLoading] = useState(true);
  const [compareLoading, setCompareLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState(false);
  const [workspaceErrors, setWorkspaceErrors] = useState([]);
  const [marketRefresh, setMarketRefresh] = useState(0);

  useEffect(() => {
    const refresh = () => setRefreshNonce((value) => value + 1);
    window.addEventListener('crypto-agent-dashboard-refresh', refresh);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', refresh);
  }, []);

  useEffect(() => {
    let mounted = true;
    let inFlight = false;
    let successfulPolls = 0;
    const controller = new AbortController();
    async function loadDashboard(initial = false) {
      if (inFlight || (!initial && document.hidden)) return;
      inFlight = true;
      if (initial) { setLoading(true); setError(''); }
      try {
        const response = await api.get('/public/dashboard', {
          params: selectedSymbol ? { symbol: selectedSymbol } : {}, signal: controller.signal, timeout: 25000, silent: !initial,
        });
        if (!mounted) return;
        setDashboard(response.data);
        setError('');
        setPollError(false);
        if (!selectedSymbol && response.data.current_symbol) setSelectedSymbol(response.data.current_symbol);
        setCompareIds((prev) => {
          const allowed = new Set((response.data.compare_candidates || []).map((item) => item.config_id));
          const filtered = prev.filter((item) => allowed.has(item));
          const next = filtered.length ? filtered : response.data.default_compare_ids || [];
          return next.length === prev.length && next.every((item, index) => item === prev[index]) ? prev : next;
        });
        if (!initial && ++successfulPolls % 4 === 0) setMarketRefresh((value) => value + 1);
      } catch (err) {
        if (!mounted) return;
        if (initial) setError(err.message || 'Failed to load dashboard');
        else setPollError(true);
      } finally {
        inFlight = false;
        if (mounted && initial) setLoading(false);
      }
    }
    loadDashboard(true);
    const timer = window.setInterval(() => loadDashboard(), 8000);
    return () => { mounted = false; controller.abort(); window.clearInterval(timer); };
  }, [selectedSymbol, setSelectedSymbol, refreshNonce]);

  // Execution streaming must not refetch every exchange workspace on every token.
  const workspaceSignature = getWorkspaceSignature(dashboard?.agent_summaries);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    async function loadCompare() {
      const currentSymbol = selectedSymbol || dashboard?.current_symbol;
      if (!currentSymbol) return;
      setCompareLoading(true);
      try {
        const response = await api.get('/public/compare', {
          params: { symbol: currentSymbol, config_ids: compareIds.join(',') }, signal: controller.signal, timeout: 25000,
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
      controller.abort();
    };
  }, [compareIds, dashboard?.current_symbol, selectedSymbol, refreshNonce, marketRefresh]);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    async function loadWorkspaces() {
      const configs = JSON.parse(workspaceSignature).map(([config_id]) => ({ config_id }));
      if (!configs.length) {
        setWorkspaceMap({});
        setWorkspaceLoading(false);
        setWorkspaceErrors([]);
        return;
      }
      setWorkspaceLoading(true);
      try {
        const responses = await Promise.allSettled(
          configs.map((item) => api.get(`/public/workspace/${item.config_id}`, { params: { timeframe }, signal: controller.signal, timeout: 30000 })),
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
        setWorkspaceErrors(responses.flatMap((result, index) => result.status === 'rejected' ? [configs[index].config_id] : []));
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load workspace data');
      } finally {
        if (mounted) setWorkspaceLoading(false);
      }
    }
    loadWorkspaces();
    return () => {
      mounted = false;
      controller.abort();
    };
  }, [workspaceSignature, timeframe, refreshNonce, marketRefresh]);

  const compareSeries = useMemo(() => comparePayload?.series || [], [comparePayload]);
  const activeTab = selectDashboardTab(dashboard?.agent_summaries, requestedActiveTab);

  const symbolMatches = !selectedSymbol || dashboard?.current_symbol === selectedSymbol;
  const overviewMetrics = symbolMatches ? dashboard?.overview_metrics || {} : {};
  const heroFacts = [
    { label: t('agents'), value: overviewMetrics.agent_count ?? '—' },
    { label: t('totalTrades'), value: overviewMetrics.total_trades ?? '—' },
    { label: t('winRate'), value: formatPercentValue(overviewMetrics.total_trades ? overviewMetrics.win_rate : null) },
    { label: t('totalPnl'), value: formatPositionValue(overviewMetrics.total_pnl) },
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
      const workspace = workspaceMap[agent.config_id];
      items.push({
        key: agent.config_id,
        label: agent.config_id,
        children: workspace
          ? <WorkspacePanel workspace={{ ...workspace, agent }} timeframe={timeframe} setTimeframe={setTimeframe} authenticated={authenticated} />
          : <Card className="panel-card"><Skeleton active loading={workspaceLoading}><Empty description={locale === 'zh' ? '持仓与行情未能加载，请刷新重试。' : 'Workspace unavailable. Please refresh.'} /></Skeleton></Card>,
      });
    });
    return items;
  }, [authenticated, compareIds, compareLoading, compareSeries, dashboard, t, timeframe, workspaceMap, workspaceLoading, locale]);

  return (
    <div className="boxed-page dashboard-page dashboard-v2">
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <section className="dashboard-command-center">
        <div className="dashboard-command-top">
          <div>
            <div className="dashboard-eyebrow">{locale === 'zh' ? '交易工作台' : 'TRADING WORKSPACE'}</div>
            <Title level={2}>{selectedSymbol || dashboard?.current_symbol || t('publicHeadline')}</Title>
            <Space wrap size={8}>
              <Tag color={dashboard?.scheduler_enabled ? 'green' : 'default'}>{locale === 'zh' ? (dashboard?.scheduler_enabled ? '自动调度已启用' : '自动调度已暂停') : (dashboard?.scheduler_enabled ? 'Scheduler enabled' : 'Scheduler paused')}</Tag>
              <Text type="secondary">{dashboard?.timezone || '—'}</Text>
            </Space>
          </div>
          <div className="dashboard-refresh-controls">
            <Button icon={<ReloadOutlined />} loading={loading || workspaceLoading} onClick={() => setRefreshNonce((value) => value + 1)}>{locale === 'zh' ? '刷新数据' : 'Refresh'}</Button>
            <Text type="secondary"><ClockCircleOutlined /> {locale === 'zh' ? '更新于 ' : 'Updated '}{dashboard?.generated_at ? dayjs(dashboard.generated_at).format('HH:mm:ss') : '—'} · {locale === 'zh' ? '本地时间' : 'local time'}</Text>
          </div>
        </div>
        <FactGrid items={heroFacts} />
        <Text type="secondary" className="dashboard-metric-note">{locale === 'zh' ? '累计统计 · 胜率按已平仓交易计算；账户权益与已实现盈亏口径不同。' : 'All-time statistics · Win rate uses closed trades; account equity differs from realized P&L.'}</Text>
      </section>
      {error ? <Alert type="error" title={error} showIcon /> : null}
      {pollError ? <Alert type="warning" title={locale === 'zh' ? '自动刷新失败，当前显示上次成功的数据。' : 'Refresh failed. Showing the last successful snapshot.'} showIcon /> : null}
      {workspaceErrors.length ? <Alert type="warning" title={locale === 'zh' ? '部分持仓或行情暂时不可用，保留上次数据；请重试。' : 'Some positions or prices are unavailable. Previous data retained; please retry.'} description={workspaceErrors.join(' · ')} showIcon /> : null}

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

      {(!selectedSymbol || dashboard?.current_symbol === selectedSymbol) && (dashboard?.agent_summaries || []).length ? (
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
          <ScheduleDetails agents={dashboard?.agent_summaries || []} activeTab={activeTab} locale={locale} />
          <div className="market-workbench">
            <Tabs activeKey={activeTab} onChange={setRequestedActiveTab} items={tabItems} className="dashboard-main-tabs" renderTabBar={() => null} />
            <aside className="intelligence-rail" aria-label="Market intelligence">
              <PolymarketPanel />
              <NewsSnapshotCard snapshot={workspaceMap?.[activeTab]?.news_snapshot || dashboard?.news_snapshot} />
            </aside>
          </div>
        </div>
      ) : null}
      {!loading && !(dashboard?.agent_summaries || []).length ? <PolymarketPanel /> : null}
      </Space>
    </div>
  );
}
