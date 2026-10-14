import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Space, Table, Tabs, Tag, Typography } from 'antd';
import { api } from '../lib/api';

export default function PaperDataPanel({ namespace, userId, revision, zh }) {
  const label = (cn, en) => zh ? cn : en;
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const load = useCallback(async (signal) => {
    setLoading(true);
    try {
      const { data: next } = await api.get('/mcp/paper/dashboard', { signal, params: { ...(namespace ? { namespace } : {}), ...(userId ? { user_id: userId } : {}), limit: 100 } });
      if (!signal?.aborted) { setData(next); setError(''); }
    } catch (err) { if (!signal?.aborted) setError(err.message); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [namespace, userId]);
  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve().then(() => { if (!controller.signal.aborted) load(controller.signal); });
    return () => controller.abort();
  }, [load, revision]);
  const format = (v) => v == null ? '—' : Number(v).toLocaleString(zh ? 'zh-CN' : 'en-US', { maximumFractionDigits: 8 });
  const time = (v) => v ? new Date(v * 1000).toLocaleString(zh ? 'zh-CN' : 'en-US', { timeZone: 'Asia/Shanghai' }) : '—';
  const positions = (data?.accounts || []).flatMap((a) => [
    ...(a.positions || []).map((p) => ({ ...p, user_id: a.user_id, account_type: a.account_type, market_type: 'swap' })),
    ...(a.spot || []).map((p) => ({ ...p, user_id: a.user_id, account_type: a.account_type, market_type: 'spot', side: 'LONG' })),
  ]);
  const state = { open: label('待成交', 'Open'), filled: label('已成交', 'Filled'), cancelled: label('已撤销', 'Cancelled'), rejected: label('失败', 'Rejected') };
  const kinds = { order_filled: label('自动成交', 'Automatic fill'), liquidation: label('爆仓', 'Liquidation'), order_rejected: label('委托失败', 'Rejected order'), admin_adjustment: label('后台调整', 'Admin adjustment'), bot_created: label('Bot 开户', 'Bot created'), place_order: label('挂单', 'Order placed'), set_protection: label('设置止盈止损', 'Protection set'), borrow: label('借款', 'Borrow'), repay: label('还款', 'Repay') };
  const summary = data?.summary;
  return <Card size="small" title={label('模拟盘数据 · 持仓 / 委托 / 排行 / 事件', 'Paper data · positions / orders / ranking / events')} extra={<Button onClick={() => load()} loading={loading}>{label('刷新数据', 'Refresh data')}</Button>} style={{ marginTop: 16 }}>
    {error && <Alert type="error" title={error} style={{ marginBottom: 12 }} />}
    <Typography.Paragraph type="secondary">{label('使用交易所真实公开行情，资金与撮合为模拟。以下展示当前筛选返回的数据；排行榜按收益率计算，后台充值及贷款不计为盈利。时间为北京时间。', 'Quotes are real public exchange data; balances and execution are simulated. Summary covers returned filtered records. Ranking uses trading returns; top-ups and loans are not profit. Times are Asia/Shanghai.')}</Typography.Paragraph>
    {summary && <Space wrap style={{ marginBottom: 12 }}><Tag>{label('账户', 'Accounts')}: {data.account_total}</Tag><Tag>{label('现金', 'Cash')}: {format(summary.cash)}</Tag><Tag>{label('负债', 'Debt')}: {format(summary.debt)}</Tag><Tag>{label('净资产', 'Net equity')}: {format(summary.net_equity)}</Tag><Tag>{label('待成交', 'Open orders')}: {summary.open_orders}</Tag></Space>}
    <Tabs items={[
      { key: 'positions', label: label('持仓', 'Positions'), children: <Table size="small" loading={loading} rowKey={(r) => `${r.user_id}:${r.exchange}:${r.symbol}:${r.side}`} dataSource={positions} pagination={{ pageSize: 8 }} scroll={{ x: 1000 }} columns={[
        { title: label('账户', 'Account'), dataIndex: 'user_id', render: (v, r) => <Space><Typography.Text>{v}</Typography.Text>{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</Space> },
        { title: label('标的 / 来源', 'Symbol / source'), render: (_, r) => <Space direction="vertical" size={0}><Typography.Text>{r.symbol}</Typography.Text><Typography.Text type="secondary">{r.exchange} · {r.market_type}</Typography.Text></Space> },
        { title: label('方向', 'Side'), dataIndex: 'side' }, { title: label('数量（基础币）', 'Base quantity'), dataIndex: 'amount', render: format },
        { title: label('开仓 / 成本价', 'Entry / cost'), dataIndex: 'entry_price', render: format }, { title: label('观察价', 'Observed mark'), dataIndex: 'mark_price', render: format },
        { title: label('盈亏', 'PnL'), dataIndex: 'unrealized_pnl', render: format }, { title: label('杠杆', 'Leverage'), dataIndex: 'leverage', render: (v) => v ? `${v}×` : '—' },
        { title: label('行情时间', 'Mark time'), dataIndex: 'marked_at', render: time },
      ]} /> },
      { key: 'orders', label: label('挂单 / 止盈止损', 'Orders / TP / SL'), children: <Table size="small" rowKey="order_id" loading={loading} dataSource={data?.orders || []} pagination={{ pageSize: 8 }} scroll={{ x: 1050 }} columns={[
        { title: label('账户', 'Account'), dataIndex: 'user_id' }, { title: label('标的', 'Symbol'), dataIndex: 'symbol' }, { title: label('交易所', 'Exchange'), dataIndex: 'exchange' },
        { title: label('操作', 'Action'), dataIndex: 'action' }, { title: label('委托类型', 'Order type'), dataIndex: 'order_type' },
        { title: label('数量', 'Quantity'), render: (_, r) => r.close_all ? label('当前仓位全部', 'Current cycle all') : format(r.amount) },
        { title: label('委托 / 触发价', 'Limit / trigger'), render: (_, r) => format(r.price ?? r.trigger_price) },
        { title: label('预留金额', 'Reserved cash'), dataIndex: 'reserved_cash', render: format },
        { title: label('状态', 'State'), dataIndex: 'state', render: (v) => <Tag color={v === 'filled' ? 'green' : v === 'rejected' ? 'red' : v === 'open' ? 'blue' : 'default'}>{state[v] || v}</Tag> },
        { title: label('实际成交价', 'Fill price'), render: (_, r) => format(r.fill?.price) },
      ]} expandable={{ expandedRowRender: (r) => <Typography.Paragraph style={{ overflowWrap: 'anywhere' }}>{label('委托 ID', 'Order ID')}: {r.order_id} · OCO: {r.oco_group || '—'} · {r.reason || ''}</Typography.Paragraph> }} /> },
      { key: 'ranking', label: label('用户与 Bot 排行榜', 'Users and bot ranking'), children: <Table size="small" rowKey="participant_id" loading={loading} dataSource={data?.leaderboard || []} pagination={{ pageSize: 8 }} scroll={{ x: 620 }} columns={[
        { title: label('排名', 'Rank'), dataIndex: 'rank' }, { title: label('名称', 'Name'), dataIndex: 'name', render: (v, r) => <Space>{v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</Space> },
        { title: label('交易盈亏', 'Trading PnL'), dataIndex: 'pnl', render: format }, { title: label('收益率', 'Return'), dataIndex: 'return_pct', render: (v) => v == null ? '—' : `${format(v)}%` },
        { title: label('最早观察价时间', 'Oldest mark'), dataIndex: 'oldest_mark_at', render: time },
      ]} /> },
      { key: 'events', label: label('成交 / 爆仓事件', 'Fill / liquidation events'), children: <Table size="small" rowKey="id" loading={loading} dataSource={data?.events || []} pagination={{ pageSize: 8 }} scroll={{ x: 780 }} columns={[
        { title: 'ID', dataIndex: 'id' }, { title: label('时间', 'Time'), dataIndex: 'timestamp', render: time }, { title: label('用户', 'User'), dataIndex: 'user_id' },
        { title: label('事件', 'Event'), dataIndex: 'kind', render: (v) => <Tag color={v === 'liquidation' ? 'red' : v === 'order_filled' ? 'green' : 'default'}>{kinds[v] || v}</Tag> },
        { title: label('标的', 'Symbol'), render: (_, r) => r.payload?.symbol || r.payload?.fill?.symbol || '—' },
        { title: label('成交价', 'Fill price'), render: (_, r) => format(r.payload?.price ?? r.payload?.fill?.price) },
      ]} expandable={{ expandedRowRender: (r) => <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(r.payload, null, 2)}</pre> }} /> },
    ]} />
  </Card>;
}
