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
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import TradeReportPanel from '../components/TradeReportPanel';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock from '../components/ReasoningBlock';
import KlineChart from '../components/KlineChart';
import PositionCycleHistory from '../components/PositionCycleHistory';
import ExitManagementPanel from '../components/ExitManagementPanel';
import { EditOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { chartTimeframeOptions, isChartTimeframe, activityRecordKey } from '../lib/dashboard';
import { exitModeLabel, resolveExitMode } from '../lib/exitManagement';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';
import '../pages/DashboardPage.css';

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
  const portfolio = stats?.is_portfolio || (stats?.by_symbol || []).length > 1;
  return [
    { label: t('marketValue'), value: formatPositionValue(stats?.market_value) },
    { label: t('totalInvested'), value: formatPositionValue(stats?.total_invested) },
    ...(!portfolio ? [
      { label: t('actualBalance'), value: formatPositionValue(stats?.actual_balance) },
      { label: t('recordedQty'), value: formatPositionValue(stats?.total_qty) },
      { label: t('avgCost'), value: formatPositionValue(stats?.avg_cost) },
      { label: t('mark'), value: formatPositionValue(stats?.current_price) },
    ] : []),
    { label: t('unrealizedPnl'), value: formatPositionValue(stats?.unrealized_pnl) },
    { label: t('roiPct'), value: formatPercentValue(stats?.return_pct) },
    { label: t('buyCount'), value: stats?.buy_count ?? 0 },
    { label: t('pendingOrders'), value: stats?.pending_orders ?? pendingOrders.length },
  ];
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
      key={activityRecordKey(row)}
      size="small"
      className="dashboard-mobile-card order-record-card activity-record-card"
      title={(
        <Space size={8} wrap>
          {row.symbol ? <Tag>{row.symbol}</Tag> : null}
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
        {pageOrders.map((row) => <OrderRecordCard key={activityRecordKey(row)} row={row} t={t} />)}
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

export function WorkspacePanel({ workspace, timeframe, setTimeframe, authenticated, chartLoading = false, chartError = false, chartSymbol, setChartSymbol }) {
  const { t, locale } = usePreferences();
  const timeframeControlsRef = useRef(null);
  useEffect(() => {
    const controls = timeframeControlsRef.current;
    const selected = controls?.querySelector('[aria-pressed="true"]');
    if (!selected) return;
    const offset = selected.getBoundingClientRect().left - controls.getBoundingClientRect().left;
    if (offset < 0 || offset + selected.offsetWidth > controls.clientWidth) {
      controls.scrollLeft += offset - (controls.clientWidth - selected.offsetWidth) / 2;
    }
  }, [timeframe]);
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const agent = workspace?.agent;
  const position = workspace?.position || {};
  const kline = workspace?.kline || {};
  const shortMemories = workspace?.short_memories?.short_memories || [];
  const recentOrders = workspace?.orders?.orders || [];
  const pendingOrders = kline?.pending_orders || [];
  const spotMode = isSpotMode(agent?.mode || position?.mode);
  const spotStats = position?.dca_stats || {};
  const chartSymbols = agent?.symbols?.length ? agent.symbols : [agent?.symbol].filter(Boolean);
  const requestedChartSymbol = chartSymbols.includes(chartSymbol) ? chartSymbol : agent?.symbol;
  const displayedChartSymbol = kline.symbol || agent?.symbol;
  const exitMode = position.exit_management?.mode || resolveExitMode({ ...agent, ...position });
  const independentExits = exitMode === 'independent_exits';

  const [editingMemory, setEditingMemory] = useState(null);
  const [memoryEditText, setMemoryEditText] = useState('');
  const [memorySaving, setMemorySaving] = useState(false);
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

  const timeframeOptions = chartTimeframeOptions(workspace?.market_timeframes, timeframe);
  const displayedTimeframe = isChartTimeframe(workspace?.timeframe) ? workspace.timeframe : null;
  const changingTimeframe = displayedTimeframe !== timeframe || displayedChartSymbol !== requestedChartSymbol;
  const waitingForChart = chartLoading || (changingTimeframe && !chartError);
  const chartStatus = waitingForChart
    ? (locale === 'zh'
      ? `${changingTimeframe ? '正在加载' : '正在更新'} ${requestedChartSymbol || ''} ${timeframe}，当前显示 ${displayedChartSymbol || ''} ${displayedTimeframe || '—'}。`
      : `${changingTimeframe ? 'Loading' : 'Updating'} ${requestedChartSymbol || ''} ${timeframe}; showing ${displayedChartSymbol || ''} ${displayedTimeframe || '—'}.`)
    : chartError
      ? (locale === 'zh'
        ? `${requestedChartSymbol || ''} ${timeframe} 行情加载失败，保留 ${displayedChartSymbol || ''} ${displayedTimeframe || '—'} 数据。`
        : `Could not load ${requestedChartSymbol || ''} ${timeframe}. Keeping ${displayedChartSymbol || ''} ${displayedTimeframe || '—'} data.`)
      : (locale === 'zh' ? '自动更新 · 记住上次选择' : 'Auto refresh · Interval remembered');

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
    ...(!independentExits ? [
      { label: t('takeProfit'), value: pos.take_profit ? <CopyNumber value={pos.take_profit} /> : '-' },
      { label: t('stopLoss'), value: pos.stop_loss ? <CopyNumber value={pos.stop_loss} /> : '-' },
      { label: locale === 'zh' ? '保护状态' : 'Protection', value: <Tag color={pos.protection_error ? 'red' : pos.protection_state === 'ACTIVE' ? 'green' : 'orange'}>{pos.protection_error ? (locale === 'zh' ? '待核验' : 'Unverified') : pos.protection_state || (locale === 'zh' ? '未设置' : 'Not set')}</Tag> },
    ] : []),
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="panel-card market-chart-card">
        <div className="market-chart-toolbar">
          <div className="market-chart-heading">
            <Title level={5}>{t('liveWorkspace')}</Title>
            <Text type="secondary">{displayedChartSymbol || '—'} · {locale === 'zh' ? '当前图表 ' : 'Showing '}<strong>{displayedTimeframe || '—'}</strong></Text>
          </div>
          <div className="market-chart-controls">
            {spotMode && chartSymbols.length > 1 && setChartSymbol ? (
              <Select
                aria-label={locale === 'zh' ? '图表标的' : 'Chart symbol'}
                value={requestedChartSymbol}
                options={chartSymbols.map((symbol) => ({ value: symbol, label: symbol }))}
                onChange={setChartSymbol}
                style={{ minWidth: 140 }}
              />
            ) : null}
            <div ref={timeframeControlsRef} className="market-chart-timeframes" role="group" aria-label={locale === 'zh' ? 'K线周期' : 'Chart interval'}>
              {timeframeOptions.map((value) => (
                <button
                  key={value}
                  type="button"
                  className={`market-chart-timeframe${value === timeframe ? ' is-active' : ''}`}
                  aria-pressed={value === timeframe}
                  onClick={() => setTimeframe(value)}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className={`market-chart-status${chartError && !waitingForChart ? ' is-error' : ''}`} role="status" aria-live="polite" aria-atomic="true">
          {waitingForChart ? <Spin size="small" /> : null}
          <span>{chartStatus}</span>
          {chartError && !waitingForChart ? (
            <Button type="link" size="small" icon={<ReloadOutlined />} onClick={() => window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'))}>
              {locale === 'zh' ? '重试' : 'Retry'}
            </Button>
          ) : null}
        </div>
        <div className="chart-wrap chart-wrap-large" aria-busy={waitingForChart}>
          <KlineChart payload={kline} chartKey={`${workspace?.agent?.config_id}:${displayedChartSymbol || ''}:${displayedTimeframe || 'unknown'}`} timeframe={displayedTimeframe} />
        </div>
      </Card>


      <TradeReportPanel report={agent.report} />
      <Card className="panel-card">
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div className="position-header-row">
            <Space size={8} wrap>
              <Text strong>{spotMode ? t('spotAccount') : t('positions')}</Text>
              {spotMode ? <Tag color="gold">SPOT_DCA</Tag> : null}
              {!spotMode ? <Tag color={independentExits ? 'blue' : 'default'}>{exitModeLabel(exitMode, locale)}</Tag> : null}
              {!spotMode && !independentExits && authenticated && activePositions.length === 1 ? (
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
              {spotStats.sync_status === 'partial' ? <Alert type="warning" showIcon message={locale === 'zh' ? '部分账户或行情数据未能核验，未知金额显示为 —，请刷新后确认。' : 'Some account or market data could not be verified. Unknown values are shown as —. Refresh to verify.'} /> : null}
              {spotStats.missing_symbols?.length ? <Alert type="warning" showIcon message={locale === 'zh' ? `部分标的数据不可用：${spotStats.missing_symbols.join(', ')}，组合总额仅含已读取标的。` : `Some symbols are unavailable: ${spotStats.missing_symbols.join(', ')}. Totals include available symbols only.`} /> : null}
              {spotStats.is_portfolio && spotStats.by_symbol?.length ? (
                <Space direction="vertical" size="middle" style={{ width: '100%', marginTop: 16 }}>
                  <Text type="secondary">{locale === 'zh' ? '以下按标的列出持仓，数量与均价分别计算；组合总额使用共同计价币。' : 'Holdings, quantities and average costs are calculated per symbol. Portfolio totals use the shared quote currency.'}</Text>
                  {spotStats.by_symbol.map((asset) => (
                    <Card size="small" key={asset.symbol} title={asset.symbol}>
                      <FactGrid items={buildSpotFacts(t, asset)} />
                    </Card>
                  ))}
                  <Text type="secondary">{locale === 'zh' ? '本次运行组合共用额度' : 'Shared allowance per run'}: {formatPositionValue(spotStats.dca_amount_per)} {spotStats.quote_asset || agent.symbol?.split('/')[1] || ''}</Text>
                </Space>
              ) : null}
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
                      {authenticated && !independentExits ? (
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
          {!spotMode ? <ExitManagementPanel management={position.exit_management} /> : null}
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

      <Card className="panel-card" title={t('analysis')}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <TaskExecutionPanel execution={agent.execution} locale={locale} />
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label={t('executedAt')}>{agent.timestamp || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('nextRun')}>{agent.next_run || '-'} · {agent.schedule?.timezone || ''}</Descriptions.Item>
          </Descriptions>
          {!agent.report || agent.report.validation_status === 'invalid' ? <MarkdownBlock content={normalizedAnalysis.content || ''} /> : null}
          {!historyReasoningDuplicated ? (
            <ReasoningBlock
              title={t('reasoning')}
              content={normalizedAnalysis.reasoning}
              reasoningTokens={agent.reasoning_tokens || 0}
            />
          ) : null}
          {agent.strategy_logic ? (
            <details className="content-disclosure strategy-block">
              <summary>{t('strategyLogic')}</summary>
              <MarkdownBlock content={agent.strategy_logic} />
            </details>
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
            <details className="content-disclosure">
              <summary>{memory.window_start || memory.bucket_start} → {memory.window_end || memory.bucket_end}</summary>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Text type="secondary" className="memory-source-meta">
                  {locale === 'zh' ? '更新于' : 'Updated'} {memory.created_at} · {locale === 'zh' ? '来源' : 'Sources'} {memory.source_count ?? 0} {memory.version ? ` · v${memory.version}` : ''}
                </Text>
                <MarkdownBlock content={memory.market_summary || ''} />
                {memory.position_summary ? <details className="content-disclosure"><summary>{locale === 'zh' ? '成交事实快照' : 'Execution snapshot'}</summary><MarkdownBlock content={memory.position_summary} /></details> : null}
              </Space>
            </details>
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
              <Text type="secondary">{editingMemory.window_start || editingMemory.bucket_start} — {editingMemory.window_end || editingMemory.bucket_end}</Text>
              <TextArea rows={10} value={memoryEditText} onChange={(e) => setMemoryEditText(e.target.value)} />
            </Space>
          ) : null}
        </Modal>
      </Card>

      <div className="workspace-grid">
        <Card className="panel-card" title={`${t('pendingOrders')} · ${displayedChartSymbol || ''}`}>
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

    </Space>
  );
}

export function ShortMemoryPanel({ dashboard, authenticated, embedded = false }) {
  const { t, locale } = usePreferences();
  const [rows, setRows] = useState([]);
  const requestIdRef = useRef(0);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState({ symbol: '', config_id: 'ALL', limit: 100 });
  const [modalOpen, setModalOpen] = useState(false);
  const [editingRow, setEditingRow] = useState(null);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [reviewing, setReviewing] = useState(false);
  const [reviewResult, setReviewResult] = useState(null);
  const reviewInFlight = useRef(false);
  const reviewSequence = useRef(0);
  const [form] = Form.useForm();

  useEffect(() => () => { ++reviewSequence.current; }, []);

  const configOptions = useMemo(
    () => [
      { label: 'ALL', value: 'ALL' },
      ...((dashboard?.agent_summaries || []).map((agent) => ({ label: agent.config_id, value: agent.config_id }))),
    ],
    [dashboard],
  );

  const loadRows = async (nextFilter = filter) => {
    const symbol = nextFilter.symbol || dashboard?.current_symbol;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      const response = await api.get('/public/short-memories', {
        params: {
          symbol: symbol || undefined,
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

  const reviewMemory = async () => {
    if (!authenticated || filter.config_id === 'ALL' || !filter.config_id || reviewInFlight.current) return;
    const configId = filter.config_id;
    const sequence = ++reviewSequence.current;
    const zh = locale === 'zh';
    reviewInFlight.current = true;
    setReviewing(true);
    setReviewResult(null);
    try {
      const { data } = await api.post('/history/short-memories/generate', { config_id: configId });
      if (sequence !== reviewSequence.current) return;
      const status = data.review_status;
      const result = status === 'partial'
        ? { type: 'warning', title: zh ? '规则操作已有回执；记忆整理未完成，旧记忆保留。' : 'Rule operations returned receipts, but memory consolidation did not finish. Previous memory was preserved.' }
        : status === 'unchanged'
          ? { type: 'info', title: zh ? '本窗口无新增证据，沿用已有记忆。' : 'No new evidence for this window. Existing memory is retained.' }
          : status === 'completed' && data.generated
            ? { type: 'success', title: zh ? '短期动态记忆已更新。' : 'Short-term memory updated.' }
            : { type: 'error', title: zh ? '复盘未完成，未生成新记忆。' : 'Review did not complete; no new memory was generated.' };
      setReviewResult({ ...result, configId, error: data.error || '', receipts: data.rule_receipts || [] });
    } catch (err) {
      if (sequence === reviewSequence.current) setReviewResult({ type: 'error', title: zh ? '复盘请求失败，以下仍显示已有记忆。' : 'Review request failed. Existing memory is shown below.', configId, error: err.message, receipts: [] });
    } finally {
      reviewInFlight.current = false;
      if (sequence === reviewSequence.current) {
        setReviewing(false);
        await loadRows();
      }
    }
  };

  return (
    <Card className={embedded ? 'memory-inner-card' : 'panel-card'} title={embedded ? null : t('shortMemories')} bordered={!embedded}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space className="memory-toolbar" wrap>
          <Select
            style={{ minWidth: 180 }}
            disabled={reviewing}
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
            disabled={reviewing}
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
              <Button type="primary" onClick={reviewMemory} loading={reviewing} disabled={!filter.config_id || filter.config_id === 'ALL'}>{locale === 'zh' ? '复盘并整理' : 'Review and consolidate'}</Button>
              <Popconfirm title={t('confirmDelete')} onConfirm={deleteSelected} disabled={!selectedRowKeys.length}>
                <Button danger disabled={!selectedRowKeys.length}>{t('deleteSelected')}</Button>
              </Popconfirm>
              <Popconfirm title={t('confirmDelete')} onConfirm={deleteFiltered}>
                <Button danger>{t('deleteFiltered')}</Button>
              </Popconfirm>
            </>
          ) : null}
        </Space>
        {authenticated ? <Text type="secondary">{locale === 'zh' ? '每次运行后结合前 4 小时摘要更新短期记忆。选择任务可手动整理，规则改动以回执为准。' : 'Memory refreshes after each run using the previous four hours. Select a task to refresh manually; rule changes require confirmed receipts.'}</Text> : null}
        {authenticated && reviewResult ? <Alert type={reviewResult.type} showIcon title={`${reviewResult.configId} · ${reviewResult.title}`} description={<Space direction="vertical" style={{ width: '100%' }}>{reviewResult.error ? <Text>{reviewResult.error}</Text> : null}{reviewResult.receipts.length ? <details><summary>{locale === 'zh' ? '规则工具回执' : 'Rule tool receipts'} ({reviewResult.receipts.length})</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 300, overflow: 'auto', fontSize: 12 }}>{JSON.stringify(reviewResult.receipts, null, 2)}</pre></details> : null}</Space>} /> : null}
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
                {!!row.source_summary_ids?.length && <Text type="secondary">{locale === 'zh' ? '来源摘要：' : 'Source summaries: '}{row.source_summary_ids.join(', ')}</Text>}
                {row.position_summary ? <details><summary>本轮成交事实快照</summary><MarkdownBlock content={row.position_summary} /></details> : null}
              </Space>
            ),
          }}
          columns={[
            { title: locale === 'zh' ? '窗口开始' : 'Window start', dataIndex: 'bucket_start', width: 170, render: (value,row) => row.window_start || value },
            { title: t('end'), dataIndex: 'bucket_end', width: 170, responsive: ['lg'], render: (value,row) => row.window_end || value },
            { title: locale === 'zh' ? '版本' : 'Version', dataIndex: 'version', width: 80, render: (value) => value ? `v${value}` : '—' },
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
