import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Space, Switch, Table, Tag, Typography } from 'antd';
import { BankOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, RobotOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { formatPaperAmount, paperFilterCount, paperValuationIncomplete } from '../lib/paperDisplay';
import AuditDisclosure from './AuditDisclosure';
import PaperDataPanel from './PaperDataPanel';
import './PaperTradingPanel.css';

export default function PaperTradingPanel({ settings: initial, zh, onChanged, onSettingsSaved, onCreateKey }) {
  const label = (cn, en) => zh ? cn : en;
  const [draft, setDraft] = useState(null);
  const settings = draft || initial;
  const [accounts, setAccounts] = useState([]);
  const [total, setTotal] = useState(null);
  const [page, setPage] = useState(1);
  const [userId, setUserId] = useState('');
  const [namespace, setNamespace] = useState('qqbot');
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const load = useCallback(async (target = 1, signal) => {
    setLoading(true);
    try {
      const { data } = await api.get('/mcp/paper/accounts', { signal, params: { limit: 20, offset: (target - 1) * 20, ...(userId ? { user_id: userId } : {}), ...(namespace ? { namespace } : {}) } });
      if (signal?.aborted) return;
      setAccounts(data.items); setTotal(data.total); setPage(target); setError('');
    } catch (err) { if (!signal?.aborted) setError(err.message); }
    finally { if (!signal?.aborted) setLoading(false); }
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
      // Keep any draft edited while this request was in flight.
      setDraft((current) => current === submitted ? null : current);
      await load(page);
      setRevision((v) => v + 1);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const update = (field, value) => setDraft({ ...settings, [field]: value });
  const edit = (field, value) => setEditing((v) => ({ ...v, [field]: value }));
  const path = (row) => '/mcp/paper/accounts/' + encodeURIComponent(row.namespace) + '/' + encodeURIComponent(row.user_id);
  const format = (v) => formatPaperAmount(v, zh);
  const number = (v) => <span className="paper-number">{format(v)}</span>;
  const moneyFields = { cash: label('账本现金 · USDT', 'Ledger cash · USDT'), debt: label('负债 · USDT', 'Debt · USDT') };
  const numericField = (field, title, min, max, step) => <div className="form-field" key={field}><label htmlFor={'paper-setting-' + field}>{title}</label><InputNumber id={'paper-setting-' + field} min={min} max={max} step={step} precision={field === 'max_leverage' ? 0 : undefined} value={settings[field]} onChange={(v) => update(field, v ?? min)} /></div>;
  const openEditor = (r) => setEditing({ namespace: r.namespace, user_id: r.user_id, name: r.name, cash: r.cash, debt: r.debt, leverage: r.leverage, markets: r.markets, enabled: r.enabled, leaderboard_visible: r.leaderboard_visible ?? true, notifications_enabled: r.notifications_enabled ?? true });
  if (!settings) return null;
  return <div className="paper-workspace">
    <header className="paper-hero">
      <div className="paper-hero-main">
        <div className="paper-hero-icon"><RobotOutlined /></div>
        <div><div className="paper-eyebrow">QQBOT · PAPER</div><h2>{label('模拟交易工作台', 'Paper trading workspace')}</h2><p>{label('查看账户资金、持仓和委托，集中管理机器人交易。', 'Monitor balances, positions and orders in one place.')}</p></div>
      </div>
      <div className="paper-hero-actions">
        <Tag className="paper-service-tag" color={initial?.enabled ? 'green' : 'default'}>{initial?.enabled ? label('服务已启用', 'Service enabled') : label('服务已暂停', 'Service paused')}</Tag>
        <Button type="primary" icon={<KeyOutlined />} onClick={onCreateKey}>{label('创建 Paper Key', 'Create paper key')}</Button>
      </div>
    </header>
    {error && <Alert type="error" showIcon title={error} />}
    <div className="paper-filter-bar">
      <div className="paper-filter-title"><SearchOutlined /><span>{label('查看范围', 'View scope')}</span></div>
      <div className="paper-filter-field"><label htmlFor="paper-namespace">{label('命名空间', 'Namespace')}</label><Input id="paper-namespace" aria-label={label('账户命名空间', 'Account namespace')} placeholder={label('全部命名空间', 'All namespaces')} value={namespace} onChange={(e) => setNamespace(e.target.value)} allowClear /></div>
      <div className="paper-filter-field paper-filter-user"><label htmlFor="paper-user">{label('用户 ID', 'User ID')}</label><Input id="paper-user" aria-label={label('用户 ID', 'User ID')} placeholder={label('全部账户 / 输入完整 QQ 用户 ID', 'All accounts / exact QQ user ID')} value={userId} onChange={(e) => setUserId(e.target.value)} allowClear /></div>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={async () => { await load(1); setRevision((v) => v + 1); }}>{label('刷新', 'Refresh')}</Button>
    </div>

    <PaperDataPanel namespace={namespace} userId={userId} revision={revision} zh={zh} maxLeverage={initial.max_leverage} onChanged={() => load(page)} />

    <section className="paper-accounts-section">
      <div className="paper-section-heading">
        <div><h3>{label('账户管理', 'Account management')}<span className="paper-heading-count">{format(total)}</span></h3><p>{label('查看独立账本，展开账户了解持仓、付息提醒与审计记录。', 'Independent ledgers. Expand an account for holdings, interest and audit details.')}</p></div>
        <Button icon={<PlusOutlined />} disabled={!namespace || !userId} loading={busy} onClick={() => mutate(() => api.post(path({ namespace, user_id: userId })))}>{label('按筛选 ID 开户', 'Create by selected ID')}</Button>
      </div>
      <Card size="small" className="paper-records-card">
        <Table size="small" loading={loading} rowKey={(row) => row.namespace + ':' + row.user_id} dataSource={accounts} scroll={{ x: 1160 }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={label('当前范围暂无账户；填写命名空间和用户 ID 可开户。', 'No matching accounts. Enter a namespace and user ID to create one.')} /> }} pagination={{ current: page, pageSize: 20, total: total ?? undefined, hideOnSinglePage: true, showSizeChanger: false, onChange: (target) => load(target) }} columns={[
          { title: label('账户', 'Account'), dataIndex: 'user_id', width: 210, render: (v, r) => <div className="paper-cell-stack"><strong>{r.name || v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</strong><span className="paper-cell-note">{v}</span><span className="paper-cell-note">{r.namespace}</span></div> },
          { title: label('状态', 'Status'), width: 115, render: (_, r) => <div className="paper-cell-stack"><Tag color={r.account_status === 'bankrupt' ? 'red' : r.account_status === 'active' ? 'green' : 'default'}>{r.account_status === 'bankrupt' ? label('破产冻结', 'Bankrupt') : r.account_status === 'active' ? label('正常', 'Active') : r.account_status || '—'}</Tag>{!r.enabled && <span className="paper-cell-note">{label('交易暂停', 'Trading paused')}</span>}<span className="paper-cell-note">{label('下海 ', 'Resets ')}{format(r.reset_count)}</span></div> },
          { title: label('账本现金', 'Ledger cash'), dataIndex: 'cash', render: number, align: 'right' },
          { title: label('可用现金', 'Available cash'), dataIndex: 'available_cash', render: number, align: 'right' },
          { title: label('保证金', 'Margin'), dataIndex: 'used_margin', render: number, align: 'right' },
          { title: label('负债', 'Debt'), dataIndex: 'debt', render: number, align: 'right' },
          { title: label('净资产', 'Net equity'), dataIndex: 'net_equity', align: 'right', render: (v, r) => <div className="paper-cell-stack paper-align-right"><strong className={'paper-number' + (v != null && v < 0 ? ' paper-negative' : '')}>{format(v)}</strong>{paperValuationIncomplete(r) && <Tag color="orange">{label('估值不完整', 'Incomplete')}</Tag>}</div> },
          { title: label('默认杠杆 / 市场', 'Default leverage / markets'), width: 145, render: (_, r) => <div className="paper-cell-stack"><span className="paper-number">{r.leverage != null ? format(r.leverage) + '×' : '—'}</span><span className="paper-cell-note">{r.markets?.map((v) => v === 'spot' ? label('现货', 'Spot') : v === 'swap' ? label('永续', 'Perpetual') : v).join(' · ') || '—'}</span>{paperFilterCount(r.filter) > 0 && <Tag>{label('历史过滤 ', 'Filtered ')}{paperFilterCount(r.filter)}</Tag>}</div> },
          { title: label('操作', 'Actions'), width: 105, render: (_, r) => <Button size="small" onClick={() => openEditor(r)}>{label('调整账户', 'Adjust')}</Button> },
        ]} expandable={{ expandedRowRender: (r) => <div className="paper-account-detail">
          <div className="paper-detail-facts">
            {[[label('已实现盈亏', 'Realized PnL'), r.realized_pnl, 'USDT'], [label('本轮手续费', 'Cycle fees'), r.fees, 'USDT'], [label('本轮已付利息', 'Cycle interest paid'), r.interest_paid, 'USDT'], [label('账户周期', 'Account cycle'), r.account_epoch, '']].map(([title, value, unit]) => <div key={title}><span>{title}</span><strong className="paper-number">{format(value)} <small>{unit}</small></strong></div>)}
          </div>
          {r.interest_reserve?.message && <Alert type={r.interest_reserve.shortfall > 0 ? 'warning' : 'info'} showIcon title={label('付息资金提醒', 'Interest cash reminder')} description={r.interest_reserve.message} />}
          {paperFilterCount(r.filter) > 0 && <Alert type="info" showIcon title={label('历史资源已过滤，资金占用仍保留', 'Filtered history retains financial commitments')} description={label('预留现金：', 'Reserved cash: ') + format(r.filter.retained_reserved_cash) + ' USDT · ' + label('保证金：', 'Margin: ') + format(r.filter.retained_margin) + ' USDT'} />}
          <div className="paper-account-holdings">
            {[...(r.positions || []).map((p) => ({ ...p, kind: 'swap' })), ...(r.spot || []).map((p) => ({ ...p, kind: 'spot' }))].map((holding, index) => <div className="paper-holding" key={holding.resource_id || holding.episode_id || [holding.exchange, holding.symbol, holding.side, index].join(':')}>
              <div><strong>{holding.symbol}</strong><Tag>{holding.kind === 'spot' ? label('现货', 'Spot') : label('永续', 'Perpetual')}{holding.side ? ' · ' + (holding.side === 'LONG' ? label('多', 'Long') : label('空', 'Short')) : ''}</Tag></div>
              <span>{label('数量 ', 'Quantity ')}<b className="paper-number">{format(holding.amount)}</b></span><span>{label('成本价 ', 'Entry ')}<b className="paper-number">{format(holding.entry_price)}</b></span>
            </div>)}
            {!r.positions?.length && !r.spot?.length && <span className="paper-cell-note">{label('当前无可见持仓', 'No visible positions')}</span>}
          </div>
          <AuditDisclosure url={path(r) + '/audit'} zh={zh} title={label('查看原始账本审计（含已过滤历史）', 'Original ledger audit (including filtered history)')} views={false} />
        </div> }} />
      </Card>
    </section>

    <Collapse className="paper-settings-collapse" items={[{ key: 'settings', label: <div className="paper-settings-label"><SettingOutlined /><span>{label('交易与银行设置', 'Trading & bank settings')}</span>{draft && <Tag color="orange">{label('有未保存修改', 'Unsaved changes')}</Tag>}</div>, extra: <span className="paper-cell-note">{label('开户资金、利率、杠杆上限', 'Initial cash, interest & leverage cap')}</span>, children: <>
      <div className="paper-settings-groups">
        <section><h4><RobotOutlined />{label('服务与机器人', 'Service & bot')}</h4><div className="settings-form-grid">
          <div className="form-field"><label>{label('启用模拟交易', 'Enable paper trading')}</label><Switch aria-label={label('启用模拟交易', 'Enable paper trading')} checked={settings.enabled} onChange={(v) => update('enabled', v)} /></div>
          <div className="form-field"><label>{label('真实公开行情', 'Public market source')}</label><Typography.Text>Binance · USDT</Typography.Text></div>
          <div className="form-field"><label htmlFor="paper-bot-id">{label('Bot 账户 ID', 'Bot account ID')}</label><Input id="paper-bot-id" value={settings.bot_user_id} onChange={(e) => update('bot_user_id', e.target.value)} /></div>
          <div className="form-field"><label htmlFor="paper-bot-name">{label('Bot 默认名称', 'Default bot name')}</label><Input id="paper-bot-name" value={settings.bot_name} onChange={(e) => update('bot_name', e.target.value)} /></div>
          {numericField('initial_balance', label('开户 / 重置资金 · USDT', 'Creation / reset cash · USDT'), 0, 1e12, 100)}
        </div></section>
        <section><h4><BankOutlined />{label('银行与借款', 'Bank & loans')}</h4><div className="settings-form-grid">
          <div className="form-field"><label>{label('允许新增借款', 'Allow new loans')}</label><Switch aria-label={label('允许新增借款', 'Allow new loans')} checked={settings.bank_enabled} onChange={(v) => update('bank_enabled', v)} /></div>
          {numericField('daily_interest_rate', label('日利率（0.01 = 1%）', 'Daily rate (0.01 = 1%)'), 0, undefined, .0001)}
          {numericField('loan_limit', label('统一借款上限 · USDT', 'Loan limit · USDT'), 0, 1e12, 100)}
        </div><p>{label('按本金单利计息，每日北京时间 00:00 扣息。实际现金不足时可扣为负数并冻结；挂单占用不会单独触发破产。', 'Simple interest on principal, settled at midnight Beijing time. A real cash shortfall can make cash negative and freeze the account; reservations alone do not trigger bankruptcy.')}</p></section>
        <section><h4><SettingOutlined />{label('交易规则', 'Trading rules')}</h4><div className="settings-form-grid">
          {numericField('max_leverage', label('杠杆上限', 'Leverage cap'), 1, 125, 1)}
          {numericField('fee_rate', label('手续费比例（0.001 = 0.1%）', 'Fee rate (0.001 = 0.1%)'), 0, .01, .0001)}
          {numericField('maintenance_margin_rate', label('维持保证金比例（0.005 = 0.5%）', 'Maintenance rate (0.005 = 0.5%)'), .000001, .1, .001)}
        </div><p>{label('仅支持 Binance 的 USDT 现货与线性永续。账户默认杠杆用于后续开仓，已有仓位保持各自参数。', 'Binance USDT spot and linear perpetuals only. Account default leverage applies to new entries; existing positions retain their parameters.')}</p></section>
      </div>
      <div className="paper-settings-save"><span>{label('设置保存后生效；开户资金仅用于新账户与下海重置。', 'Saved settings apply going forward; initial cash is used for creation and resets.')}</span><Button type="primary" loading={busy} onClick={saveSettings}>{label('保存设置', 'Save settings')}</Button></div>
    </> }]} />

    <Modal open={Boolean(editing)} title={label('调整模拟账户 · ', 'Adjust paper account · ') + (editing?.user_id || '')} onCancel={() => setEditing(null)} confirmLoading={busy} okButtonProps={{ disabled: !editing?.markets?.length }} onOk={async () => {
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
  </div>;
}
