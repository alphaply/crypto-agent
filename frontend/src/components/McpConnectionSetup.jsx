import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Collapse, Input, Space, Tag, Typography, message } from 'antd';
import { CopyOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { mcpChatGptCallback, mcpConnectionDraftChanged } from '../lib/mcpConnection';

const { Paragraph, Text } = Typography;

export default function McpConnectionSetup({ connection = {}, savedSettings, draftSettings, locale, busy }) {
  const zh = locale === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const [diagnostic, setDiagnostic] = useState(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState('');
  const [redirectUri, setRedirectUri] = useState('');
  const [scopes, setScopes] = useState(['read']);
  const [creating, setCreating] = useState(false);
  const [clientError, setClientError] = useState('');
  const [registration, setRegistration] = useState(null);
  const checkRequest = useRef(null);
  const clientRequest = useRef(null);
  const unsaved = mcpConnectionDraftChanged(draftSettings, savedSettings);
  const callback = mcpChatGptCallback(redirectUri);
  const canConnect = savedSettings?.enabled && !connection.restart_required && !unsaved;
  const client = registration?.client;
  const clientConnection = { ...connection, ...registration?.connection };

  useEffect(() => () => {
    checkRequest.current?.abort();
    clientRequest.current?.abort();
  }, []);

  const copy = async (value) => {
    try { await navigator.clipboard.writeText(value); message.success(label('已复制', 'Copied')); }
    catch { message.error(label('复制失败，请选择文本手动复制。', 'Copy failed. Select the text and copy manually.')); }
  };

  const checkConnection = async () => {
    checkRequest.current?.abort();
    const controller = new AbortController();
    checkRequest.current = controller;
    setChecking(true);
    setCheckError('');
    try {
      const { data } = await api.get('/mcp/connection-check', { signal: controller.signal });
      if (!controller.signal.aborted) setDiagnostic(data);
    } catch (err) {
      if (!controller.signal.aborted) { setDiagnostic(null); setCheckError(err.message); }
    } finally {
      if (!controller.signal.aborted) setChecking(false);
    }
  };

  const createClient = async () => {
    if (!callback || !canConnect || clientRequest.current) return;
    const controller = new AbortController();
    clientRequest.current = controller;
    setCreating(true);
    setClientError('');
    try {
      const { data } = await api.post('/mcp/oauth-clients', { name: 'ChatGPT', redirect_uri: callback, scopes }, { signal: controller.signal });
      if (!controller.signal.aborted) setRegistration(data);
    } catch (err) {
      if (!controller.signal.aborted) setClientError(err.message);
    } finally {
      if (!controller.signal.aborted) { setCreating(false); clientRequest.current = null; }
    }
  };

  const copyField = (name, value, secret = false) => <div className="form-field" key={name}>
    <label>{name}</label>
    <Space.Compact style={{ width: '100%' }}>
      {secret
        ? <Input.Password aria-label={name} value={value || ''} readOnly autoComplete="off" />
        : <Input aria-label={name} value={value || ''} readOnly />}
      <Button aria-label={label(`复制 ${name}`, `Copy ${name}`)} icon={<CopyOutlined />} disabled={!value} onClick={() => copy(value)} />
    </Space.Compact>
  </div>;

  return <div className="settings-stack" style={{ marginTop: 20 }}>
    <div>
      <Text strong>{label('连接 ChatGPT', 'Connect ChatGPT')}</Text>
      <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 12 }}>{label('在 ChatGPT 的应用设置中新建自定义连接，按下面的步骤完成接入。', 'Create a custom connection in ChatGPT app settings, then follow these steps.')}</Paragraph>
      {unsaved && <Alert style={{ marginBottom: 12 }} type="warning" showIcon title={label('接入设置尚未保存', 'Connection settings are not saved')} description={label('先保存上方设置，再检查或建立连接。以下地址来自已保存的配置。', 'Save the settings above before checking or connecting. The address below is from the saved configuration.')} />}
      {!savedSettings?.enabled && <Alert style={{ marginBottom: 12 }} type="warning" showIcon title={label('MCP 尚未启用', 'MCP is disabled')} description={label('启用上方 MCP 开关并保存设置。', 'Enable MCP above and save the settings.')} />}
      {copyField(label('1. 复制服务器 URL', '1. Copy the server URL'), connection.url)}
    </div>

    <div className="settings-stack" style={{ gap: 12 }}>
      <Space wrap><Text strong>{label('2. 检查公网连接', '2. Check the public connection')}</Text><Button icon={<ReloadOutlined />} loading={checking} disabled={busy || unsaved} onClick={checkConnection}>{label(diagnostic ? '重新检查' : '检查连接', diagnostic ? 'Check again' : 'Check connection')}</Button></Space>
      {!diagnostic && !checkError && <Text type="secondary">{label('检查公开地址和 OAuth 发现接口，确认 ChatGPT 能读取正确的授权信息。', 'Check the public address and OAuth discovery endpoints that ChatGPT uses to connect.')}</Text>}
      {checkError && <Alert type="error" showIcon title={label('连接检查失败', 'Connection check failed')} description={checkError} />}
      {diagnostic && <>
        <Alert type={diagnostic.ready ? 'success' : 'warning'} showIcon title={label(diagnostic.ready ? '连接检查通过，可以前往 ChatGPT 添加' : '连接配置需要处理', diagnostic.ready ? 'Connection check passed. Add it in ChatGPT.' : 'Connection settings need attention')} description={diagnostic.checked_at ? label(`检查时间：${new Date(diagnostic.checked_at * 1000).toLocaleString()}`, `Checked: ${new Date(diagnostic.checked_at * 1000).toLocaleString()}`) : undefined} />
        {(diagnostic.checks || []).map((check, index) => <div key={check.id || index} style={{ minWidth: 0 }}>
          <Space wrap><Tag color={check.status === 'ok' ? 'green' : check.status === 'error' ? 'red' : 'orange'}>{check.status === 'ok' ? label('通过', 'Passed') : check.status === 'error' ? label('失败', 'Failed') : label('提示', 'Attention')}</Tag><Text strong>{check.label || check.id}</Text></Space>
          {check.message && <Paragraph style={{ marginTop: 4, marginBottom: 0 }}>{check.message}</Paragraph>}
          {check.url && <Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{check.url}</Text>}
        </div>)}
      </>}
    </div>

    <div>
      <Text strong>{label('3. 在 ChatGPT 中选择 OAuth', '3. Choose OAuth in ChatGPT')}</Text>
      <Paragraph style={{ marginTop: 8, marginBottom: 0 }}>{label('粘贴服务器 URL，选择 OAuth，使用自动客户端注册。随后在本站授权页面输入管理员密码，并选择允许访问的连接配置和权限。', 'Paste the server URL, select OAuth and use automatic client registration. On this site’s authorization page, enter the administrator password and choose the permitted profiles and scopes.')}</Paragraph>
    </div>

    <Collapse items={[{
      key: 'custom-oauth',
      label: label('无法自动注册？生成自定义 OAuth 凭据', 'Automatic registration unavailable? Create custom OAuth credentials'),
      children: <div className="settings-stack" style={{ gap: 14 }}>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>{label('在 ChatGPT 中选择自定义 OAuth 客户端，将页面显示的回调 URL 粘贴到这里。生成后把 Client ID 和 Client Secret 填回 ChatGPT，仍需完成登录授权。', 'Choose a custom OAuth client in ChatGPT and paste its callback URL here. Copy the generated Client ID and Client Secret back to ChatGPT, then complete sign-in and authorization.')}</Paragraph>
        <Alert type="info" showIcon title={label('先解决上方连接检查中的错误', 'Resolve connection check errors first')} description={label('自定义凭据可以替代自动注册，但仍需要正确的公开地址和 OAuth 发现接口。', 'Custom credentials replace automatic registration, but still require the correct public address and OAuth discovery endpoints.')} />
        {!client ? <>
          <div className="form-field"><label htmlFor="mcp-chatgpt-callback">{label('ChatGPT 回调 URL', 'ChatGPT callback URL')}</label><Input id="mcp-chatgpt-callback" value={redirectUri} disabled={creating} status={redirectUri && !callback ? 'error' : undefined} placeholder="https://chatgpt.com/connector/oauth/..." onChange={(event) => { setRedirectUri(event.target.value); setClientError(''); }} />{redirectUri && !callback && <Text type="danger">{label('请粘贴 ChatGPT 提供的完整 HTTPS 回调地址。', 'Paste the complete HTTPS callback URL provided by ChatGPT.')}</Text>}</div>
          <div className="form-field"><label>{label('可申请的权限', 'Scopes the client may request')}</label><Checkbox.Group disabled={creating} value={scopes} options={[{ value: 'read', label: label('查询', 'Read'), disabled: true }, { value: 'trade', label: label('交易', 'Trade') }, { value: 'cancel', label: label('撤单', 'Cancel') }]} onChange={(values) => setScopes([...new Set(['read', ...values])])} /><Text type="secondary">{label('这里只注册客户端，账户访问和交易权限在登录授权时确认。', 'This registers the client only. Account access and trading permissions are chosen during authorization.')}</Text></div>
          {clientError && <Alert type="error" showIcon title={label('客户端创建失败', 'Client creation failed')} description={clientError} />}
          <div><Button type="primary" loading={creating} disabled={busy || !canConnect || !callback} onClick={createClient}>{label('生成并注册客户端', 'Create and register client')}</Button></div>
        </> : <>
          <Alert type="success" showIcon title={label('客户端已注册', 'Client registered')} description={label('复制下方凭据后填回 ChatGPT。密钥只在本次页面显示，请在离开前保存。', 'Copy these credentials into ChatGPT. The secret is shown only on this page; save it before leaving.')} />
          {copyField('Client ID', client.client_id)}
          {copyField('Client Secret', client.client_secret, true)}
          <Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{label('回调地址', 'Callback URL')}: {(client.redirect_uris || [callback]).join(', ')}</Text>
          <Text>{label('客户端认证方式', 'Client authentication method')}: {client.token_endpoint_auth_method || 'client_secret_post'}</Text>
          <Collapse size="small" items={[{ key: 'endpoints', label: label('需要手填其他 OAuth 字段？', 'Need other OAuth fields?'), children: <div className="settings-stack" style={{ gap: 12 }}>
            {copyField(label('授权 URL', 'Authorization URL'), clientConnection.authorization_endpoint)}
            {copyField(label('Token URL', 'Token URL'), clientConnection.token_endpoint)}
            {copyField(label('客户端注册 URL', 'Client registration URL'), clientConnection.registration_endpoint)}
            {copyField(label('授权服务器基础地址 / Issuer', 'Authorization server base URL / Issuer'), clientConnection.oauth_issuer)}
            {copyField(label('资源地址 / Resource', 'Resource URL'), clientConnection.resource || clientConnection.url)}
            {copyField(label('权限范围 / Scopes', 'Scopes'), client.scope || scopes.join(' '))}
            <Text type="secondary">{label('基础 Scope 为 read；需要交易或撤单时再添加已注册的 trade、cancel。', 'The base scope is read. Add the registered trade or cancel scopes only when needed.')}</Text>
            <Space><Text>OIDC</Text><Tag>{label('关闭', 'Disabled')}</Tag><Text type="secondary">{label('无需请求 openid 权限。', 'Do not request the openid scope.')}</Text></Space>
          </div> }]} />
          <div><Button onClick={() => { setRegistration(null); setClientError(''); }}>{label('清除本页凭据显示', 'Clear credentials from this page')}</Button><Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0 }}>{label('清除显示不会删除客户端或撤销已完成的授权。', 'Clearing this display does not delete the client or revoke an existing authorization.')}</Paragraph></div>
        </>}
      </div>,
    }]} />
  </div>;
}
