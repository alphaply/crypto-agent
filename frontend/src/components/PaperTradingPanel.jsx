import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, Input, InputNumber, Modal, Space, Switch, Table, Tag, Typography } from 'antd';
import { api } from '../lib/api';
import { formatPaperAmount, paperFilterCount, paperValuationIncomplete } from '../lib/paperDisplay';
import AuditDisclosure from './AuditDisclosure';
import PaperDataPanel from './PaperDataPanel';

export default function PaperTradingPanel({ settings: initial, zh, onChanged, onSettingsSaved, onCreateKey }) {
  const label = (cn, en) => zh ? cn : en;
  const [draft, setDraft] = useState(null);
  const settings = draft || initial;
  const [accounts, setAccounts] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [userId, setUserId] = useState('');
  const [namespace, setNamespace] = useState('qqbot');
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const load = useCallback(async (target = 1, signal) => {
    try {
      const { data } = await api.get('/mcp/paper/accounts', { signal, params: { limit: 20, offset: (target - 1) * 20, ...(userId ? { user_id: userId } : {}), ...(namespace ? { namespace } : {}) } });
      if (signal?.aborted) return;
      setAccounts(data.items); setTotal(data.total); setPage(target); setError('');
    } catch (err) { if (!signal?.aborted) setError(err.message); }
  }, [userId, namespace]);
  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve().then(() => { if (!controller.signal.aborted) load(1, controller.signal); });
    return () => controller.abort();
  }, [load]);
  const mutate = async (action) => {
    setBusy(true);
    try { await action(); await load(page); await onChanged(); setRevision((v) => v + 1); setError(''); return true; }
    catch (err) { setError(err.message); return false; }
    finally { setBusy(false); }
  };
  const saveSettings = async () => {
    const submitted = settings;
    setBusy(true);
    setError('');
    try {
      const { data } = await api.put('/mcp/paper/settings', { ...submitted, exchange: 'binance' });
      onSettingsSaved(data.settings);
      // A new edit made while saving belongs to the next save.
      setDraft((current) => current === submitted ? null : current);
      await load(page);
      setRevision((v) => v + 1);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const update = (field, value) => setDraft({ ...settings, [field]: value });
  const edit = (field, value) => setEditing((v) => ({ ...v, [field]: value }));
  const path = (row) => `/mcp/paper/accounts/${encodeURIComponent(row.namespace)}/${encodeURIComponent(row.user_id)}`;
  const format = (v) => formatPaperAmount(v, zh);
  if (!settings) return null;
  const moneyFields = { cash: label('账本现金 · USDT', 'Ledger cash · USDT'), debt: label('负债 · USDT', 'Debt · USDT') };
  return <Card className="panel-card" title={label('QQBot 模拟交易 · Paper', 'QQBot paper trading')} extra={<Button type="primary" onClick={onCreateKey}>{label('创建 Paper Key', 'Create paper key')}</Button>}>
    <Typography.Paragraph type="secondary">{label('仅交易 Binance 的 USDT 现货和线性永续，按用户 ID 独立记账。其他交易所及来源不明的历史资源从默认列表过滤，原账本和已占用资金保留。市价成交使用真实公开买卖价；逐仓爆仓后贷款仍保留。', 'Trade only Binance USDT spot and linear perpetuals, with accounts by user ID. Historical resources from other or unresolved venues are filtered from default lists; their ledger and financial commitments remain. Market fills use real public bid/ask; isolated liquidation retains debt.')}</Typography.Paragraph>
    {error && <Alert type="error" title={error} style={{ marginBottom: 12 }} />}
    <div className="settings-form-grid">
      <div className="form-field"><label>{label('启用模拟交易', 'Enable paper trading')}</label><Switch checked={settings.enabled} onChange={(v) => update('enabled', v)} /></div>
      <div className="form-field"><label>{label('真实公开行情来源', 'Real public market source')}</label><Typography.Text>Binance</Typography.Text></div>
      <div className="form-field"><label>{label('Bot 账户 ID', 'Bot account ID')}</label><Input value={settings.bot_user_id || 'bot:xiaojin-mengzhizi'} onChange={(e) => update('bot_user_id', e.target.value)} /></div>
      <div className="form-field"><label>{label('Bot 默认名称', 'Default bot name')}</label><Input value={settings.bot_name || '小金萌智子'} onChange={(e) => update('bot_name', e.target.value)} /></div>
      <div className="form-field"><label>{label('银行允许借款', 'Allow borrowing')}</label><Switch checked={settings.bank_enabled} onChange={(v) => update('bank_enabled', v)} /></div>
      {[
        ['daily_interest_rate', label('日利率（0.01 = 1%）', 'Daily interest rate (0.01 = 1%)'), 0, undefined, .0001],
        ['initial_balance', label('新账户初始金额 · USDT', 'New account initial cash · USDT'), 0, 1e12, 100],
        ['max_leverage', label('杠杆上限', 'Leverage cap'), 1, 125, 1],
        ['loan_limit', label('银行统一借款上限 · USDT', 'Bank loan limit · USDT'), 0, 1e12, 100],
        ['fee_rate', label('手续费比例（0.001 = 0.1%）', 'Fee rate (0.001 = 0.1%)'), 0, .01, .0001],
        ['maintenance_margin_rate', label('维持保证金比例（0.005 = 0.5%）', 'Maintenance rate (0.005 = 0.5%)'), .000001, .1, .001],
      ].map(([field, title, min, max, step]) => <div className="form-field" key={field}><label>{title}</label><InputNumber min={min} max={max} step={step} precision={field === 'max_leverage' ? 0 : undefined} value={settings[field]} onChange={(v) => update(field, v ?? min)} /></div>)}
    </div>
    <Button style={{ marginTop: 12 }} loading={busy} onClick={saveSettings}>{label('保存模拟交易设置', 'Save paper settings')}</Button>
    <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>{label('初始金额作用于开户和下海重置。借款额度与日利率统一配置，每日北京时间 00:00 按本金单利扣息；现金可以扣负。破产账户仅能收款，用户主动说下海才会重置。杠杆调整只作用于后续开仓。后台分批更新 BN 资源；估值不完整时净资产显示为 —，不参加对应收益排名。可用现金扣除完整账本中的预留。', 'Cash applies to creation/reset. Bank limits and daily simple interest are central. Interest settles at midnight Beijing time and may make cash negative. Bankrupt users may receive funds and must explicitly request a reset. Leverage changes affect subsequent entries only. Background batches update Binance resources. Incomplete equity is shown as — and excluded from the corresponding return ranking. Available cash subtracts all ledger reservations.')}</Typography.Paragraph>
    <Space wrap style={{ marginBottom: 12 }}>
      <Input aria-label={label('账户命名空间', 'Account namespace')} placeholder={label('命名空间', 'Namespace')} value={namespace} onChange={(e) => setNamespace(e.target.value)} style={{ width: 150 }} />
      <Input aria-label={label('用户 ID', 'User ID')} placeholder={label('QQ 用户 ID（完整匹配）', 'QQ user ID (exact match)')} value={userId} onChange={(e) => setUserId(e.target.value)} style={{ width: 220 }} />
      <Button onClick={() => load(1)}>{label('刷新账户', 'Refresh accounts')}</Button>
      <Button disabled={!namespace || !userId} loading={busy} onClick={() => mutate(() => api.post(path({ namespace, user_id: userId })))}>{label('按 ID 创建账户', 'Create account by ID')}</Button>
    </Space>
    <Table size="small" rowKey={(row) => `${row.namespace}:${row.user_id}`} dataSource={accounts} scroll={{ x: 1100 }} pagination={{ current: page, pageSize: 20, total, onChange: (target) => load(target) }} columns={[
      { title: label('用户 ID', 'User ID'), dataIndex: 'user_id', render: (v, r) => <Space direction="vertical" size={0}><Typography.Text>{v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</Typography.Text><Typography.Text type="secondary">{r.name} · {r.namespace}</Typography.Text></Space> },
      { title: label('状态 / 下海次数', 'Status / resets'), render: (_, r) => <Space><Tag color={r.account_status === 'bankrupt' ? 'red' : 'green'}>{r.account_status === 'bankrupt' ? label('破产', 'Bankrupt') : label('正常', 'Active')}</Tag>{r.reset_count ?? 0} · {label('周期', 'Cycle')} {r.account_epoch ?? 0}</Space> },
      { title: label('账本现金', 'Ledger cash'), dataIndex: 'cash', render: format },
      { title: label('可用现金', 'Available cash'), dataIndex: 'available_cash', render: format },
      { title: label('保证金', 'Margin'), dataIndex: 'used_margin', render: format },
      { title: label('负债', 'Debt'), dataIndex: 'debt', render: format },
      { title: label('净资产', 'Net equity'), dataIndex: 'net_equity', render: (v, r) => <Space direction="vertical" size={0}>{format(v)}{paperValuationIncomplete(r) && <Tag color="orange">{label('估值不完整', 'Incomplete valuation')}</Tag>}</Space> },
      { title: label('历史过滤', 'History filtering'), render: (_, r) => paperFilterCount(r.filter) ? <Tag>{label('已过滤', 'Filtered')}: {paperFilterCount(r.filter)}</Tag> : '—' },
      { title: label('杠杆 / 市场', 'Leverage / markets'), render: (_, r) => <Space wrap><Tag>{r.leverage}×</Tag>{r.markets.map((v) => <Tag key={v}>{v}</Tag>)}{!r.enabled && <Tag>{label('暂停', 'Paused')}</Tag>}</Space> },
      { title: label('操作', 'Actions'), render: (_, r) => <Button size="small" onClick={() => setEditing({ namespace: r.namespace, user_id: r.user_id, name: r.name, cash: r.cash, debt: r.debt, leverage: r.leverage, markets: r.markets, enabled: r.enabled, leaderboard_visible: r.leaderboard_visible ?? true, notifications_enabled: r.notifications_enabled ?? true })}>{label('调整账户', 'Adjust account')}</Button> },
    ]} expandable={{ expandedRowRender: (r) => <>
      {paperFilterCount(r.filter) > 0 && <Typography.Paragraph>{label('被过滤历史的资金承诺仍保留。预留现金', 'Filtered history retains its financial commitments. Reserved cash')}: {format(r.filter.retained_reserved_cash)} USDT · {label('保证金', 'Margin')}: {format(r.filter.retained_margin)} USDT</Typography.Paragraph>}
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify({ spot: r.spot, positions: r.positions, realized_pnl: r.realized_pnl, fees: r.fees, oldest_mark_at: r.oldest_mark_at, completeness: r.completeness, filter: r.filter }, null, 2)}</pre>
      <AuditDisclosure url={`/mcp/paper/accounts/${encodeURIComponent(r.namespace)}/${encodeURIComponent(r.user_id)}/audit`} zh={zh} title={label('原始账本审计（含已过滤历史）', 'Original ledger audit (including filtered history)')} views={false} />
    </> }} />
    <PaperDataPanel namespace={namespace} userId={userId} revision={revision} zh={zh} />
    <Modal open={Boolean(editing)} title={label(`调整模拟账户 · ${editing?.user_id || ''}`, `Adjust paper account · ${editing?.user_id || ''}`)} onCancel={() => setEditing(null)} confirmLoading={busy} okButtonProps={{ disabled: !editing?.markets?.length }} onOk={async () => {
      const { namespace: ns, user_id: id, ...payload } = editing;
      if (await mutate(() => api.patch(path({ namespace: ns, user_id: id }), payload))) setEditing(null);
    }}>
      {editing && <div className="settings-form-grid">
        {error && <Alert type="error" title={error} />}
        <div className="form-field"><label>{label('名称', 'Name')}</label><Input value={editing.name} onChange={(e) => edit('name', e.target.value)} /></div>
        {Object.entries(moneyFields).map(([field, title]) => <div className="form-field" key={field}><label>{title}</label><InputNumber aria-label={field} min={field === 'cash' ? -1e12 : 0} max={1e12} value={editing[field]} onChange={(v) => edit(field, v ?? 0)} /></div>)}
        <div className="form-field"><label>{label('后续开仓杠杆', 'Leverage for new entries')}</label><InputNumber min={1} max={settings.max_leverage} precision={0} value={editing.leverage} onChange={(v) => edit('leverage', v ?? 1)} /></div>
        <div className="form-field"><label>{label('启用账户', 'Enable account')}</label><Switch checked={editing.enabled} onChange={(v) => edit('enabled', v)} /></div>
        <div className="form-field"><label>{label('参与排行榜', 'Leaderboard participation')}</label><Switch checked={editing.leaderboard_visible} onChange={(v) => edit('leaderboard_visible', v)} /></div>
        <div className="form-field"><label>{label('成交与爆仓提醒', 'Fill and liquidation notifications')}</label><Switch checked={editing.notifications_enabled} onChange={(v) => edit('notifications_enabled', v)} /></div>
        <div className="form-field"><label>{label('允许市场', 'Allowed markets')}</label><Checkbox.Group value={editing.markets} options={[{ value: 'spot', label: label('现货', 'Spot') }, { value: 'swap', label: label('合约', 'Perpetual') }]} onChange={(v) => edit('markets', v)} /></div>
      </div>}
    </Modal>
  </Card>;
}
