import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Col, Collapse, Descriptions, Row, Statistic, Table, Tag, Typography } from 'antd';
import { api } from '../lib/api';

const reason = (value) => (value || 'unknown').split(',').map((v) => ({
  agent_exit: '主动平仓', take_profit: '止盈', stop_loss: '止损', external_unknown_exit: '外部退出（原因未知）',
  emergency_exit: '保护性退出', local_stop_loss: '本地止损', local_take_profit: '本地止盈',
}[v] || v)).join(' + ');

const money = (value) => value == null ? '未知' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const feesText = (fees) => Object.entries(fees || {}).map(([currency, cost]) => `${money(cost)} ${currency}`).join(' · ') || '未知';
const dateText = (value) => value == null ? '尚未同步' : new Date(value).toLocaleString('zh-CN');

function Activity({ data }) {
  if (!data || typeof data !== 'object') return null;
  const cycles = data.position_cycles || {};
  return <section style={{ marginTop: 24 }}>
    <Typography.Title level={5}>最近 7 天成交活动</Typography.Title>
    <Typography.Paragraph type="secondary">按成交时间统计，与上方完整周期盈亏不可相加。数据截至同步时间。</Typography.Paragraph>
    <Row gutter={[16, 16]}>
      <Col xs={12} md={6}><Statistic title="已归属成交" value={data.fill_count} suffix="条" /></Col>
      <Col xs={12} md={6}><Statistic title="已确认盈亏（手续费前）" value={data.known_realized_pnl_before_fees} precision={4} /></Col>
      <Col xs={12} md={6}><Typography.Text type="secondary">已知手续费</Typography.Text><div>{feesText(data.fees_by_currency)}</div></Col>
      <Col xs={12} md={6}><Typography.Text type="secondary">7 天窗口覆盖</Typography.Text><div><Tag color={data.window_complete ? 'green' : 'orange'}>{data.window_complete ? '完整' : '尚未完整覆盖'}</Tag></div></Col>
    </Row>
    <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ marginTop: 20 }} items={[
      { key: 'pnl', label: '缺失盈亏', children: `${data.missing_pnl_count} 条` },
      { key: 'fee', label: '缺失手续费', children: `${data.missing_fee_count} 条` },
      { key: 'owner', label: '未知归属成交', children: `${data.unknown_owner_fill_count} 条` },
      { key: 'unmatched', label: '无法匹配成交', children: `${cycles.unmatched_fill_count} 条` },
      { key: 'excluded', label: '混合归属 / 不完整周期排除', children: `${cycles.excluded_cycle_count} 个` },
    ]} />
    <Collapse ghost items={[{ key: 'sync', label: '同步详情与未闭合周期', children: <>
      {(data.sync || []).length === 0 && <Typography.Text type="secondary">暂无同步记录</Typography.Text>}
      {(data.sync || []).map((item, index) => <div key={`${item.symbol}-${index}`} style={{ marginBottom: 12 }}>
        <strong>{item.symbol}</strong> <Tag color={item.complete ? 'green' : 'orange'}>{item.complete ? '最近同步成功' : '同步未完成'}</Tag>
        <div>最近尝试：{dateText(item.attempted_at)} · 请求 {item.calls ?? 0} 次</div>
        <div>成交覆盖：{(item.covered_intervals || []).map(([from, to]) => `${dateText(from)} — ${dateText(to)}`).join('；') || '未知'}</div>
        {item.error && <Alert type="error" message={item.error} />}
        <div>账户资金流水：{item.income_sync?.complete ? '同步成功' : '未完整同步'} · {dateText(item.income_sync?.from_ms)} — {dateText(item.income_sync?.through_ms)}</div>
      </div>)}
      <Typography.Paragraph type="secondary">未闭合周期是历史成交重建结果，不代表实时持仓。</Typography.Paragraph>
      {(cycles.open_cycles || []).length === 0 ? '无未闭合周期' : (cycles.open_cycles || []).map((cycle, index) => <div key={index}>{cycle.side} · 剩余数量 {cycle.remaining_base} · {cycle.foreign_entry ? '混合归属' : '本策略归属'}</div>)}
    </> }]} />
  </section>;
}

export default function PositionCycleHistory({ configId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!configId) return;
    setLoading(true);
    try { setData((await api.get('/history/position-cycles', { params: { config_id: configId } })).data); setError(''); }
    catch (e) { setError(e.response?.data?.detail || e.message); }
    finally { setLoading(false); }
  }, [configId]);
  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    window.addEventListener('crypto-agent-dashboard-refresh', load);
    return () => { window.clearTimeout(timer); window.removeEventListener('crypto-agent-dashboard-refresh', load); };
  }, [load]);
  return <Card title="最近 7 天持仓周期" extra={<Button onClick={load} loading={loading}>刷新历史</Button>}>
    <Typography.Paragraph type="secondary">按最终平仓时间筛选；加仓、部分平仓在仓位归零前属于同一周期。净盈亏扣除开、平仓手续费；费用缺失或涉及其他币种时标记未知。资金费不包含在内。</Typography.Paragraph>
    {error && <Alert type="error" message={error} />}
    <Table loading={loading} size="small" rowKey={(r) => r.position_id || `${r.opened_at}-${r.closed_at}`}
      dataSource={data?.positions || []} scroll={{ x: 850 }} columns={[
        { title: '方向', dataIndex: 'side' }, { title: '开仓', dataIndex: 'opened_at' }, { title: '归零', dataIndex: 'closed_at' },
        { title: '累计入场量', dataIndex: 'amount' }, { title: '入场均价', dataIndex: 'entry_price' },
        { title: '平仓均价', dataIndex: 'close_price' }, { title: '加仓次数', dataIndex: 'add_count' },
        { title: '退出原因', dataIndex: 'exit_reason', render: reason },
        { title: '手续费前盈亏', dataIndex: 'realized_pnl', render: (v, r) => `${money(v)} ${r.settlement_currency || 'USDT'}` },
        { title: '手续费', dataIndex: 'fees', render: (v, r) => <>{feesText(v)}{r.fees_complete === false && <Tag color="orange">不完整</Tag>}</> },
        { title: '净盈亏', dataIndex: 'net_realized_pnl', render: (v, r) => `${money(r.source === 'execution_position_history' ? v : r.realized_pnl)} ${r.settlement_currency || 'USDT'}` },
      ]} expandable={{ expandedRowRender: (r) => <>
        <Typography.Paragraph>已知手续费（空值不代表无费用）：{feesText(r.fees)} · 周期 ID：{r.position_id}</Typography.Paragraph>
        {(r.events || []).map((e) => <div key={e.trade_id}>{new Date(e.timestamp_ms).toLocaleString()} · {e.role === 'entry' ? '入场 / 加仓' : reason(e.role)} · {e.quantity} @ {e.price} · 剩余 {e.remaining_base}</div>)}
        <Typography.Paragraph>保护变化：{JSON.stringify(r.protection_history || [])}</Typography.Paragraph>
      </> }} />
    <Activity data={data?.activity} />
  </Card>;
}
