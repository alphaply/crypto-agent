import { useState } from 'react';
import { Alert, Button, Card, Empty, Pagination, Select, Skeleton, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { usePreferences } from '../app/usePreferences';
import { useTaskResource } from '../hooks/useTaskResource';
import { historyNumber, historyDate, pnlClass } from '../lib/historyPresentation';
import './PositionHistory.css';

const issueLabels = {
  missing_entry_history: ['缺少建仓记录', 'Missing entry history'], incomplete_fees: ['手续费不完整', 'Incomplete fees'],
  fee_currency_conversion_required: ['部分费用无法换算', 'Fee conversion unavailable'], missing_execution_time: ['缺少准确成交时间', 'Execution time unavailable'],
  incomplete_fill_details: ['逐笔成交明细不完整', 'Incomplete fill details'], quantity_mismatch: ['持仓数量待核对', 'Quantity needs verification'],
  cost_basis_unavailable: ['成本依据不完整', 'Incomplete cost basis'], cost_calibration: ['含人工成本校准', 'Includes cost calibration'],
  foreign_activity: ['存在非本任务的仓位变化', 'Includes activity outside this task'], unknown_execution: ['成交方向或用途待核对', 'Execution purpose unconfirmed'],
  missing_pnl: ['成交盈亏缺失', 'Missing realized PnL'], history_incomplete: ['历史记录不完整', 'Incomplete history'],
  conflicting_evidence: ['成交证据不一致', 'Conflicting execution evidence'],
};
export function HistoryIssues({ issues = [], zh }) {
  return issues.length > 0 && <div className="history-issues">{issues.map(issue => <span key={issue}>{issueLabels[issue]?.[zh ? 0 : 1] || (zh ? '记录待核实' : 'Evidence unconfirmed')}</span>)}</div>;
}
export function Fees({ fees, zh }) {
  return Object.entries(fees || {}).length ? Object.entries(fees).map(([currency, amount]) => <span className="history-fee" key={currency}>{historyNumber(amount, 8)} {currency}</span>) : <span>{zh ? '未记录' : 'Not recorded'}</span>;
}
const stateLabels = { open: ['持仓中', 'Open'], closed: ['已结束', 'Closed'], unconfirmed: ['待核实', 'Unconfirmed'] };
const eventLabels = { entry: ['建仓', 'Entry'], add: ['加仓', 'Add'], reduce: ['减仓', 'Reduce'], close: ['清仓', 'Close'], exit: ['退出', 'Exit'], calibration: ['成本校准', 'Cost calibration'], unknown: ['待核实变化', 'Unconfirmed change'] };

function PositionTimeline({ configId, positionId, refreshKey, zh }) {
  const { data, loading, error, retry } = useTaskResource(`/history/positions/${encodeURIComponent(positionId)}`, { config_id: configId }, refreshKey);
  return <div className="position-timeline-wrap">
    {error && <Alert type="warning" showIcon title={zh ? '持仓明细更新失败' : 'Unable to refresh details'} description={error} action={<Button onClick={retry}>{zh ? '重试' : 'Retry'}</Button>} />}
    {loading && !data ? <Skeleton active paragraph={{ rows: 3 }} /> : data && <ol className="position-timeline">{data.position.events.map(event => <li key={event.event_id}>
      <div className="timeline-heading"><strong>{eventLabels[event.kind]?.[zh ? 0 : 1] || event.kind}</strong><time>{historyDate(event.timestamp, zh)}</time>{!event.is_fill && event.kind !== 'calibration' && <Tag>{zh ? '历史汇总' : 'Historical total'}</Tag>}</div>
      <div className="timeline-facts">
        <span>{zh ? '数量' : 'Quantity'}<b>{historyNumber(event.quantity, 8)}</b></span>
        <span>{zh ? '价格' : 'Price'}<b>{historyNumber(event.price, 8)}</b></span>
        <span>{zh ? '仓位变化' : 'Position change'}<b>{historyNumber(event.quantity_before, 8)} → {historyNumber(event.quantity_after, 8)}</b></span>
        {event.kind !== 'calibration' && <span>{zh ? '已实现净盈亏' : 'Realized net PnL'}<b className={pnlClass(event.realized_net_pnl)}>{event.realized_net_pnl == null ? (zh ? '待核实' : 'Unconfirmed') : historyNumber(event.realized_net_pnl)}</b></span>}
      </div>
      <div className="timeline-fees">{zh ? '手续费：' : 'Fees: '}<Fees fees={(event.fees || []).filter(f => f.cost != null && f.currency).reduce((fees, fee) => ({ ...fees, [fee.currency]: (fees[fee.currency] || 0) + fee.cost }), {})} zh={zh} /></div>
      <HistoryIssues issues={event.issues} zh={zh} />
      {(event.order_id || event.trade_id || event.reason) && <details className="timeline-evidence"><summary>{zh ? '成交身份与操作理由' : 'Execution identity & reason'}</summary>
        {event.order_id && <p>{zh ? '订单' : 'Order'}: {event.order_id}</p>}{event.trade_id && <p>{zh ? '成交' : 'Fill'}: {event.trade_id}</p>}
        <p>{event.reason || (zh ? '历史未记录操作理由' : 'No reason recorded')}</p>
      </details>}
    </li>)}</ol>}
  </div>;
}

function PositionRow({ item, configId, refreshKey, zh }) {
  const [expanded, setExpanded] = useState(false);
  const days = item.holding_seconds == null ? null : item.holding_seconds / 86400;
  return <article className="position-history-row">
    <div className="position-history-heading"><div><strong>{item.symbol}</strong><Tag>{({ LONG: zh ? '多头' : 'Long', SHORT: zh ? '空头' : 'Short', SPOT: zh ? '现货' : 'Spot' })[item.side] || (zh ? '方向待核实' : 'Unknown side')}</Tag><Tag color={item.status === 'open' ? 'blue' : item.status === 'closed' ? 'default' : 'gold'}>{stateLabels[item.status]?.[zh ? 0 : 1]}</Tag></div>
      <div className="position-net"><small>{zh ? '实际净盈亏' : 'Net PnL'}{item.status === 'open' && (zh ? ' · 未结束' : ' · ongoing')}</small><strong className={pnlClass(item.realized_net_pnl)}>{item.realized_net_pnl == null ? (zh ? '待核实' : 'Unconfirmed') : historyNumber(item.realized_net_pnl)} <em>{item.currency}</em></strong></div>
    </div>
    <dl className="position-summary-grid">
      <div><dt>{zh ? '开始时间' : 'Opened'}</dt><dd>{historyDate(item.opened_at, zh)}</dd></div>
      <div><dt>{zh ? '结束时间' : 'Closed'}</dt><dd>{historyDate(item.closed_at, zh)}</dd></div>
      <div><dt>{zh ? '持仓时长' : 'Duration'}</dt><dd>{days == null ? '—' : days >= 1 ? `${historyNumber(days, 1)} ${zh ? '天' : 'days'}` : `${historyNumber(item.holding_seconds / 3600, 1)} ${zh ? '小时' : 'hours'}`}</dd></div>
      <div><dt>{zh ? '累计建仓 / 剩余' : 'Entered / remaining'}</dt><dd>{historyNumber(item.entered_quantity, 8)} / {historyNumber(item.remaining_quantity, 8)}</dd></div>
      <div><dt>{zh ? '费前盈亏' : 'Gross PnL'}</dt><dd>{historyNumber(item.realized_pnl_before_fees)} {item.currency}</dd></div>
      <div><dt>{zh ? '已记录手续费' : 'Recorded fees'}</dt><dd><Fees fees={item.fees_by_currency} zh={zh} /></dd></div>
    </dl>
    <div className="position-history-footer"><HistoryIssues issues={item.issues} zh={zh} /><Button type="link" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? (zh ? '收起持仓过程' : 'Hide timeline') : (zh ? '查看持仓过程' : 'View timeline')}</Button></div>
    {expanded && <PositionTimeline configId={configId} positionId={item.position_id} refreshKey={refreshKey} zh={zh} />}
  </article>;
}

export default function PositionHistory({ configId, refreshKey }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [filters, setFilters] = useState({ page: 1 });
  const { data, loading, error, retry } = useTaskResource('/history/positions', { config_id: configId, per_page: 20, ...filters }, refreshKey);
  const change = values => setFilters(previous => ({ ...previous, ...values, page: 1 }));
  return <Card className="panel-card position-history" title={zh ? '持仓历史' : 'Position history'} extra={<Button type="text" icon={<ReloadOutlined />} loading={loading} onClick={retry} aria-label={zh ? '刷新持仓历史' : 'Refresh position history'} />}>
    <div className="position-history-toolbar"><p>{zh ? '从建仓到清仓，每次变化都有记录。' : 'Every recorded change, from entry to exit.'}</p><div>
      <Select allowClear aria-label={zh ? '持仓标的' : 'Position symbol'} placeholder={zh ? '全部标的' : 'All symbols'} value={filters.symbol} onChange={symbol => change({ symbol })} options={(data?.symbols || []).map(symbol => ({ value: symbol, label: symbol }))} />
      <Select allowClear aria-label={zh ? '持仓状态' : 'Position status'} placeholder={zh ? '全部状态' : 'All statuses'} value={filters.status} onChange={status => change({ status })} options={Object.entries(stateLabels).map(([value, labels]) => ({ value, label: labels[zh ? 0 : 1] }))} />
    </div></div>
    {error && <Alert type="warning" showIcon title={zh ? '持仓历史更新失败' : 'History refresh failed'} description={error} />}
    {data?.errors && Object.keys(data.errors).length > 0 && <Alert type="warning" showIcon title={zh ? '部分标的记录不完整' : 'Some symbol records are incomplete'} description={Object.values(data.errors).join(' · ')} />}
    {loading && !data ? <Skeleton active paragraph={{ rows: 4 }} /> : data && (data.items.length ? <div className="position-history-list">{data.items.map(item => <PositionRow key={item.position_id} item={item} configId={configId} refreshKey={`${refreshKey}:${data.as_of}`} zh={zh} />)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '此范围暂无已记录持仓' : 'No recorded positions in this range'} />)}
    {data?.total > 20 && <Pagination size="small" current={filters.page} total={data.total} pageSize={20} showSizeChanger={false} onChange={page => setFilters(previous => ({ ...previous, page }))} />}
    <p className="history-footnote">{zh ? '仅当前任务本地已归属记录。净盈亏包含交易手续费，不含资金费；现货买入费随卖出份额结算。' : 'Locally recorded task history. Net PnL includes trading fees, excludes funding; spot entry fees are allocated when sold.'}{data?.as_of && <> · {zh ? '更新于 ' : 'Updated '}{historyDate(data.as_of, zh)}</>}</p>
  </Card>;
}
