import { useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Select, Space, Table, Typography } from 'antd';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import { observationTime } from '../lib/positionManagement';

const number = (value, zh, suffix = '') => value == null || !Number.isFinite(Number(value)) ? (zh ? '暂不可统计' : 'Unavailable') : `${Number(value).toLocaleString(zh ? 'zh-CN' : 'en-US', { maximumFractionDigits: 4 })}${suffix}`;

function LedgerDetails({ data, locale }) {
  const zh = locale === 'zh';
  const ledger = data.ledger || {};
  const cycles = ledger.position_cycles || {};
  const closed = Array.isArray(data.closed_positions) ? data.closed_positions : data.closed_positions?.positions || cycles.completed || [];
  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text type="secondary">{zh ? '按成交时间与完整平仓计算的盈亏不可相加；详情是历史证据，当前持仓请看账户状态。' : 'Fill-based and completed-position profit must not be added. These are historical records; use account state for current exposure.'}</Typography.Text>
    <Descriptions column={{ xs: 1, sm: 2 }} size="small" items={[
      { key: 'coverage', label: zh ? '区间覆盖' : 'Window coverage', children: ledger.window_complete ? (zh ? '完整' : 'Complete') : (zh ? '未完整覆盖' : 'Incomplete') },
      { key: 'owner', label: zh ? '未知归属成交' : 'Unattributed fills', children: ledger.unknown_owner_fill_count ?? '—' },
      { key: 'fees', label: zh ? '已知手续费' : 'Known fees', children: Object.entries(ledger.fees_by_currency || {}).map(([asset, amount]) => `${number(amount, zh)} ${asset}`).join(' · ') || (zh ? '暂无已核验费用' : 'No verified fees') },
      { key: 'missing', label: zh ? '缺失盈亏 / 手续费' : 'Missing profit / fees', children: `${ledger.missing_pnl_count ?? '—'} / ${ledger.missing_fee_count ?? '—'}` },
    ]} />
    <Typography.Text strong>{zh ? '成交明细' : 'Fills'}</Typography.Text>
    <Table size="small" dataSource={ledger.recent_fills || []} rowKey={(row) => `${row.symbol}:${row.trade_id || row.id}:${row.timestamp_ms}`} pagination={{ pageSize: 5 }} scroll={{ x: 760 }} columns={[
      { title: zh ? '时间' : 'Time', render: (_, row) => observationTime(row.timestamp_ms || row.timestamp, locale) },
      { title: zh ? '标的' : 'Symbol', dataIndex: 'symbol' },
      { title: zh ? '方向 / 作用' : 'Side / role', render: (_, row) => `${row.side || row.pos_side || '—'} / ${row.role || '—'}` },
      { title: zh ? '数量' : 'Quantity', render: (_, row) => number(row.quantity ?? row.amount, zh) },
      { title: zh ? '价格' : 'Price', dataIndex: 'price', render: (value) => number(value, zh) },
      { title: zh ? '盈亏（手续费前）' : 'Profit before fees', dataIndex: 'realized_pnl', render: (value) => number(value, zh) },
    ]} />
    <details className="content-disclosure"><summary>{zh ? '平仓与周期记录' : 'Closed positions and cycles'}</summary><Table size="small" dataSource={closed} rowKey={(row, index) => row.position_id || `${row.opened_at}:${index}`} scroll={{ x: 640 }} pagination={{ pageSize: 5 }} columns={[
      { title: zh ? '方向' : 'Side', dataIndex: 'side' }, { title: zh ? '开仓' : 'Opened', dataIndex: 'opened_at' }, { title: zh ? '平仓' : 'Closed', dataIndex: 'closed_at' }, { title: zh ? '净盈亏' : 'Net profit', dataIndex: 'net_realized_pnl', render: (value) => number(value, zh) },
    ]} /><Evidence value={cycles} zh={zh} /></details>
    <details className="content-disclosure"><summary>{zh ? '同步、归属与完整审计证据' : 'Synchronization, attribution and full audit evidence'}</summary><Evidence value={data} zh={zh} /></details>
  </Space>;
}

function Evidence({ value, zh }) {
  return <pre aria-label={zh ? '历史账本证据' : 'Historical ledger evidence'} tabIndex={0} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 460, overflow: 'auto', fontSize: 12 }}>{JSON.stringify(value, null, 2)}</pre>;
}

export default function PerformancePanel({ configId }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [hours, setHours] = useState(168);
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState(null);
  const [detailError, setDetailError] = useState('');
  const identity = `${configId}:${hours}:${refresh}`;
  const current = result?.identity === identity ? result.performance : null;
  const currentDetails = details?.identity === identity ? details.data : null;
  useEffect(() => {
    const controller = new AbortController();
    api.get(`/stats/performance/${encodeURIComponent(configId)}`, { params: { hours }, signal: controller.signal, silent: true }).then(({ data }) => { setResult({ identity, performance: data.performance }); setError(''); }).catch((err) => { if (err.code !== 'ERR_CANCELED') setError(err.message); });
    return () => controller.abort();
  }, [configId, hours, identity]);
  useEffect(() => {
    const update = () => setRefresh((old) => old + 1);
    window.addEventListener('crypto-agent-dashboard-refresh', update);
    return () => window.removeEventListener('crypto-agent-dashboard-refresh', update);
  }, []);
  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    api.get(`/stats/performance/${encodeURIComponent(configId)}/details`, { params: { hours }, signal: controller.signal, silent: true }).then(({ data }) => { setDetails({ identity, data: data.details }); setResult({ identity, performance: data.performance }); setDetailError(''); }).catch((err) => { if (err.code !== 'ERR_CANCELED') setDetailError(err.message); });
    return () => controller.abort();
  }, [configId, hours, identity, open]);
  const equity = current?.equity;
  const profit = current?.profit;
  const drawdown = current?.drawdown;
  return <Card className="panel-card" title={zh ? '收益与回撤' : 'Performance'} loading={!current && !error} extra={<Button size="small" onClick={() => setRefresh((old) => old + 1)}>{zh ? '刷新' : 'Refresh'}</Button>}>
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Select aria-label={zh ? '收益统计区间' : 'Performance window'} value={hours} style={{ width: 160 }} options={[24, 168, 720].map((value) => ({ value, label: zh ? `最近 ${value / 24} 天` : `Last ${value / 24} days` }))} onChange={setHours} />
      {error && <Alert type="warning" title={error} />}
      <div className="dashboard-fact-grid">
        <div><Typography.Text type="secondary">{zh ? '权益变化' : 'Equity change'}</Typography.Text><div>{equity?.available ? number(equity.change, zh, ` ${equity.currency || 'USDT'}`) : number(null, zh)}</div>{equity?.available && <Typography.Text type="secondary">{number(equity.start?.equity ?? equity.start, zh)} → {number(equity.latest?.equity ?? equity.latest, zh)} · {number(equity.return_pct, zh, '%')}</Typography.Text>}</div>
        <div><Typography.Text type="secondary">{zh ? '已实现盈亏' : 'Realized profit'}</Typography.Text><div>{profit?.available ? number(profit.amount, zh, ` ${profit.currency || 'USDT'}`) : number(null, zh)}</div><Typography.Text type="secondary">{profit?.basis === 'mock_realized_pnl' ? (zh ? '模拟已实现盈亏' : 'Simulated realized profit') : (zh ? '已归属成交，扣除已知计价币手续费' : 'Attributed fills, after known quote-currency fees')}</Typography.Text></div>
        <div><Typography.Text type="secondary">{zh ? '回撤' : 'Drawdown'}</Typography.Text><div>{zh ? '当前' : 'Current'} {drawdown?.available ? number(drawdown.current_pct, zh, '%') : number(null, zh)}</div><Typography.Text type="secondary">{zh ? '区间最大' : 'Window maximum'} {drawdown?.available ? number(drawdown.max_pct, zh, '%') : number(null, zh)}</Typography.Text></div>
      </div>
      <Typography.Text type="secondary">{zh ? '本地权益快照，非实时余额；账户权益变化未剔除出入金与其他策略。' : 'Local equity snapshots, not live balance; equity change includes transfers and other strategies.'} {zh ? '截至' : 'As of'} {observationTime(current?.as_of, locale)}</Typography.Text>
      <details className="content-disclosure" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>{zh ? '查看账本详情' : 'View ledger details'}</summary>
        {open && (detailError ? <Alert type="error" title={detailError} action={<Button onClick={() => setRefresh((old) => old + 1)}>{zh ? '重试' : 'Retry'}</Button>} /> : currentDetails ? <LedgerDetails data={currentDetails} locale={locale} /> : <Typography.Text>{zh ? '正在读取详情…' : 'Loading details…'}</Typography.Text>)}
      </details>
    </Space>
  </Card>;
}
