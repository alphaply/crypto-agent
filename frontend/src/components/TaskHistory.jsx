import { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Pagination, Segmented, Select, Skeleton, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import ToolResultContent from './ToolResultContent';
import './TaskHistory.css';

const actions = { open: ['开仓 / 加仓', 'Open / add'], close: ['平仓 / 退出', 'Close / exit'], edit: ['改单', 'Edit'], cancel: ['撤单', 'Cancel'], protect: ['保护设置', 'Protection'] };
const statuses = { submitted: ['已受理', 'Accepted', 'processing'], completed: ['已完成', 'Completed', 'success'], filled: ['已成交', 'Filled', 'success'], failed: ['失败', 'Failed', 'error'], rejected: ['审批拒绝', 'Declined', 'default'], unknown: ['待核验', 'Unconfirmed', 'warning'], pending: ['待核验', 'Pending', 'warning'], running: ['执行中', 'Running', 'processing'], awaiting_approval: ['待审批', 'Awaiting approval', 'warning'], not_executed: ['未执行', 'Not executed', 'default'] };
const format = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const dateBoundary = (value, through = false) => value ? new Date(`${value}T${through ? '23:59:59.999' : '00:00:00'}`).toISOString() : undefined;

function useTaskResource(path, params, refreshKey) {
  const [state, setState] = useState({ loading: true });
  const [revision, setRevision] = useState(0);
  const key = JSON.stringify(params);
  useEffect(() => {
    const controller = new AbortController();
    const identity = `${path}:${key}`;
    setState(previous => ({ ...(previous.identity === identity ? previous : {}), identity, loading: true, error: '' }));
    api.get(path, { params: JSON.parse(key), signal: controller.signal, silent: true }).then(({ data }) => {
      if (!controller.signal.aborted) setState({ identity, data, loading: false });
    }).catch(error => {
      if (!controller.signal.aborted) setState(previous => ({ ...previous, error: error.message, loading: false }));
    });
    return () => controller.abort();
  }, [path, key, revision, refreshKey]);
  return { ...(state.identity === `${path}:${key}` ? state : { loading: true }), retry: () => setRevision(v => v + 1) };
}

const metricReasons = {
  no_trades: ['暂无成交', 'No fills'], no_completed_cycles: ['尚无完整平仓', 'No completed exits'],
  missing_entry_history: ['缺少开仓记录', 'Missing entry history'], unknown_order_role: ['成交用途待核对', 'Fill purpose unconfirmed'],
  incomplete_fees: ['手续费缺失', 'Missing fees'], history_incomplete: ['历史同步未完成，仅展示已记录部分', 'History incomplete; recorded evidence only'],
  no_loss_samples: ['暂无亏损样本', 'No losing samples'], no_win_samples: ['暂无盈利样本', 'No winning samples'],
  breakeven_only: ['仅有保本平仓', 'Only breakeven exits'], missing_pnl: ['成交盈亏缺失', 'Missing realized PnL'],
  fee_currency_conversion_required: ['手续费币种不同，无法直接扣减', 'Fees require currency conversion'],
};
export function TaskStatistics({ configId, refreshKey }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [period, setPeriod] = useState('seven_days');
  const { data, loading, error, retry } = useTaskResource(`/stats/task-history/${encodeURIComponent(configId)}`, {}, refreshKey);
  const statistics = data?.statistics;
  const row = statistics?.[period];
  const fields = [
    ['realized_pnl_after_fees', zh ? '已实现净盈亏' : 'Realized net PnL', row?.currency],
    ['open_count', zh ? '成交开仓数' : 'Filled entries', zh ? '开仓 / 加仓订单' : 'Entry / add orders'],
    ['win_rate', zh ? '胜率' : 'Win rate', zh ? '完整平仓 · 扣费后' : 'Closed cycles · after fees'],
    ['profit_loss_ratio', zh ? '平均盈亏比' : 'Average win / loss', zh ? '平均盈利 ÷ 平均亏损' : 'Average win ÷ average loss'],
  ];
  return <Card className="panel-card task-statistics" title={zh ? '任务统计' : 'Task statistics'} extra={<Button type="text" icon={<ReloadOutlined />} onClick={retry} aria-label={zh ? '刷新统计' : 'Refresh statistics'} loading={loading} />}>
    <Segmented size="small" value={period} onChange={setPeriod} options={[{ value: 'seven_days', label: zh ? '最近 7 天' : 'Last 7 days' }, { value: 'all_history', label: zh ? '全部历史' : 'All history' }]} />
    {error && <Alert type="warning" showIcon title={zh ? '统计更新失败' : 'Statistics refresh failed'} description={error} />}
    {loading && !row ? <Skeleton active paragraph={{ rows: 1 }} /> : row && <>
      <div className="task-stat-grid">{fields.map(([key, label, unit]) => {
        const metric = row.metrics?.[key];
        const value = metric ? metric.value : row[key];
        const reason = metric?.reason || (!row.fill_count ? 'no_trades' : null);
        const hint = metricReasons[reason]?.[zh ? 0 : 1];
        return <div className="task-stat" key={key}><span>{label}</span><strong className={key === 'realized_pnl_after_fees' && value != null ? value < 0 ? 'is-negative' : 'is-positive' : ''}>{format(value, key === 'open_count' ? 0 : 2)}{key === 'win_rate' && value != null ? '%' : ''}</strong><small>{hint || unit}</small></div>;
      })}</div>
      <div className="task-stat-coverage"><span>{zh ? '仅当前任务' : 'This task only'}</span><span>{zh ? '更新于 ' : 'Updated '}{statistics.as_of ? new Date(statistics.as_of).toLocaleString(zh ? 'zh-CN' : 'en-GB', { hour12: false }) : '—'}</span></div>
      <details className="task-metric-details"><summary>{zh ? '费用、完整精度与统计说明' : 'Fees, precision & definitions'}</summary>
        <p>{zh ? '费前盈亏' : 'Gross PnL'}: {row.realized_pnl_before_fees ?? '—'} {row.currency} · {zh ? '扣费后' : 'Net'}: {row.realized_pnl_after_fees ?? '—'}</p>
        <p>{zh ? '已记录手续费' : 'Recorded fees'}: {Object.entries(row.fees_by_currency || {}).map(([currency, value]) => `${value} ${currency}`).join(' · ') || '—'}</p>
        <p>{zh ? `完整平仓 ${row.closed_cycles} · 盈利 ${row.winning_cycles} · 亏损 ${row.losing_cycles} · 保本 ${row.breakeven_cycles} · 未知 ${row.unknown_cycles}` : `Closed ${row.closed_cycles} · Wins ${row.winning_cycles} · Losses ${row.losing_cycles} · Flat ${row.breakeven_cycles} · Unknown ${row.unknown_cycles}`}</p>
        <p>{zh ? '全历史仅含本地已归属记录。每个实际成交开仓订单计一次；部分成交和改单不重复计数。已实现盈亏含部分平仓，胜率与盈亏比使用完整平仓周期。缺失不视为零。' : 'History includes attributed local fills. Partial fills and amended orders count once. Realized profit includes partial exits; win rate and ratio use completed cycles. Missing values are not zero.'}</p>
        <p>{zh ? '记录起点' : 'Recorded since'}: {statistics.coverage_start_ms ? new Date(statistics.coverage_start_ms).toLocaleString(zh ? 'zh-CN' : 'en-GB', { hour12: false }) : '—'} · {statistics.window_complete ? zh ? '最近 7 天同步完整' : 'Last 7 days synchronized' : zh ? '历史覆盖尚未确认完整' : 'History coverage unconfirmed'}</p>
      </details>
    </>}
  </Card>;
}

export function TradeCallHistory({ configId, refreshKey, compact = false, pendingOnly = false }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [filters, setFilters] = useState({ page: 1 });
  const { data, loading, error, retry } = useTaskResource('/history/trade-calls', { config_id: configId, per_page: compact ? 3 : 20, ...filters, ...(pendingOnly ? { status: 'unresolved' } : {}) }, refreshKey);
  const update = values => setFilters(previous => ({ ...previous, ...values, page: 1 }));
  if (pendingOnly && !error && !data?.items?.length) return null;
  return <Card className="panel-card trade-call-history" title={pendingOnly ? zh ? '待核验操作' : 'Unconfirmed operations' : compact ? zh ? '最近操作' : 'Recent operations' : zh ? '操作记录与理由' : 'Operations & reasons'} extra={<Button type="text" icon={<ReloadOutlined />} onClick={retry} loading={loading} aria-label={zh ? '刷新操作记录' : 'Refresh operations'} />}>
    {!compact && !pendingOnly && <div className="trade-history-filters">
      <Select aria-label={zh ? '筛选动作' : 'Filter action'} allowClear placeholder={zh ? '全部动作' : 'All actions'} value={filters.action} onChange={action => update({ action })} options={Object.entries(actions).map(([value, label]) => ({ value, label: label[zh ? 0 : 1] }))} />
      <Select aria-label={zh ? '筛选状态' : 'Filter status'} allowClear placeholder={zh ? '全部状态' : 'All statuses'} value={filters.status} onChange={status => update({ status })} options={Object.entries(statuses).map(([value, label]) => ({ value, label: label[zh ? 0 : 1] }))} />
      <label>{zh ? '开始日期' : 'From'}<Input type="date" aria-label={zh ? '开始日期' : 'From date'} onChange={event => update({ start: dateBoundary(event.target.value) })} /></label>
      <label>{zh ? '结束日期' : 'Through'}<Input type="date" aria-label={zh ? '结束日期' : 'Through date'} onChange={event => update({ end: dateBoundary(event.target.value, true) })} /></label>
    </div>}
    {error && <Alert type="warning" showIcon title={zh ? '操作记录更新失败' : 'Operations refresh failed'} description={error} />}
    {loading && !data ? <Skeleton active paragraph={{ rows: 1 }} /> : !data && error ? null : !data?.items?.length ? <Typography.Text type="secondary">{zh ? '此范围暂无操作记录' : 'No operations in this range'}</Typography.Text> : <div className="trade-call-list">{data.items.map(item => {
      return <article className="trade-call" key={item.operation_id}>
        <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString(zh ? 'zh-CN' : 'en-GB', { hour12: false })}</time>
        <ToolResultContent content={item.result || { status: item.status }} toolName={item.tool_name || item.action} fallbackStatus={item.status} request={{ symbol: item.symbol, ...item.arguments, reason: item.reason || item.arguments?.reason }} zh={zh} />
      </article>;
    })}</div>}
    {!compact && !loading && data?.total > 20 && <Pagination size="small" current={filters.page} pageSize={20} total={data.total} showSizeChanger={false} onChange={page => setFilters(previous => ({ ...previous, page }))} />}
  </Card>;
}
