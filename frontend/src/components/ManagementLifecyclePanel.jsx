import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Input, Popconfirm, Space, Tag, Typography } from 'antd';
import { api } from '../lib/api';
import { useLatestRequest } from '../hooks/useLatestRequest';
import { usePreferences } from '../app/usePreferences';
import ManagementStatus from './ManagementStatus';

export default function ManagementLifecyclePanel({ configId }) {
  return configId ? <ManagementLifecycleContent key={configId} configId={configId} /> : null;
}

function ManagementLifecycleContent({ configId }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [receipt, setReceipt] = useState(null);
  const base = `/management/${encodeURIComponent(configId || '')}`;
  const { begin, cancel } = useLatestRequest();
  const load = useCallback(async () => {
    const signal = begin();
    if (!signal) return;
    setBusy(true);
    try {
      const response = await api.get(`/management/${encodeURIComponent(configId)}`, { silent: true, signal });
      if (!signal.aborted) { setData(response.data); setError(null); }
    } catch (err) { if (!signal.aborted) setError(err.response?.data?.detail || err.message); }
    finally { if (!signal.aborted) setBusy(false); }
  }, [configId, begin]);
  useEffect(() => {
    let active = true;
    Promise.resolve().then(() => { if (active) load(); });
    return () => { active = false; cancel(); };
  }, [load, cancel]);
  const act = async (scope, action) => {
    setBusy(true); setError(null); setReceipt(null);
    try {
      const response = await api.post(`${base}/scopes/${encodeURIComponent(scope.claim_id)}/${action}`,
        action === 'release' ? { confirmed: true } : { target_config_id: target.trim(), accepted });
      setReceipt(response.data);
      await load();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setBusy(false); }
  };
  const inspect = async (identity) => {
    setBusy(true);
    try { setReceipt((await api.get(`${base}/recovery/${encodeURIComponent(identity)}`, { silent: true })).data); setError(null); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: '100%', minWidth: 0 }}>
    <Space wrap><Typography.Text strong>{zh ? '管理状态与交接' : 'Management and transfer'}</Typography.Text><Button size="small" loading={busy} onClick={load}>{zh ? '刷新管理状态' : 'Refresh management'}</Button></Space>
    <Typography.Text type="secondary">{zh ? '暂停保留管理权。释放会核验仓位、挂单及未决写入；交接由接收任务承担原有资源和恢复责任。' : 'Pausing retains ownership. Release verifies exposure, orders and unresolved writes. A transfer hands their management and recovery responsibility to the receiving task.'}</Typography.Text>
    {data && <ManagementStatus management={data.management} />}
    {Boolean(data?.management?.scopes?.length) && <>
      <Input aria-label={zh ? '接收任务或配置 ID' : 'Receiving task or profile ID'} value={target} onChange={(event) => { setTarget(event.target.value); setAccepted(false); }} placeholder={zh ? '接收任务 ID；MCP 配置使用 mcp:配置ID' : 'Task ID; use mcp:profileID for MCP'} />
      <Checkbox checked={accepted} onChange={(event) => setAccepted(event.target.checked)}>{zh ? '确认接收任务接受资源与未决操作的恢复责任' : 'Confirm the receiving task accepts resources and unresolved recovery'}</Checkbox>
      {data.management.scopes.map((scope) => <Space wrap key={scope.claim_id}>
        <Tag>{scope.symbol || scope.resource} · {scope.side}</Tag>
        <Popconfirm title={zh ? '核验无仓位、挂单和未决写入后释放此范围？' : 'Release this scope after verifying no exposure, orders or unresolved writes?'} onConfirm={() => act(scope, 'release')}><Button danger size="small" disabled={busy}>{zh ? '核验并释放' : 'Verify and release'}</Button></Popconfirm>
        <Popconfirm title={zh ? '确认向此任务交接管理与恢复责任？' : 'Transfer management and recovery responsibility to this task?'} onConfirm={() => act(scope, 'transfer')}><Button size="small" disabled={busy || !accepted || !target.trim() || target.trim() === configId}>{zh ? '接受并交接' : 'Accept and transfer'}</Button></Popconfirm>
      </Space>)}
    </>}
    {(data?.recovery_targets || []).map((item) => <div key={item.recovery_id} style={{ overflowWrap: 'anywhere' }}>
      <Typography.Text>{zh ? '已接受恢复身份' : 'Accepted recovery identity'}: {item.recovery_id}</Typography.Text><br />
      <Typography.Text type="secondary">{item.operation_id} · {item.source_owner || item.original_owner}</Typography.Text><br />
      <Button size="small" disabled={busy} onClick={() => inspect(item.recovery_id)}>{zh ? '核验原操作' : 'Inspect original operation'}</Button>
    </div>)}
    {error && <Alert type="warning" showIcon title={zh ? '管理权保留，操作未完成' : 'Ownership retained; action incomplete'} description={<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{typeof error === 'string' ? error : JSON.stringify(error, null, 2)}</pre>} />}
    {receipt && <details open><summary>{zh ? '管理操作详情' : 'Management operation details'}</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto' }}>{JSON.stringify(receipt, null, 2)}</pre></details>}
  </Space>;
}
