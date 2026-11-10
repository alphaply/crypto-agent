import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Select, Space, Spin, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { useLatestRequest } from '../hooks/useLatestRequest';
import { formatPaperAmount } from '../lib/paperDisplay';

const REASONS = {
  history_processing: ['历史正在分批补算', 'Historical statistics are being rebuilt'],
  unverified_account_epoch: ['账户周期证据不足', 'Account cycle evidence is incomplete'],
  missing_cycle_evidence: ['缺少持仓周期或数量证据', 'Missing position cycle or quantity evidence'],
  missing_entry_evidence: ['缺少开仓证据', 'Missing entry evidence'],
  missing_fee: ['缺少手续费记录', 'Missing fee records'],
  missing_realized_pnl: ['缺少已实现盈亏记录', 'Missing realized PnL evidence'],
  missing_interest: ['缺少利息记录', 'Missing interest records'],
  missing_fill_evidence: ['缺少实际成交证据', 'Missing fill evidence'],
  unverified_fill: ['部分成交来源无法核实', 'Some fill origins cannot be verified'],
  unreadable_event: ['部分历史记录无法读取', 'Some historical events cannot be read'],
  reused_closed_cycle: ['历史持仓周期存在冲突', 'Conflicting historical position cycles'],
  current_valuation_incomplete: ['当前估值或本金证据不完整', 'Current valuation or capital evidence is incomplete'],
  paper_capital_unverified: ['本金证据不足，本轮总盈亏无法计算', 'Capital evidence is incomplete; cycle PnL is unavailable'],
  missing_price_observation: ['缺少当前持仓的行情观察时间', 'Missing price observation times for current holdings'],
  excluded_venue: ['部分持仓来源不在现有统计范围内', 'Some holding origins are outside the existing scope'],
  unresolved_origin: ['部分持仓来源无法核实', 'Some holding origins cannot be verified'],
  ledger_history_mismatch: ['历史记录与账户累计金额不一致，需补全证据', 'Historical records do not reconcile to account totals'],
};

export default function PaperStatisticsPanel({ namespace, userId, revision, zh }) {
  const [period, setPeriod] = useState('current_cycle');
  const [response, setResponse] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { begin, cancel } = useLatestRequest();
  const scope = JSON.stringify([namespace, userId, period]);
  const data = response?.scope === scope ? response.value : null;
  const label = (cn, en) => zh ? cn : en;
  const format = (value) => formatPaperAmount(value, zh);
  const load = useCallback(async () => {
    const signal = begin();
    if (!signal) return;
    setLoading(true);
    try {
      const { data: value } = await api.get('/mcp/paper/statistics', {
        signal, params: { ...(namespace ? { namespace } : {}), ...(userId ? { user_id: userId } : {}), period },
      });
      if (!signal.aborted) { setResponse({ scope, value }); setError(''); }
    } catch (err) {
      if (!signal.aborted) { setResponse(null); setError(err.message); }
    } finally { if (!signal.aborted) setLoading(false); }
  }, [namespace, userId, period, scope, begin]);
  useEffect(() => {
    let active = true;
    Promise.resolve().then(() => { if (active) load(); });
    return () => { active = false; cancel(); };
  }, [load, revision, cancel]);
  useEffect(() => {
    if (data?.status !== 'processing') return;
    const timer = setTimeout(load, 5000);
    return () => clearTimeout(timer);
  }, [data, load]);
  const current = data?.current;
  const perf = data?.performance;
  const timestamp = (value) => value == null ? '—' : new Date(value * 1000).toLocaleString(zh ? 'zh-CN' : 'en-US');
  const metrics = [
    ['net_equity', label('当前净资产', 'Current net equity'), label('扣除负债后的权益', 'Equity after debt')],
    ['available_cash', label('当前可用资金', 'Available cash'), label('已扣除挂单占用', 'After order reservations')],
    ['debt', label('当前借款负债', 'Outstanding debt'), label('借款不计入收益', 'Borrowing is not profit')],
    ['performance_pnl', label('本轮总盈亏（当前）', 'Current cycle total PnL'), label('包含浮动盈亏，排除资金调整', 'Includes open PnL, excludes capital adjustments')],
  ];
  const summary = !data ? '' : zh ? data.summary : data.status === 'processing'
    ? 'Historical statistics are being rebuilt; complete totals are not ready.'
    : perf?.closed_count == null ? 'Some history is incomplete; unavailable metrics are shown separately.'
      : !perf.closed_count ? 'No fully closed trades in this period. Win rate is unavailable.'
        : `${perf.closed_count} closed trades, ${perf.win_count} profitable, ${format(perf.win_rate)}% win rate.`;
  return <div className="paper-statistics" aria-label={label('完整交易统计', 'Complete trading statistics')} aria-busy={loading}>
    <div className="paper-section-heading">
      <div><h3>{label('资金与交易概览', 'Capital & trading overview')}</h3><p>{label('覆盖当前筛选范围内的全部账户，明细翻页不会改变总览。', 'All accounts matching the filters; detail pagination does not change totals.')}</p></div>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>{label('刷新统计', 'Refresh statistics')}</Button>
    </div>
    {error && <Alert type="error" showIcon title={error} />}
    <div className="paper-metrics">
      {metrics.map(([key, title, note]) => <div key={key} className={'paper-metric' + (key === 'net_equity' ? ' paper-metric-primary' : '')}>
        <div className="paper-metric-title">{title}<span>USDT</span></div>
        <div className={'paper-metric-value' + (key === 'performance_pnl' && current?.[key] != null ? current[key] > 0 ? ' paper-positive' : current[key] < 0 ? ' paper-negative' : '' : '')}>{format(current?.[key])}</div>
        <div className="paper-metric-note">{data && current?.[key] == null ? label('资料不足，暂无法计算', 'Insufficient evidence') : note}</div>
      </div>)}
    </div>
    <div className="paper-balance-strip">
      {[[label('当前账本现金', 'Ledger cash'), current?.cash], [label('当前持仓保证金', 'Position margin'), current?.used_margin], [label('当前挂单占用', 'Reserved cash'), current?.reserved_cash], [label('当前合约浮动盈亏', 'Open perpetual PnL'), current?.unrealized_pnl]].map(([title, value]) => <div key={title}><span>{title}</span><strong>{format(value)} <small>USDT</small></strong></div>)}
    </div>
    <div className="paper-statistics-period">
      <Space wrap><strong>{label('期间交易表现', 'Performance over time')}</strong><Select aria-label={label('统计期间', 'Statistics period')} value={period} onChange={setPeriod} options={[
        { value: 'current_cycle', label: label('本轮', 'Current cycle') }, { value: '7d', label: label('近 7 天', 'Last 7 days') },
        { value: '30d', label: label('近 30 天', 'Last 30 days') }, { value: 'all', label: label('全部历史', 'All history') },
      ]} /></Space>
      <span>{period === 'current_cycle' ? <>{label('各账户开户或最近重置起', 'Since each account’s creation or last reset')}{data?.period_start != null && <>（{label('最早 ', 'earliest ')}{timestamp(data.period_start)}）</>}</> : data?.period_start != null ? timestamp(data.period_start) : label('完整历史', 'All recorded history')} → {timestamp(data?.period_end)}</span>
    </div>
    <div className="paper-profit-strip paper-period-metrics">
      {[
        [label('已实现净收益（含利息）', 'Realized net income after interest'), perf?.realized_net_after_interest, 'USDT'],
        [label('已实现交易净收益', 'Realized trading net PnL'), perf?.realized_net, 'USDT'],
        [label('期间手续费支出', 'Fees paid in period'), perf?.fees, 'USDT'],
        [label('期间已付利息', 'Interest paid in period'), perf?.interest_paid, 'USDT'],
        [label('实际成交次数', 'Actual fills'), perf?.fill_count, ''],
        [label('完整结束交易', 'Fully closed trades'), perf?.closed_count, ''],
        [label('胜率', 'Win rate'), perf?.win_rate, '%'],
        [label('爆仓次数', 'Liquidations'), perf?.liquidation_count, ''],
      ].map(([title, value, unit]) => <div key={title}><span>{title}</span><strong>{format(value)} <small>{value == null ? '' : unit}</small></strong></div>)}
    </div>
    <div className="paper-account-state-line"><Tag>{label('覆盖账户 ', 'Covered accounts ')}{format(data?.coverage?.covered)} / {format(data?.coverage?.accounts)}</Tag><Tag color="blue">{label('已启用 ', 'Enabled ')}{format(current?.enabled_accounts)}</Tag><Tag>{label('冻结 ', 'Bankrupt ')}{format(current?.bankrupt_accounts)}</Tag><span>{label('行情最早观察时间：', 'Oldest price observation: ')}{timestamp(current?.oldest_mark_at)}</span></div>
    {summary && <Alert type={data?.status === 'partial' ? 'warning' : 'info'} showIcon icon={data?.status === 'processing' ? <Spin size="small" /> : undefined} title={summary} description={(data?.reason_codes || []).map((code) => REASONS[code]?.[zh ? 0 : 1] || code).join('；')} />}
    <p className="paper-statistics-method">{label('收益已扣对应的开平仓手续费，不要再减一次期间手续费。分批平仓合并到完整交易，全部平仓后才计胜率；利息按账户单列。借还款、转账和重置补款不算收益。时间筛选只影响本区期间指标，下方明细保留各自分页。', 'Net income already deducts allocated entry and exit fees; do not subtract period fees again. Partial exits count as one trade only once fully closed. Interest stays at account level. Loans, transfers and reset capital are not income. The period selector affects performance metrics; records retain their own pagination.')}</p>
  </div>;
}
