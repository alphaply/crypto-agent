import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, InputNumber, Modal, Popconfirm, Select, Space, Spin, Switch, Table, Tag, Typography, message } from 'antd';
import { ApiOutlined, CopyOutlined, KeyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Text, Paragraph } = Typography;
const blankProfile = () => ({ profile_id: `connection-${Date.now()}`, name: '', exchange_profile_id: '', market_type: 'swap', symbols: ['BTC/USDT'], enabled: true, max_leverage: 5, leverage: 1, exit_mode: 'attached_required', spot_allowance: 100 });

export default function McpSettingsPanel({ profiles: exchangeProfiles = [] }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const [data, setData] = useState(null);
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null);
  const [newProfile, setNewProfile] = useState(false);
  const [keyDraft, setKeyDraft] = useState(null);
  const [selectedKey, setSelectedKey] = useState('');

  const load = useCallback(async () => {
    try {
      const { data: next } = await api.get('/mcp');
      setData(next);
      setSettings(next.settings);
      setError('');
    } catch (err) { setError(err.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    let active = true;
    Promise.resolve().then(() => { if (active) load(); });
    return () => { active = false; };
  }, [load]);

  const mutate = async (action) => {
    setBusy(true);
    try { await action(); await load(); return true; }
    catch (err) { setError(err.message); return false; }
    finally { setBusy(false); }
  };
  const copy = async (value) => {
    try { await navigator.clipboard.writeText(value); message.success(label('已复制', 'Copied')); }
    catch { message.error(label('复制失败，请手动选择文本复制。', 'Copy failed. Select the text and copy manually.')); }
  };
  const updateProfile = (key, value) => setEditing((current) => ({ ...current, [key]: value }));
  const activeKeys = (data?.keys || []).filter((key) => !key.revoked);
  const connectionKey = activeKeys.find((key) => key.id === selectedKey);
  const connectionConfig = JSON.stringify({ mcpServers: { 'crypto-agent': { url: data?.connection?.url || '', ...(connectionKey ? { headers: { Authorization: `Bearer ${connectionKey.secret}` } } : {}) } } }, null, 2);

  if (loading) return <Card className="panel-card"><Spin /></Card>;
  if (!data) return <Alert type="error" title={error || label('MCP 配置加载失败', 'MCP settings unavailable')} action={<Button onClick={load}>{label('重试', 'Retry')}</Button>} />;

  return <div className="settings-stack">
    {error && <Alert type="error" title={error} closable onClose={() => setError('')} />}
    <Card className="panel-card" title={<Space><ApiOutlined />{label('外部助手接入', 'External assistants')}</Space>} extra={<Button icon={<ReloadOutlined />} onClick={load}>{label('刷新', 'Refresh')}</Button>}>
      <Paragraph type="secondary">{label('在 Workbuddy、ChatGPT 或其他 MCP 客户端中连接此服务，共享消息情报、查询账户并执行授权的交易。每个连接配置独立于内部 Agent。', 'Connect Workbuddy, ChatGPT or another MCP client to shared intelligence, account queries and authorized trading. Connection profiles are independent of internal agents.')}</Paragraph>
      <div className="settings-form-grid">
        <div className="form-field"><label>{label('启用 MCP', 'Enable MCP')}</label><Switch checked={settings.enabled} onChange={(enabled) => setSettings({ ...settings, enabled })} /></div>
        <div className="form-field"><label>{label('服务公开地址', 'Public service origin')}</label><Input value={settings.public_url} placeholder="https://agent.example.com" onChange={(event) => setSettings({ ...settings, public_url: event.target.value })} /></div>
      </div>
      <Space wrap style={{ marginTop: 16 }}><Button type="primary" loading={busy} onClick={() => mutate(() => api.put('/mcp/settings', settings))}>{label('保存接入设置', 'Save connection settings')}</Button><Text type="secondary">{label('公开地址变更后重启服务；其他设置即时生效。', 'Restart after changing the public origin. Other settings take effect immediately.')}</Text></Space>
      {data.connection?.restart_required && <Alert style={{ marginTop: 12 }} type="warning" showIcon title={label('公开地址已修改，请重启后再建立新连接。', 'The public origin changed. Restart before creating connections.')} />}
      <div className="settings-stack" style={{ marginTop: 20 }}>
        <div><Text type="secondary">Streamable HTTP</Text><Paragraph copyable={{ text: data.connection?.url }} style={{ marginBottom: 0 }}>{data.connection?.url}</Paragraph></div>
        <Text type="secondary">{label('ChatGPT 使用 OAuth：添加服务 URL 后，使用管理员密码登录并选择权限与配置。WorkBuddy 可使用下方 API Key 的 Bearer 连接方式。', 'ChatGPT uses OAuth: add the URL, sign in with the administrator password and choose scopes and profiles. WorkBuddy can use a Bearer API key below.')}</Text>
        <Select allowClear value={selectedKey || undefined} onChange={(value) => setSelectedKey(value || '')} options={activeKeys.map((key) => ({ value: key.id, label: key.name }))} placeholder={label('OAuth 连接 / 选择 API Key 生成配置', 'OAuth connection / select an API key')} />
        <pre style={{ margin: 0, padding: 16, borderRadius: 12, background: 'var(--bg-subtle)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{connectionConfig}</pre>
        <Button icon={<CopyOutlined />} onClick={() => copy(connectionConfig)}>{label('复制连接配置', 'Copy connection configuration')}</Button>
      </div>
    </Card>

    <Card className="panel-card" title={label('交易连接配置', 'Trading connection profiles')} extra={<Button icon={<PlusOutlined />} onClick={() => { setEditing(blankProfile()); setNewProfile(true); }}>{label('新建配置', 'New profile')}</Button>}>
      <div className="settings-stack">
        {(data.profiles || []).map((profile) => <Card key={profile.profile_id} size="small" title={<Space wrap><Text strong>{profile.name}</Text><Tag>{profile.market_type === 'spot' ? label('现货', 'Spot') : label('合约', 'Perpetual')}</Tag><Tag color={profile.enabled ? 'green' : 'default'}>{profile.enabled ? label('启用', 'Enabled') : label('暂停', 'Paused')}</Tag></Space>} extra={<Space><Button onClick={() => { setEditing({ ...profile }); setNewProfile(false); }}>{label('编辑', 'Edit')}</Button><Popconfirm title={label('删除此连接配置？已有交易记录的配置只能停用。', 'Delete this profile? Profiles with trading history must be disabled instead.')} onConfirm={() => mutate(() => api.delete(`/mcp/profiles/${encodeURIComponent(profile.profile_id)}`))}><Button danger>{label('删除', 'Delete')}</Button></Popconfirm></Space>}>
          <Space direction="vertical"><Text>{profile.symbols.join(' · ')}</Text><Text type="secondary">{label('交易账户', 'Exchange account')}: {exchangeProfiles.find((item) => item.profile_id === profile.exchange_profile_id)?.name || profile.exchange_profile_id}</Text><Text type="secondary">{profile.market_type === 'swap' ? label(`MCP 杠杆上限 ${profile.max_leverage}× · 读取交易所实际杠杆后执行`, `MCP leverage cap ${profile.max_leverage}× · verified against exchange settings`) : label(`每次请求组合共用额度 ${profile.spot_allowance}`, `Shared portfolio allowance per call: ${profile.spot_allowance}`)}</Text></Space>
        </Card>)}
        {!data.profiles?.length && <Empty description={label('先创建交易账户，再建立 MCP 连接配置。', 'Create an exchange account, then add an MCP connection profile.')} />}
      </div>
    </Card>

    <Card className="panel-card" title={label('OAuth 授权连接', 'OAuth connections')}>
      <div className="settings-stack">{(data.oauth_connections || []).map((connection) => <div key={connection.id}>
        <Space wrap><Text strong>{connection.name}</Text>{connection.scopes.map((scope) => <Tag key={scope}>{scope}</Tag>)}{connection.revoked ? <Tag>{label('已撤销', 'Revoked')}</Tag> : <Popconfirm title={label('撤销此客户端的全部访问权限？', 'Revoke all access for this connection?')} onConfirm={() => mutate(() => api.delete(`/mcp/oauth-grants/${connection.id}`))}><Button size="small" danger>{label('撤销', 'Revoke')}</Button></Popconfirm>}</Space>
        <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>{label('配置范围', 'Profiles')}: {connection.profile_ids.join(' · ') || label('仅共享消息', 'Shared news only')} · {label('到期', 'Expires')}: {new Date(connection.expires_at * 1000).toLocaleString()}</Paragraph>
      </div>)}{!data.oauth_connections?.length && <Empty description={label('尚未建立 OAuth 连接', 'No OAuth connections yet')} />}</div>
    </Card>

    <Card className="panel-card" title={<Space><KeyOutlined />API Keys</Space>} extra={<Button icon={<PlusOutlined />} onClick={() => setKeyDraft({ name: '', scopes: ['read'], profile_ids: [] })}>{label('创建 API Key', 'Create API key')}</Button>}>
      <div className="settings-stack">{(data.keys || []).map((key) => <div key={key.id} style={{ padding: '12px 0', borderBottom: '1px solid var(--border-soft)' }}>
        <Space wrap><Text strong>{key.name}</Text>{key.scopes.map((scope) => <Tag key={scope}>{scope}</Tag>)}{key.revoked ? <Tag>{label('已撤销', 'Revoked')}</Tag> : <Popconfirm title={label('撤销此 API Key？', 'Revoke this API key?')} onConfirm={() => mutate(() => api.delete(`/mcp/keys/${key.id}`))}><Button size="small" danger>{label('撤销', 'Revoke')}</Button></Popconfirm>}</Space>
        {!key.revoked && <Input style={{ marginTop: 10 }} value={key.secret} readOnly addonAfter={<Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copy(key.secret)} />} />}
        <Text type="secondary">{label('配置范围', 'Profiles')}: {key.profile_ids.join(' · ') || label('仅共享消息', 'Shared news only')}</Text>
      </div>)}{!data.keys?.length && <Empty description={label('尚未创建 API Key', 'No API keys yet')} />}</div>
    </Card>

    <Card className="panel-card" title={label('可用工具', 'Available tools')}><Space wrap>{(data.tools || []).map((tool) => <Tag key={tool.name}>{tool.name} · {tool.scope}</Tag>)}</Space><Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>{label('交易请求必须携带稳定 operation_id；同一请求重试返回原回执。查询不会执行交易。结果未知时请先核验订单。', 'Trade requests require a stable operation_id; retries return the original receipt. Queries never trade. Inspect orders after an unknown outcome.')}</Paragraph></Card>
    <Card className="panel-card" title={label('最近调用', 'Recent calls')}><Table size="small" rowKey="id" dataSource={data.audit || []} pagination={{ pageSize: 8 }} scroll={{ x: 680 }} columns={[
      { title: label('时间', 'Time'), dataIndex: 'timestamp', render: (value) => new Date(value * 1000).toLocaleString() },
      { title: label('工具', 'Tool'), dataIndex: 'tool' }, { title: label('配置', 'Profile'), dataIndex: 'profile_id' },
      { title: label('结果', 'Status'), dataIndex: 'status', render: (value) => <Tag color={value === 'failed' ? 'red' : value === 'unknown' ? 'orange' : 'default'}>{value}</Tag> },
    ]} expandable={{ expandedRowRender: (record) => <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify({ operation_id: record.operation_id, principal: record.principal, result: (() => { try { return JSON.parse(record.result); } catch { return record.result; } })() }, null, 2)}</pre> }} /></Card>

    <Modal open={Boolean(editing)} title={label(newProfile ? '新建连接配置' : '编辑连接配置', newProfile ? 'New connection profile' : 'Edit connection profile')} onCancel={() => setEditing(null)} confirmLoading={busy} onOk={async () => { if (await mutate(() => api.put(`/mcp/profiles/${encodeURIComponent(editing.profile_id)}`, editing))) setEditing(null); }} width={720}>
      {editing && <div className="settings-form-grid">
        <div className="form-field"><label>{label('名称', 'Name')}</label><Input value={editing.name} onChange={(event) => updateProfile('name', event.target.value)} /></div>
        <div className="form-field"><label>ID</label><Input disabled={!newProfile} value={editing.profile_id} onChange={(event) => updateProfile('profile_id', event.target.value)} /></div>
        <div className="form-field"><label>{label('市场', 'Market')}</label><Select value={editing.market_type} options={[{ value: 'swap', label: label('合约', 'Perpetual') }, { value: 'spot', label: label('现货', 'Spot') }]} onChange={(value) => setEditing({ ...editing, market_type: value, exchange_profile_id: '' })} /></div>
        <div className="form-field"><label>{label('已保存的交易账户', 'Saved exchange account')}</label><Select value={editing.exchange_profile_id || undefined} options={exchangeProfiles.filter((profile) => (profile.market_type || 'swap') === editing.market_type).map((profile) => ({ value: profile.profile_id, label: profile.name || profile.profile_id }))} onChange={(value) => updateProfile('exchange_profile_id', value)} /></div>
        <div className="form-field"><label>{label('允许交易的标的', 'Allowed symbols')}</label><Select mode="tags" value={editing.symbols} tokenSeparators={[',', ' ']} onChange={(value) => updateProfile('symbols', value)} /></div>
        <div className="form-field"><label>{label('启用', 'Enabled')}</label><Switch checked={editing.enabled} onChange={(value) => updateProfile('enabled', value)} /></div>
        {editing.market_type === 'swap' ? <>
          <div className="form-field"><label>{label('MCP 杠杆上限', 'MCP leverage cap')}</label><InputNumber min={1} max={125} value={editing.max_leverage} onChange={(value) => updateProfile('max_leverage', value ?? 5)} /></div>
          <div className="form-field"><label>{label('配置杠杆', 'Configured leverage')}</label><InputNumber min={1} max={editing.max_leverage} value={editing.leverage} onChange={(value) => updateProfile('leverage', value ?? 1)} /></div>
          <div className="form-field"><label>{label('退出模式', 'Exit mode')}</label><Select value={editing.exit_mode} options={[{ value: 'attached_required', label: label('必须附带 TP / SL', 'Required TP / SL') }, { value: 'attached_optional', label: label('可选 TP / SL', 'Optional TP / SL') }, { value: 'independent_exits', label: label('独立退出委托', 'Independent exit orders') }]} onChange={(value) => updateProfile('exit_mode', value)} /></div>
        </> : <div className="form-field"><label>{label('每次请求组合共用额度', 'Portfolio allowance per request')}</label><InputNumber min={0.01} value={editing.spot_allowance} onChange={(value) => updateProfile('spot_allowance', value ?? 100)} /></div>}
      </div>}
    </Modal>
    <Modal open={Boolean(keyDraft)} title={label('创建 API Key', 'Create API key')} onCancel={() => setKeyDraft(null)} confirmLoading={busy} onOk={async () => { if (await mutate(() => api.post('/mcp/keys', keyDraft))) setKeyDraft(null); }}>
      {keyDraft && <div className="settings-stack">
        <Input placeholder={label('名称，例如 Workbuddy', 'Name, for example Workbuddy')} value={keyDraft.name} onChange={(event) => setKeyDraft({ ...keyDraft, name: event.target.value })} />
        <Checkbox.Group value={keyDraft.scopes} options={[{ value: 'read', label: label('查询', 'Read'), disabled: true }, { value: 'trade', label: label('交易', 'Trade') }, { value: 'cancel', label: label('撤单', 'Cancel') }]} onChange={(scopes) => setKeyDraft({ ...keyDraft, scopes: [...new Set(['read', ...scopes])] })} />
        <Select mode="multiple" placeholder={label('允许访问的配置', 'Allowed profiles')} value={keyDraft.profile_ids} options={(data.profiles || []).map((profile) => ({ value: profile.profile_id, label: profile.name }))} onChange={(profile_ids) => setKeyDraft({ ...keyDraft, profile_ids })} />
      </div>}
    </Modal>
  </div>;
}
