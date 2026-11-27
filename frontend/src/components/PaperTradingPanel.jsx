import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, InputNumber, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, Tooltip, Typography, message } from 'antd';
import { ApiOutlined, BankOutlined, CopyOutlined, KeyOutlined, LinkOutlined, PlusOutlined, ReloadOutlined, RobotOutlined, SearchOutlined, SettingOutlined, ToolOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { useLatestRequest } from '../hooks/useLatestRequest';
import { formatPaperAmount, paperFilterCount, paperValuationIncomplete, paperValuationReasons } from '../lib/paperDisplay';
import AuditDisclosure from './AuditDisclosure';
import McpToolCatalog from './McpToolCatalog';
import PaperDataPanel from './PaperDataPanel';
import './PaperTradingPanel.css';

const { Text, Paragraph } = Typography;

function formatPercentHint(value) {
  if (value == null || !Number.isFinite(Number(value))) return '';
  return `${Number((Number(value) * 100).toPrecision(6))}%`;
}

export default function PaperTradingPanel({
  settings: initial,
  mcpSettings,
  connection,
  keys = [],
  tools = [],
  busy: mcpBusy = false,
  zh,
  onSettingsSaved,
  onCreateKey,
  onRevokeKey,
  onEnableMcp,
  onNavigateMcp,
  onCopy,
}) {
  const label = (cn, en) => zh ? cn : en;
  const [activeTab, setActiveTab] = useState('overview');
  const [draft, setDraft] = useState(null);
  const settings = draft || initial;
  const [accounts, setAccounts] = useState([]);
  const [total, setTotal] = useState(null);
  const [pagination, setPagination] = useState(null);
  const [userId, setUserId] = useState('');
  const [namespace, setNamespace] = useState('qqbot');
  const page = pagination?.namespace === namespace && pagination?.userId === userId ? pagination.page : 1;
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [selectedAccessKeyId, setSelectedAccessKeyId] = useState('');
  const { begin, cancel } = useLatestRequest();
  const currentView = useRef(null);

  const load = useCallback(async (target = 1) => {
    const signal = begin();
    if (!signal) return;
    setLoading(true);
    try {
      const { data } = await api.get('/mcp/paper/accounts', {
        signal,
        params: { limit: 20, offset: (target - 1) * 20, ...(userId ? { user_id: userId } : {}), ...(namespace ? { namespace } : {}) },
      });
      if (signal?.aborted) return;
      setAccounts(data.items);
      setTotal(data.total);
      setPagination({ namespace, userId, page: target });
      setError('');
      return true;
    } catch (err) {
      if (!signal.aborted) { setError(err.message); return false; }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [userId, namespace, begin]);

  useEffect(() => {
    let active = true;
    Promise.resolve().then(() => { if (active) load(1); });
    return () => { active = false; cancel(); };
  }, [load, cancel]);

  useEffect(() => {
    currentView.current = () => load(page);
    return () => { currentView.current = null; };
  }, [load, page]);

  const mutate = async (action) => {
    setBusy(true);
    setError('');
    try { await action(); await currentView.current?.(); setRevision((v) => v + 1); return true; }
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
      await currentView.current?.();
      setRevision((v) => v + 1);
      message.success(label('模拟交易规则已保存', 'Paper trading settings saved'));
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };

  const copy = async (value) => {
    if (onCopy) return onCopy(value);
    try { await navigator.clipboard.writeText(value); message.success(label('已复制', 'Copied')); }
    catch { message.error(label('复制失败，请手动选择文本复制。', 'Copy failed. Select the text and copy manually.')); }
  };

  const update = (field, value) => setDraft((current) => ({ ...(current || initial), [field]: value }));
  const edit = (field, value) => setEditing((v) => ({ ...v, [field]: value }));
  const path = (row) => '/mcp/paper/accounts/' + encodeURIComponent(row.namespace) + '/' + encodeURIComponent(row.user_id);
  const format = (v) => formatPaperAmount(v, zh);
  const number = (v) => <span className="paper-number">{format(v)}</span>;
  const moneyFields = { cash: label('账本现金 · USDT', 'Ledger cash · USDT'), debt: label('负债 · USDT', 'Debt · USDT') };
  const rateHints = {
    daily_interest_rate: settings ? label(`当前相当于每日 ${formatPercentHint(settings.daily_interest_rate)}`, `Equals ${formatPercentHint(settings.daily_interest_rate)} / day`) : '',
    fee_rate: settings ? label(`当前相当于每笔 ${formatPercentHint(settings.fee_rate)}`, `Equals ${formatPercentHint(settings.fee_rate)} per fill`) : '',
    maintenance_margin_rate: settings ? label(`当前相当于 ${formatPercentHint(settings.maintenance_margin_rate)}`, `Equals ${formatPercentHint(settings.maintenance_margin_rate)}`) : '',
  };
  const numericField = (field, title, min, max, step) => (
    <div className="form-field" key={field}>
      <label htmlFor={'paper-setting-' + field}>{title}</label>
      <InputNumber id={'paper-setting-' + field} min={min} max={max} step={step} precision={field === 'max_leverage' ? 0 : undefined} value={settings[field]} onChange={(v) => update(field, v ?? min)} />
      {rateHints[field] && <span className="paper-field-hint">{rateHints[field]}</span>}
    </div>
  );
  const openEditor = (r) => setEditing({ namespace: r.namespace, user_id: r.user_id, name: r.name, cash: r.cash, debt: r.debt, leverage: r.leverage, markets: r.markets, enabled: r.enabled, leaderboard_visible: r.leaderboard_visible ?? true, notifications_enabled: r.notifications_enabled ?? true });

  if (!settings) return null;

  const endpointUrl = connection?.url || (mcpSettings?.public_url ? `${String(mcpSettings.public_url).replace(/\/+$/, '')}/mcp` : '');
  const selectedAccessKey = keys.find((key) => key.id === selectedAccessKeyId) || keys[0] || null;
  const qqbotConfigJson = JSON.stringify({
    mcpServers: {
      'crypto-agent-paper': {
        url: endpointUrl || 'https://your-domain.example.com/mcp',
        ...(selectedAccessKey ? { headers: { Authorization: `Bearer ${selectedAccessKey.secret}` } } : {}),
      },
    },
  }, null, 2);

  const readinessIssues = [];
  if (mcpSettings && !mcpSettings.enabled) {
    readinessIssues.push({
      key: 'mcp-disabled',
      severity: 'warning',
      text: label('MCP 总开关未开启，外部 QQBot 无法接入模拟交易。', 'MCP is disabled. QQBot cannot connect until MCP is enabled.'),
      actionLabel: label('立即开启 MCP', 'Enable MCP'),
      actionType: 'primary',
      loading: mcpBusy,
      onAction: onEnableMcp,
    });
  }
  if (!initial?.enabled) {
    readinessIssues.push({
      key: 'paper-disabled',
      severity: 'warning',
      text: label('模拟交易服务当前已暂停。', 'Paper trading is currently paused.'),
      actionLabel: label('前往规则设置', 'Open settings'),
      onAction: () => setActiveTab('settings'),
    });
  }
  if (mcpSettings && (!mcpSettings.public_url || connection?.restart_required)) {
    readinessIssues.push({
      key: 'public-url',
      severity: 'info',
      text: connection?.restart_required
        ? label('服务公开地址已保存，需重启后端容器/进程后才能应用新地址。', 'Public origin is saved; restart the backend service to activate it.')
        : label('服务公开地址尚未配置，外网 QQBot 将无法通过域名访问。', 'Public service origin is not configured yet.'),
      actionLabel: onNavigateMcp ? label('前往 MCP 配置', 'Open MCP settings') : '',
      onAction: onNavigateMcp,
    });
  }
  if (mcpSettings && keys.length === 0) {
    readinessIssues.push({
      key: 'no-paper-key',
      severity: 'info',
      text: label('尚未创建 Paper API Key，QQBot 需要 Paper Key 才可调用模拟交易。', 'No Paper API key yet. Create one for QQBot access.'),
      actionLabel: label('去创建 Paper Key', 'Create Paper key'),
      onAction: () => { setActiveTab('access'); onCreateKey?.(); },
    });
  }
  const hasBlockingIssue = readinessIssues.some((item) => item.severity === 'warning');
  const showScopeFilter = activeTab === 'overview' || activeTab === 'accounts';

  const tabItems = [
    { key: 'overview', label: label('概览与明细', 'Overview & records') },
    {
      key: 'accounts',
      label: (
        <span className="paper-tab-label">
          {label('账户管理', 'Accounts')}
          {total != null && <span className="paper-tab-count">{format(total)}</span>}
        </span>
      ),
    },
    {
      key: 'access',
      label: (
        <span className="paper-tab-label">
          {label('接入与密钥', 'Access & keys')}
          <span className="paper-tab-count">{keys.length}</span>
        </span>
      ),
    },
    {
      key: 'settings',
      label: (
        <span className="paper-tab-label">
          {label('规则设置', 'Rules & settings')}
          {draft && <Tag color="orange" className="paper-tab-unsaved">{label('未保存', 'Unsaved')}</Tag>}
        </span>
      ),
    },
  ];

  return (
    <div className="paper-workspace">
      <header className="paper-hero">
        <div className="paper-hero-main">
          <div className="paper-hero-icon"><RobotOutlined /></div>
          <div>
            <div className="paper-eyebrow">QQBOT · PAPER</div>
            <h2>{label('模拟交易工作台', 'Paper trading workspace')}</h2>
            <p>{label('集中查看资金明细、管理用户账本、获取 QQBot 接入配置与交易规则。', 'Monitor balances, manage accounts, configure QQBot access and adjust rules.')}</p>
          </div>
        </div>
        <div className="paper-hero-actions">
          <Tag className="paper-service-tag" color={initial?.enabled ? 'green' : 'default'}>
            {initial?.enabled ? label('服务已启用', 'Service enabled') : label('服务已暂停', 'Service paused')}
          </Tag>
          {mcpSettings && (
            <Tag className="paper-service-tag" color={mcpSettings.enabled ? 'blue' : 'orange'}>
              {mcpSettings.enabled ? label('MCP 已启用', 'MCP enabled') : label('MCP 未开启', 'MCP disabled')}
            </Tag>
          )}
          <Button type="primary" icon={<KeyOutlined />} onClick={() => { setActiveTab('access'); onCreateKey?.(); }}>
            {label('创建 Paper Key', 'Create paper key')}
          </Button>
        </div>
      </header>

      {error && <Alert type="error" showIcon title={error} />}

      {readinessIssues.length > 0 && (
        <Alert
          className="paper-readiness-alert"
          type={hasBlockingIssue ? 'warning' : 'info'}
          showIcon
          title={hasBlockingIssue
            ? label('QQBot 模拟交易接入尚未完全就绪', 'QQBot paper access needs attention')
            : label('配置提示：完成以下步骤即可让 QQBot 接入', 'Setup checklist for QQBot access')}
          description={
            <div className="paper-readiness-list">
              {readinessIssues.map((item) => (
                <div key={item.key} className="paper-readiness-item">
                  <span>{item.text}</span>
                  {item.actionLabel && item.onAction && (
                    <Button size="small" type={item.actionType || 'default'} loading={Boolean(item.loading)} onClick={item.onAction}>
                      {item.actionLabel}
                    </Button>
                  )}
                </div>
              ))}
            </div>
          }
        />
      )}

      <Tabs className="paper-workspace-tabs" activeKey={activeTab} onChange={setActiveTab} items={tabItems} />

      {showScopeFilter && (
        <div className="paper-filter-bar">
          <div className="paper-filter-title"><SearchOutlined /><span>{label('查看范围', 'View scope')}</span></div>
          <div className="paper-filter-field">
            <label htmlFor="paper-namespace">{label('命名空间', 'Namespace')}</label>
            <Input id="paper-namespace" aria-label={label('账户命名空间', 'Account namespace')} placeholder={label('全部命名空间', 'All namespaces')} value={namespace} onChange={(e) => setNamespace(e.target.value)} allowClear />
          </div>
          <div className="paper-filter-field paper-filter-user">
            <label htmlFor="paper-user">{label('用户 ID', 'User ID')}</label>
            <Input id="paper-user" aria-label={label('用户 ID', 'User ID')} placeholder={label('全部账户 / 输入完整 QQ 用户 ID', 'All accounts / exact QQ user ID')} value={userId} onChange={(e) => setUserId(e.target.value)} allowClear />
          </div>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={async () => { if (await load(1) !== undefined) setRevision((v) => v + 1); }}>
            {label('刷新', 'Refresh')}
          </Button>
        </div>
      )}

      <div role="tabpanel" hidden={activeTab !== 'overview'}>
        <PaperDataPanel namespace={namespace} userId={userId} revision={revision} zh={zh} maxLeverage={initial.max_leverage} onChanged={() => currentView.current?.()} />
      </div>

      <div role="tabpanel" hidden={activeTab !== 'accounts'}>
        <section className="paper-accounts-section">
          <div className="paper-section-heading">
            <div>
              <h3>{label('账户管理', 'Account management')}<span className="paper-heading-count">{format(total)}</span></h3>
              <p>{label('查看独立账本，展开账户了解持仓、付息提醒与审计记录。', 'Independent ledgers. Expand an account for holdings, interest and audit details.')}</p>
            </div>
            <Button icon={<PlusOutlined />} disabled={!namespace || !userId} loading={busy} onClick={() => mutate(() => api.post(path({ namespace, user_id: userId })))}>
              {label('按筛选 ID 开户', 'Create by selected ID')}
            </Button>
          </div>
          <Card size="small" className="paper-records-card">
            <Table
              size="small"
              loading={loading}
              rowKey={(row) => row.namespace + ':' + row.user_id}
              dataSource={accounts}
              scroll={{ x: 1160 }}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={label('当前范围暂无账户；填写命名空间和用户 ID 可开户。', 'No matching accounts. Enter a namespace and user ID to create one.')} /> }}
              pagination={{ current: page, pageSize: 20, total: total ?? undefined, hideOnSinglePage: true, showSizeChanger: false, onChange: (target) => load(target) }}
              columns={[
                { title: label('账户', 'Account'), dataIndex: 'user_id', width: 210, render: (v, r) => <div className="paper-cell-stack"><strong>{r.name || v}{r.account_type === 'bot' && <Tag color="blue">Bot</Tag>}</strong><span className="paper-cell-note">{v}</span><span className="paper-cell-note">{r.namespace}</span></div> },
                { title: label('状态', 'Status'), width: 115, render: (_, r) => <div className="paper-cell-stack"><Tag color={r.account_status === 'bankrupt' ? 'red' : r.account_status === 'active' ? 'green' : 'default'}>{r.account_status === 'bankrupt' ? label('破产冻结', 'Bankrupt') : r.account_status === 'active' ? label('正常', 'Active') : r.account_status || '—'}</Tag>{!r.enabled && <span className="paper-cell-note">{label('交易暂停', 'Trading paused')}</span>}<span className="paper-cell-note">{label('下海 ', 'Resets ')}{format(r.reset_count)}</span></div> },
                { title: label('账本现金', 'Ledger cash'), dataIndex: 'cash', render: number, align: 'right' },
                { title: label('可用现金', 'Available cash'), dataIndex: 'available_cash', render: number, align: 'right' },
                { title: label('保证金', 'Margin'), dataIndex: 'used_margin', render: number, align: 'right' },
                { title: label('负债', 'Debt'), dataIndex: 'debt', render: number, align: 'right' },
                { title: label('净资产', 'Net equity'), dataIndex: 'net_equity', align: 'right', render: (v, r) => <div className="paper-cell-stack paper-align-right"><strong className={'paper-number' + (v != null && v < 0 ? ' paper-negative' : '')}>{format(v)}</strong>{paperValuationIncomplete(r) && <Tooltip title={paperValuationReasons(r, zh).join(' ') || undefined}><Tag color="orange">{label('估值不完整', 'Incomplete')}</Tag></Tooltip>}</div> },
                { title: label('默认杠杆 / 市场', 'Default leverage / markets'), width: 145, render: (_, r) => <div className="paper-cell-stack"><span className="paper-number">{r.leverage != null ? format(r.leverage) + '×' : '—'}</span><span className="paper-cell-note">{r.markets?.map((v) => v === 'spot' ? label('现货', 'Spot') : v === 'swap' ? label('永续', 'Perpetual') : v).join(' · ') || '—'}</span>{paperFilterCount(r.filter) > 0 && <Tag>{label('历史过滤 ', 'Filtered ')}{paperFilterCount(r.filter)}</Tag>}</div> },
                { title: label('操作', 'Actions'), width: 105, render: (_, r) => <Button size="small" onClick={() => openEditor(r)}>{label('调整账户', 'Adjust')}</Button> },
              ]}
              expandable={{
                expandedRowRender: (r) => (
                  <div className="paper-account-detail">
                    <div className="paper-detail-facts">
                      {[[label('已实现盈亏', 'Realized PnL'), r.realized_pnl, 'USDT'], [label('本轮手续费', 'Cycle fees'), r.fees, 'USDT'], [label('本轮已付利息', 'Cycle interest paid'), r.interest_paid, 'USDT'], [label('账户周期', 'Account cycle'), r.account_epoch, '']].map(([title, value, unit]) => (
                        <div key={title}><span>{title}</span><strong className="paper-number">{format(value)} <small>{unit}</small></strong></div>
                      ))}
                    </div>
                    {paperValuationIncomplete(r) && paperValuationReasons(r, zh).length > 0 && <Alert className="paper-valuation-reasons" type="warning" showIcon title={label('估值不完整的原因', 'Why valuation is incomplete')} description={paperValuationReasons(r, zh).join(' ')} />}
                    {r.interest_reserve?.message && <Alert type={r.interest_reserve.shortfall > 0 ? 'warning' : 'info'} showIcon title={label('付息资金提醒', 'Interest cash reminder')} description={r.interest_reserve.message} />}
                    {paperFilterCount(r.filter) > 0 && <Alert type="info" showIcon title={label('历史资源已过滤，资金占用仍保留', 'Filtered history retains financial commitments')} description={label('预留现金：', 'Reserved cash: ') + format(r.filter.retained_reserved_cash) + ' USDT · ' + label('保证金：', 'Margin: ') + format(r.filter.retained_margin) + ' USDT'} />}
                    <div className="paper-account-holdings">
                      {[...(r.positions || []).map((p) => ({ ...p, kind: 'swap' })), ...(r.spot || []).map((p) => ({ ...p, kind: 'spot' }))].map((holding, index) => (
                        <div className="paper-holding" key={holding.resource_id || holding.episode_id || [holding.exchange, holding.symbol, holding.side, index].join(':')}>
                          <div><strong>{holding.symbol}</strong><Tag>{holding.kind === 'spot' ? label('现货', 'Spot') : label('永续', 'Perpetual')}{holding.side ? ' · ' + (holding.side === 'LONG' ? label('多', 'Long') : label('空', 'Short')) : ''}</Tag></div>
                          <span>{label('数量 ', 'Quantity ')}<b className="paper-number">{format(holding.amount)}</b></span>
                          <span>{label('成本价 ', 'Entry ')}<b className="paper-number">{format(holding.entry_price)}</b></span>
                        </div>
                      ))}
                      {!r.positions?.length && !r.spot?.length && <span className="paper-cell-note">{label('当前无可见持仓', 'No visible positions')}</span>}
                    </div>
                    <AuditDisclosure url={path(r) + '/audit'} zh={zh} title={label('查看原始账本审计（含已过滤历史）', 'Original ledger audit (including filtered history)')} views={false} />
                  </div>
                ),
              }}
            />
          </Card>
        </section>
      </div>

      {activeTab === 'access' && (
        <div role="tabpanel" className="paper-access-stack">
          <Card
            size="small"
            className="paper-records-card"
            title={<Space><LinkOutlined />{label('QQBot MCP 接入配置', 'QQBot MCP connection')}</Space>}
            extra={onNavigateMcp && <Button size="small" icon={<ApiOutlined />} onClick={onNavigateMcp}>{label('前往 MCP 总设置', 'Open MCP settings')}</Button>}
          >
            <div className="paper-access-grid">
              <div className="paper-access-column">
                <Space wrap style={{ marginBottom: 12 }}>
                  <Tag color={mcpSettings?.enabled ? 'green' : 'orange'}>{mcpSettings?.enabled ? label('MCP 总开关已启用', 'MCP enabled') : label('MCP 总开关未开启', 'MCP disabled')}</Tag>
                  <Tag color={initial?.enabled ? 'green' : 'default'}>{initial?.enabled ? label('模拟交易已启用', 'Paper enabled') : label('模拟交易已暂停', 'Paper paused')}</Tag>
                  {connection?.restart_required && <Tag color="orange">{label('地址待重启生效', 'Restart required')}</Tag>}
                </Space>
                <div className="form-field">
                  <label htmlFor="paper-mcp-endpoint">{label('MCP 服务器地址', 'MCP server URL')}</label>
                  <Space.Compact style={{ width: '100%' }}>
                    <Input id="paper-mcp-endpoint" value={endpointUrl} readOnly placeholder={label('尚未配置公开地址', 'Public origin not configured')} />
                    <Button icon={<CopyOutlined />} disabled={!endpointUrl} onClick={() => copy(endpointUrl)}>{label('复制', 'Copy')}</Button>
                  </Space.Compact>
                </div>
                <div className="form-field" style={{ marginTop: 12 }}>
                  <label htmlFor="paper-access-key-select">{label('选择生成配置的 Paper Key', 'Paper key for JSON snippet')}</label>
                  <Select
                    id="paper-access-key-select"
                    allowClear
                    value={selectedAccessKey?.id || undefined}
                    onChange={(value) => setSelectedAccessKeyId(value || '')}
                    options={keys.map((key) => ({ value: key.id, label: `${key.name} (${key.paper_namespace})` }))}
                    placeholder={label('先创建 Paper Key 后自动带入凭据', 'Create a Paper key to include credentials')}
                  />
                </div>
                <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
                  {label('QQBot 只需使用 Paper Key（仅可访问模拟盘，无法动用真实交易账户），并从可信消息事件传入 QQ 用户 ID。', 'QQBot should use a Paper key (isolated from live exchange accounts) and supply user IDs from trusted message events.')}
                </Paragraph>
              </div>
              <div className="paper-access-column">
                <div className="paper-code-header">
                  <Text strong>{label('QQBot 客户端配置 JSON', 'QQBot MCP client JSON')}</Text>
                  <Button size="small" icon={<CopyOutlined />} onClick={() => copy(qqbotConfigJson)}>{label('复制配置', 'Copy JSON')}</Button>
                </div>
                <pre className="paper-code-preview">{qqbotConfigJson}</pre>
              </div>
            </div>
          </Card>

          <Card
            size="small"
            className="paper-records-card"
            title={<Space><KeyOutlined />Paper API Keys</Space>}
            extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => onCreateKey?.()}>{label('创建 Paper Key', 'Create Paper key')}</Button>}
          >
            <div className="settings-stack">
              {keys.map((key) => (
                <div key={key.id} className="paper-key-row">
                  <Space wrap>
                    <Text strong>{key.name}</Text>
                    <Tag color="blue">Paper</Tag>
                    <Tag>{label('命名空间', 'Namespace')}: {key.paper_namespace}</Tag>
                    {(key.scopes || []).map((scope) => <Tag key={scope}>{scope}</Tag>)}
                    {onRevokeKey && (
                      <Popconfirm title={label('撤销并删除此 Paper Key？', 'Revoke and delete this Paper key?')} onConfirm={() => onRevokeKey(key.id)}>
                        <Button size="small" danger>{label('撤销', 'Revoke')}</Button>
                      </Popconfirm>
                    )}
                  </Space>
                  <Input style={{ marginTop: 10 }} value={key.secret} readOnly addonAfter={<Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copy(key.secret)} />} />
                </div>
              ))}
              {!keys.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={label('尚未创建 Paper Key，点击右上角即可生成。', 'No Paper keys yet. Create one above for QQBot.')} />}
            </div>
          </Card>

          <Card size="small" className="paper-records-card" title={<Space><ToolOutlined />{label('QQBot 模拟交易可用工具', 'Available tools for QQBot paper trading')}</Space>}>
            <McpToolCatalog tools={tools} zh={zh} />
          </Card>
        </div>
      )}

      {activeTab === 'settings' && (
        <div role="tabpanel">
          <Card
            size="small"
            className="paper-records-card paper-settings-card"
            title={
              <div className="paper-settings-label">
                <SettingOutlined />
                <span>{label('交易与银行设置', 'Trading & bank settings')}</span>
                {draft && <Tag color="orange">{label('有未保存修改', 'Unsaved changes')}</Tag>}
              </div>
            }
            extra={<span className="paper-cell-note">{label('开户资金、利率、杠杆上限', 'Initial cash, interest & leverage cap')}</span>}
          >
            <div className="paper-settings-groups">
              <section>
                <h4><RobotOutlined />{label('服务与机器人', 'Service & bot')}</h4>
                <div className="settings-form-grid">
                  <div className="form-field"><label>{label('启用模拟交易', 'Enable paper trading')}</label><Switch aria-label={label('启用模拟交易', 'Enable paper trading')} checked={settings.enabled} onChange={(v) => update('enabled', v)} /></div>
                  <div className="form-field"><label>{label('真实公开行情', 'Public market source')}</label><Typography.Text>Binance · USDT</Typography.Text></div>
                  <div className="form-field"><label htmlFor="paper-bot-id">{label('Bot 账户 ID', 'Bot account ID')}</label><Input id="paper-bot-id" value={settings.bot_user_id} onChange={(e) => update('bot_user_id', e.target.value)} /></div>
                  <div className="form-field"><label htmlFor="paper-bot-name">{label('Bot 默认名称', 'Default bot name')}</label><Input id="paper-bot-name" value={settings.bot_name} onChange={(e) => update('bot_name', e.target.value)} /></div>
                  {numericField('initial_balance', label('开户 / 重置资金 · USDT', 'Creation / reset cash · USDT'), 0, 1e12, 100)}
                </div>
              </section>
              <section>
                <h4><BankOutlined />{label('银行与借款', 'Bank & loans')}</h4>
                <div className="settings-form-grid">
                  <div className="form-field"><label>{label('允许新增借款', 'Allow new loans')}</label><Switch aria-label={label('允许新增借款', 'Allow new loans')} checked={settings.bank_enabled} onChange={(v) => update('bank_enabled', v)} /></div>
                  {numericField('daily_interest_rate', label('日利率（0.01 = 1%）', 'Daily rate (0.01 = 1%)'), 0, undefined, .0001)}
                  {numericField('loan_limit', label('统一借款上限 · USDT', 'Loan limit · USDT'), 0, 1e12, 100)}
                </div>
                <p>{label('按本金单利计息，每日北京时间 00:00 扣息。实际现金不足时可扣为负数并冻结；挂单占用不会单独触发破产。', 'Simple interest on principal, settled at midnight Beijing time. A real cash shortfall can make cash negative and freeze the account; reservations alone do not trigger bankruptcy.')}</p>
              </section>
              <section>
                <h4><SettingOutlined />{label('交易规则', 'Trading rules')}</h4>
                <div className="settings-form-grid">
                  {numericField('max_leverage', label('杠杆上限', 'Leverage cap'), 1, 125, 1)}
                  {numericField('fee_rate', label('手续费比例（0.001 = 0.1%）', 'Fee rate (0.001 = 0.1%)'), 0, .01, .0001)}
                  {numericField('maintenance_margin_rate', label('维持保证金比例（0.005 = 0.5%）', 'Maintenance rate (0.005 = 0.5%)'), .000001, .1, .001)}
                </div>
                <p>{label('仅支持 Binance 的 USDT 现货与线性永续。账户默认杠杆用于后续开仓，已有仓位保持各自参数。', 'Binance USDT spot and linear perpetuals only. Account default leverage applies to new entries; existing positions retain their parameters.')}</p>
              </section>
            </div>
            <div className="paper-settings-save">
              <span>{label('设置保存后生效；开户资金仅用于新账户与下海重置。', 'Saved settings apply going forward; initial cash is used for creation and resets.')}</span>
              <Button type="primary" loading={busy} onClick={saveSettings}>{label('保存设置', 'Save settings')}</Button>
            </div>
          </Card>
        </div>
      )}

      <Modal
        open={Boolean(editing)}
        title={label('调整模拟账户 · ', 'Adjust paper account · ') + (editing?.user_id || '')}
        onCancel={() => setEditing(null)}
        confirmLoading={busy}
        okButtonProps={{ disabled: !editing?.markets?.length }}
        onOk={async () => {
          const { namespace: ns, user_id: id, ...payload } = editing;
          if (await mutate(() => api.patch(path({ namespace: ns, user_id: id }), payload))) setEditing(null);
        }}
      >
        {editing && (
          <div className="settings-form-grid">
            {error && <Alert type="error" title={error} />}
            <div className="form-field"><label>{label('名称', 'Name')}</label><Input value={editing.name} onChange={(e) => edit('name', e.target.value)} /></div>
            {Object.entries(moneyFields).map(([field, title]) => (
              <div className="form-field" key={field}>
                <label>{title}</label>
                <InputNumber aria-label={field} min={field === 'cash' ? -1e12 : 0} max={1e12} value={editing[field]} onChange={(v) => edit(field, v ?? 0)} />
              </div>
            ))}
            <div className="form-field"><label>{label('后续开仓杠杆', 'Leverage for new entries')}</label><InputNumber min={1} max={settings.max_leverage} precision={0} value={editing.leverage} onChange={(v) => edit('leverage', v ?? 1)} /></div>
            <div className="form-field"><label>{label('启用账户', 'Enable account')}</label><Switch checked={editing.enabled} onChange={(v) => edit('enabled', v)} /></div>
            <div className="form-field"><label>{label('参与排行榜', 'Leaderboard participation')}</label><Switch checked={editing.leaderboard_visible} onChange={(v) => edit('leaderboard_visible', v)} /></div>
            <div className="form-field"><label>{label('成交与爆仓提醒', 'Fill and liquidation notifications')}</label><Switch checked={editing.notifications_enabled} onChange={(v) => edit('notifications_enabled', v)} /></div>
            <div className="form-field"><label>{label('允许市场', 'Allowed markets')}</label><Checkbox.Group value={editing.markets} options={[{ value: 'spot', label: label('现货', 'Spot') }, { value: 'swap', label: label('合约', 'Perpetual') }]} onChange={(v) => edit('markets', v)} /></div>
          </div>
        )}
      </Modal>
    </div>
  );
}
