import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Empty, Space, Table, Tabs, Tag, Typography } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { formatPaperAmount, paperFilterCount, paperValuationIncomplete } from '../lib/paperDisplay';
import PaperPositionAdjustModal from './PaperPositionAdjustModal';
import './PaperTradingPanel.css';

export default function PaperDataPanel({ namespace, userId, revision, zh, maxLeverage, onChanged }) {
  const label = (cn, en) => zh ? cn : en;
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [cursor, setCursor] = useState({});
  const [adjusting, setAdjusting] = useState(null);
  const pendingPositionIntents = useRef(new Map());
  const load = useCallback(async (signal, pageCursor = {}) => {
    setLoading(true);
    try {
      const { data: next } = await api.get('/mcp/paper/dashboard', { signal, params: { ...(namespace ? { namespace } : {}), ...(userId ? { user_id: userId } : {}), ...pageCursor, limit: 100 } });
      if (!signal?.aborted) { setData(next); setCursor(pageCursor); setError(''); }
    } catch (err) { if (!signal?.aborted) setError(err.message); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [namespace, userId]);
  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve().then(() => { if (!controller.signal.aborted) load(controller.signal); });
    return () => controller.abort();
  }, [load, revision]);
  const format = (v) => formatPaperAmount(v, zh);
  const number = (v) => <span className="paper-number">{format(v)}</span>;
  const pnl = (v) => <span className={'paper-number ' + (v == null ? '' : Number(v) > 0 ? 'paper-positive' : Number(v) < 0 ? 'paper-negative' : '')}>{v != null && Number(v) > 0 ? '+' : ''}{format(v)}</span>;
  const time = (v) => v ? new Date(v * 1000).toLocaleString(zh ? 'zh-CN' : 'en-US', { timeZone: 'Asia/Shanghai' }) : '—';
  const positions = (data?.accounts || []).flatMap((a) => [
    ...(a.positions || []).map((p) => ({ ...p, namespace: a.namespace, user_id: a.user_id, account_name: a.name, account_type: a.account_type, account_available_cash: a.available_cash, account_status: a.account_status, market_type: 'swap' })),
    ...(a.spot || []).map((p) => ({ ...p, namespace: a.namespace, user_id: a.user_id, account_name: a.name, account_type: a.account_type, market_type: 'spot', side: 'LONG' })),
  ]);
  const state = { open: label('待成交', 'Open'), filled: label('已成交', 'Filled'), cancelled: label('已撤销', 'Cancelled'), rejected: label('失败', 'Rejected') };
  const actions = { buy: label('买入', 'Buy'), sell: label('卖出', 'Sell'), open_long: label('开多', 'Open long'), open_short: label('开空', 'Open short'), close_long: label('平多', 'Close long'), close_short: label('平空', 'Close short') };
  const types = { market: label('市价', 'Market'), limit: label('限价', 'Limit'), stop_loss: label('止损', 'Stop loss'), take_profit: label('止盈', 'Take profit') };
  const kinds = { interest: label('扣息', 'Interest'), bankruptcy: label('破产', 'Bankruptcy'), bankruptcy_recovered: label('状态恢复', 'Status restored'), reset: label('下海', 'Reset'), transfer_in: label('转入', 'Transfer in'), transfer_out: label('转出', 'Transfer out'), order_filled: label('自动成交', 'Automatic fill'), liquidation: label('爆仓', 'Liquidation'), order_rejected: label('委托失败', 'Rejected order'), admin_adjustment: label('后台调整', 'Admin adjustment'), bot_created: label('Bot 开户', 'Bot created'), create: label('开户', 'Account created'), place_order: label('挂单', 'Order placed'), edit_order: label('改单', 'Order edited'), cancel_order: label('撤单', 'Order cancelled'), set_protection: label('设置止盈止损', 'Protection set'), borrow: label('借款', 'Borrow'), repay: label('还款', 'Repay'), set_leverage: label('调整仓位杠杆', 'Position leverage'), add_margin: label('追加保证金', 'Margin added'), reduce_margin: label('减少保证金', 'Margin reduced') };
  const summary = data?.summary;
  const incompleteAccounts = (data?.accounts || []).filter(paperValuationIncomplete).length;
  const filteredResources = (data?.accounts || []).reduce((sum, account) => sum + (paperFilterCount(account.filter) || 0), 0);
  const partial = data?.scan_complete === false || data?.status === 'partial';
  const hasNext = data?.scan_complete === false || Object.values(data?.next_cursor || {}).some(value => value != null);
  const tableProps = { size: 'small', loading, pagination: { pageSize: 8, hideOnSinglePage: true }, locale: { emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={label('当前范围暂无记录', 'No records in this scope')} /> } };
  const account = (v, r) => <div className="paper-cell-stack"><span>{r.account_name || v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</span>{r.account_name && <span className="paper-cell-note">{v}</span>}</div>;
  const symbol = (_, r) => <div className="paper-cell-stack"><strong>{r.symbol}</strong><span className="paper-cell-note">{r.exchange} · {r.market_type === 'spot' ? label('现货', 'Spot') : r.market_type === 'swap' ? label('永续', 'Perpetual') : r.market_type}</span></div>;
  const tabLabel = (cn, en, count) => <span className="paper-tab-label">{label(cn, en)}{data && <span className="paper-tab-count">{count}</span>}</span>;
  const metrics = [
    { key: 'net_equity', title: label('净资产', 'Net equity'), value: summary?.net_equity, note: label('扣除负债后的账户权益', 'Account equity after debt'), accent: true },
    { key: 'available_cash', title: label('可用现金', 'Available cash'), value: summary?.available_cash, note: label('已扣除挂单占用', 'After order reservations') },
    { key: 'debt', title: label('借款负债', 'Outstanding debt'), value: summary?.debt, note: label('借款不计入交易收益', 'Borrowing is not trading profit') },
    { key: 'performance_pnl', title: label('本轮交易盈亏', 'Cycle trading PnL'), value: summary?.performance_pnl, note: label('扣除借款及资金调整影响', 'Excluding funding and loan effects'), signed: true },
  ];
  return <section className="paper-data-panel" aria-label={label('Paper 交易统计', 'Paper trading statistics')}>
    <div className="paper-section-heading">
      <div><h3>{label('资金与交易概览', 'Capital & trading overview')}</h3><p>{label('统计仅覆盖本页账户与订单；盈亏、费用和利息按各账户当前周期合计。', 'Figures cover this page only; PnL, fees and interest sum each account’s current cycle.')}</p></div>
      <Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>{label('刷新数据', 'Refresh data')}</Button>
    </div>
    {error && <Alert type="error" showIcon title={error} />}
    <div className="paper-metrics" aria-busy={loading}>
      {metrics.map((metric) => <div key={metric.key} className={'paper-metric' + (metric.accent ? ' paper-metric-primary' : '')}>
        <div className="paper-metric-title">{metric.title}{!metric.count && <span>USDT</span>}</div>
        <div className={'paper-metric-value' + (metric.signed && metric.value != null ? Number(metric.value) > 0 ? ' paper-positive' : Number(metric.value) < 0 ? ' paper-negative' : '' : '')}>{metric.signed && metric.value > 0 ? '+' : ''}{format(metric.value)}</div>
        <div className="paper-metric-note">{metric.key === 'net_equity' && summary?.valuation_status === 'incomplete' ? label('估值不完整，暂不显示', 'Valuation incomplete') : metric.note}</div>
      </div>)}
    </div>
    <div className="paper-profit-strip">
      {[[label('已实现盈亏', 'Realized PnL'), summary?.realized_pnl, true], [label('合约浮动盈亏', 'Perpetual unrealized PnL'), summary?.unrealized_pnl, true], [label('本轮手续费', 'Cycle fees'), summary?.fees, false], [label('本轮已付利息', 'Cycle interest paid'), summary?.interest_paid, false]].map(([title, value, signed]) => <div key={title}><span>{title}</span><strong>{signed ? pnl(value) : number(value)} <small>USDT</small></strong></div>)}
    </div>
    <div className="paper-balance-strip">
      {[[label('账本现金', 'Ledger cash'), summary?.cash], [label('持仓保证金', 'Position margin'), summary?.used_margin], [label('挂单占用', 'Reserved cash'), summary?.reserved_cash]].map(([title, value]) => <div key={title}><span>{title}</span><strong className="paper-number">{format(value)} <small>USDT</small></strong></div>)}
      <div><span>{label('本页待成交委托', 'Page open orders')}</span><strong className="paper-number">{format(summary?.open_orders)}</strong></div>
      <div><span>{label('本页 / 筛选内账户', 'Page / matching accounts')}</span><strong className="paper-number">{format(summary?.account_count)} <small>/ {format(data?.account_total)}</small></strong></div>
    </div>
    <div className="paper-account-state-line"><span>{label('本页账户状态', 'Accounts on this page')}</span><Tag color="blue">{label('已启用 ', 'Enabled ')}{format(summary?.enabled_accounts)}</Tag><Tag color={summary?.bankrupt_accounts > 0 ? 'red' : 'default'}>{label('破产冻结 ', 'Bankrupt ')}{format(summary?.bankrupt_accounts)}</Tag><span>{label('已启用与冻结分别统计，可同时出现。', 'Enabled and bankrupt counts can overlap.')}</span></div>
    {(incompleteAccounts > 0 || filteredResources > 0) && <Alert type="info" showIcon title={label('部分历史或估值不可用', 'Some history or valuations are unavailable')} description={label('本页过滤 ' + filteredResources + ' 项历史资源；' + incompleteAccounts + ' 个账户估值不完整，缺项显示为 —。被过滤资源的资金占用仍保留。', filteredResources + ' historical resources filtered; ' + incompleteAccounts + ' accounts have incomplete valuations shown as —. Their financial commitments remain.')} />}
    {partial && <Alert type="info" showIcon title={label('扫描尚未完成', 'Scan in progress')} description={label('当前列表与统计不代表完整数据集合；继续扫描可查看后续记录。', 'These lists and figures are not the complete data set. Continue scanning for later records.')} />}
    <Card className="paper-records-card" size="small">
      <div className="paper-records-meta"><span>{label('交易明细', 'Trading records')}</span><span>{label('本页记录 · 北京时间', 'Page records · Beijing time')}</span></div>
      <Tabs items={[
        { key: 'positions', label: tabLabel('持仓', 'Positions', positions.length), children: <Table {...tableProps} rowKey={(r) => [r.namespace, r.user_id, r.resource_id || r.episode_id || [r.exchange, r.symbol, r.side].join(':')].join(':')} dataSource={positions} scroll={{ x: 1190 }} columns={[
          { title: label('账户', 'Account'), dataIndex: 'user_id', render: account, width: 165 },
          { title: label('标的', 'Symbol'), render: symbol, width: 160 },
          { title: label('方向', 'Side'), dataIndex: 'side', render: (v) => v === 'LONG' || v === 'SHORT' ? <Tag color={v === 'LONG' ? 'green' : 'red'} icon={v === 'LONG' ? <ArrowUpOutlined /> : <ArrowDownOutlined />}>{v === 'LONG' ? label('多', 'Long') : label('空', 'Short')}</Tag> : v || '—', width: 80 },
          { title: label('数量（基础币）', 'Base quantity'), dataIndex: 'amount', render: number, align: 'right' },
          { title: label('开仓 / 成本价', 'Entry / cost'), dataIndex: 'entry_price', render: number, align: 'right' },
          { title: label('观察价', 'Observed mark'), dataIndex: 'mark_price', render: number, align: 'right' },
          { title: label('合约浮动盈亏', 'Perpetual unrealized PnL'), dataIndex: 'unrealized_pnl', render: pnl, align: 'right' },
          { title: label('杠杆 / 保证金', 'Leverage / margin'), render: (_, r) => <div className="paper-cell-stack paper-align-right"><span className="paper-number">{r.leverage != null ? format(r.leverage) + '×' : '—'}</span><span className="paper-cell-note">{format(r.margin)}</span></div>, align: 'right' },
          { title: label('行情时间', 'Mark time'), dataIndex: 'marked_at', render: (v) => <span className="paper-cell-note">{time(v)}</span>, width: 160 },
          { title: label('操作', 'Actions'), width: 110, fixed: 'right', render: (_, r) => r.market_type === 'swap' && r.resource_id ? <Button size="small" disabled={r.account_status === 'bankrupt'} onClick={() => setAdjusting(r)}>{label('调整仓位', 'Adjust position')}</Button> : '—' },
        ]} /> },
        { key: 'orders', label: tabLabel('委托与止盈止损', 'Orders & protection', data?.orders?.length), children: <Table {...tableProps} rowKey={(r) => [r.namespace, r.user_id, r.order_id].join(':')} dataSource={data?.orders || []} scroll={{ x: 1060 }} columns={[
          { title: label('账户', 'Account'), dataIndex: 'user_id', width: 150 },
          { title: label('标的', 'Symbol'), render: symbol, width: 150 },
          { title: label('操作', 'Action'), dataIndex: 'action', render: (v) => actions[v] || v },
          { title: label('委托类型', 'Order type'), dataIndex: 'order_type', render: (v) => <Tag>{types[v] || v}</Tag> },
          { title: label('数量', 'Quantity'), render: (_, r) => r.close_all ? label('当前仓位全部', 'Current position all') : number(r.amount), align: 'right' },
          { title: label('委托 / 触发价', 'Limit / trigger'), render: (_, r) => number(r.price ?? r.trigger_price), align: 'right' },
          { title: label('占用 USDT', 'Reserved USDT'), dataIndex: 'reserved_cash', render: number, align: 'right' },
          { title: label('状态', 'State'), dataIndex: 'state', render: (v) => <Tag color={v === 'filled' ? 'green' : v === 'rejected' ? 'red' : v === 'open' ? 'blue' : 'default'}>{state[v] || v}</Tag>, width: 95 },
          { title: label('实际成交价', 'Fill price'), render: (_, r) => number(r.fill?.price), align: 'right' },
        ]} expandable={{ expandedRowRender: (r) => <div className="paper-order-detail"><span>{label('委托 ID', 'Order ID')}: {r.order_id}</span>{r.oco_group && <span>OCO: {r.oco_group}</span>}{r.reason && <span>{label('说明', 'Note')}: {r.reason}</span>}</div> }} /> },
        { key: 'ranking', label: label('收益率榜', 'Returns'), children: <>
          <p className="paper-table-caption">{label('仅纳入完整、可核验的交易收益；充值、借款与转账不计为盈利。', 'Only complete, verifiable trading returns. Funding, loans and transfers are not profit.')}{data?.unranked_count > 0 && ' ' + label('未参与当前排名：', 'Excluded accounts: ') + data.unranked_count}</p>
          <Table {...tableProps} rowKey="participant_id" dataSource={data?.leaderboard || []} scroll={{ x: 650 }} columns={[
            { title: label('排名', 'Rank'), dataIndex: 'rank', width: 80, render: (v) => <span className={'paper-rank' + (v <= 3 ? ' paper-rank-top' : '')}>{v}</span> },
            { title: label('名称', 'Name'), dataIndex: 'name', render: (v, r) => <Space>{v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</Space> },
            { title: label('交易盈亏 · USDT', 'Trading PnL · USDT'), dataIndex: 'pnl', render: pnl, align: 'right' },
            { title: label('收益率', 'Return'), dataIndex: 'return_pct', render: (v) => <span className={'paper-number ' + (v > 0 ? 'paper-positive' : v < 0 ? 'paper-negative' : '')}>{v == null ? '—' : (v > 0 ? '+' : '') + format(v) + '%'}</span>, align: 'right' },
            { title: label('最早行情时间', 'Oldest mark'), dataIndex: 'oldest_mark_at', render: (v) => <span className="paper-cell-note">{time(v)}</span>, align: 'right' },
          ]} />
        </> },
        { key: 'resets', label: label('下海榜', 'Resets'), children: <Table {...tableProps} rowKey="participant_id" dataSource={data?.reset_leaderboard || []} columns={[
          { title: label('排名', 'Rank'), dataIndex: 'rank', width: 80, render: (v) => <span className="paper-rank">{v}</span> },
          { title: label('名称', 'Name'), dataIndex: 'name' },
          { title: label('累计下海次数', 'Lifetime resets'), dataIndex: 'reset_count', render: number, align: 'right' },
        ]} /> },
        { key: 'events', label: tabLabel('资金与成交记录', 'Activity', data?.events?.length), children: <Table {...tableProps} rowKey={(r) => [r.namespace, r.id].join(':')} dataSource={data?.events || []} scroll={{ x: 780 }} columns={[
          { title: label('时间', 'Time'), dataIndex: 'timestamp', render: (v) => <span className="paper-cell-note">{time(v)}</span>, width: 170 },
          { title: label('账户', 'Account'), dataIndex: 'user_id', width: 165 },
          { title: label('事件', 'Event'), dataIndex: 'kind', render: (v) => <Tag color={['liquidation', 'bankruptcy', 'order_rejected'].includes(v) ? 'red' : ['order_filled', 'bankruptcy_recovered'].includes(v) ? 'green' : 'default'}>{kinds[v] || actions[v] || v}</Tag> },
          { title: label('标的', 'Symbol'), render: (_, r) => r.payload?.symbol || r.payload?.fill?.symbol || '—' },
          { title: label('成交价', 'Fill price'), render: (_, r) => number(r.payload?.price ?? r.payload?.fill?.price), align: 'right' },
          { title: label('现金变动后 · USDT', 'Cash after · USDT'), render: (_, r) => number(r.payload?.cash_after), align: 'right' },
        ]} expandable={{ expandedRowRender: (r) => <pre className="paper-audit-json">{JSON.stringify(r.payload, null, 2)}</pre> }} /> },
      ]} />
      <div className="paper-records-footer"><span>{label('Binance 公开行情 · 资金和撮合为模拟', 'Binance public quotes · simulated balances and execution')}</span>{hasNext && <Button loading={loading} onClick={() => load(undefined, {
        account_cursor: data?.next_cursor?.accounts ?? cursor.account_cursor ?? 0,
        order_cursor: data?.next_cursor?.orders ?? cursor.order_cursor ?? 0,
        event_cursor: data?.next_cursor?.events ?? cursor.event_cursor ?? 0,
        namespace_cursor: data?.next_cursor?.namespaces ?? cursor.namespace_cursor ?? '',
      })}>{label('继续扫描下一页', 'Continue to next page')}</Button>}</div>
    </Card>
    {adjusting && <PaperPositionAdjustModal position={adjusting} maxLeverage={maxLeverage} pendingIntents={pendingPositionIntents} zh={zh} onClose={() => setAdjusting(null)} onCompleted={async () => { await Promise.all([load(undefined, cursor), onChanged?.()]); }} />}
  </section>;
}
