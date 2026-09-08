import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Table, Typography } from 'antd';
import { api } from '../lib/api';

const reason = (value) => (value || 'unknown').split(',').map((v) => ({
  agent_exit: '主动平仓', take_profit: '止盈', stop_loss: '止损', external_unknown_exit: '外部退出（原因未知）',
  emergency_exit: '保护性退出', local_stop_loss: '本地止损', local_take_profit: '本地止盈',
}[v] || v)).join(' + ');

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
    <Typography.Paragraph type="secondary">按最终平仓时间筛选；加仓、部分平仓在仓位归零前属于同一周期。金额为手续费前盈亏，缺失数据不记为零。</Typography.Paragraph>
    {error && <Alert type="error" message={error} />}
    <Table loading={loading} size="small" rowKey={(r) => r.position_id || `${r.opened_at}-${r.closed_at}`}
      dataSource={data?.positions || []} scroll={{ x: 850 }} columns={[
        { title: '方向', dataIndex: 'side' }, { title: '开仓', dataIndex: 'opened_at' }, { title: '归零', dataIndex: 'closed_at' },
        { title: '累计入场量', dataIndex: 'amount' }, { title: '入场均价', dataIndex: 'entry_price' },
        { title: '平仓均价', dataIndex: 'close_price' }, { title: '加仓次数', dataIndex: 'add_count' },
        { title: '退出原因', dataIndex: 'exit_reason', render: reason },
        { title: '盈亏', dataIndex: 'realized_pnl', render: (v, r) => v == null ? '未知' : `${v.toFixed(4)} ${r.settlement_currency || 'USDT'}` },
      ]} expandable={{ expandedRowRender: (r) => <>
        <Typography.Paragraph>已知手续费（空值不代表无费用）：{JSON.stringify(r.fees || {})} · 周期 ID：{r.position_id}</Typography.Paragraph>
        {(r.events || []).map((e) => <div key={e.trade_id}>{new Date(e.timestamp_ms).toLocaleString()} · {e.role === 'entry' ? '入场 / 加仓' : reason(e.role)} · {e.quantity} @ {e.price} · 剩余 {e.remaining_base}</div>)}
        <Typography.Paragraph>保护变化：{JSON.stringify(r.protection_history || [])}</Typography.Paragraph>
      </> }} />
    {data?.activity && <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{data.activity}</Typography.Paragraph>}
  </Card>;
}
