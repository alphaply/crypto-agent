import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Input, Popconfirm, Space, Tag, Typography } from 'antd';
import { ReloadOutlined, SafetyOutlined, SwapOutlined } from '@ant-design/icons';
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

  const scopes = data?.management?.scopes || [];
  const recoveryTargets = data?.recovery_targets || [];

  return (
    <div className="lifecycle-panel">
      <div className="lifecycle-panel__header">
        <Space size={8} wrap>
          <SafetyOutlined style={{ color: 'var(--accent-strong)' }} />
          <Typography.Text strong>{zh ? '管理状态与交接' : 'Management and transfer'}</Typography.Text>
          {data && (
            <Tag color={scopes.length ? 'blue' : 'default'}>
              {scopes.length ? (zh ? `${scopes.length} 个已纳管范围` : `${scopes.length} managed scopes`) : (zh ? '无活跃纳管范围' : 'No active scopes')}
            </Tag>
          )}
        </Space>
        <Button size="small" icon={<ReloadOutlined />} loading={busy} onClick={load}>
          {zh ? '刷新管理状态' : 'Refresh management'}
        </Button>
      </div>

      <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
        {zh
          ? '暂停保留管理权。释放会核验仓位、挂单及未决写入；交接由接收任务承担原有资源和恢复责任。'
          : 'Pausing retains ownership. Release verifies exposure, orders and unresolved writes. A transfer hands their management and recovery responsibility to the receiving task.'}
      </Typography.Text>

      {data && <ManagementStatus management={data.management} />}

      {Boolean(scopes.length) && (
        <div className="lifecycle-panel__scopes">
          <div className="lifecycle-transfer-box">
            <Space size={6}>
              <SwapOutlined style={{ color: 'var(--text-muted)' }} />
              <Typography.Text strong style={{ fontSize: 12.5 }}>
                {zh ? '如需交接给其他任务（可选）' : 'Transfer to another task (optional)'}
              </Typography.Text>
            </Space>
            <Input
              aria-label={zh ? '接收任务或配置 ID' : 'Receiving task or profile ID'}
              value={target}
              onChange={(event) => { setTarget(event.target.value); setAccepted(false); }}
              placeholder={zh ? '接收任务 ID；MCP 配置使用 mcp:配置ID' : 'Task ID; use mcp:profileID for MCP'}
            />
            <Checkbox checked={accepted} onChange={(event) => setAccepted(event.target.checked)}>
              {zh ? '确认接收任务接受资源与未决操作的恢复责任' : 'Confirm the receiving task accepts resources and unresolved recovery'}
            </Checkbox>
          </div>

          {scopes.map((scope) => (
            <div className="lifecycle-scope-row" key={scope.claim_id}>
              <Space size={6} wrap>
                <Tag color="blue" style={{ margin: 0 }}>{scope.symbol || scope.resource}</Tag>
                <Tag style={{ margin: 0 }}>{scope.side}</Tag>
              </Space>
              <Space size={8} wrap>
                <Popconfirm
                  title={zh ? '核验无仓位、挂单和未决写入后释放此范围？' : 'Release this scope after verifying no exposure, orders or unresolved writes?'}
                  onConfirm={() => act(scope, 'release')}
                >
                  <Button danger size="small" disabled={busy}>
                    {zh ? '核验并释放' : 'Verify and release'}
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title={zh ? '确认向此任务交接管理与恢复责任？' : 'Transfer management and recovery responsibility to this task?'}
                  onConfirm={() => act(scope, 'transfer')}
                >
                  <Button size="small" disabled={busy || !accepted || !target.trim() || target.trim() === configId}>
                    {zh ? '接受并交接' : 'Accept and transfer'}
                  </Button>
                </Popconfirm>
              </Space>
            </div>
          ))}
        </div>
      )}

      {recoveryTargets.map((item) => (
        <div key={item.recovery_id} className="lifecycle-recovery-item">
          <div>
            <Typography.Text strong style={{ fontSize: 12.5 }}>
              {zh ? '已接受恢复身份' : 'Accepted recovery identity'}: {item.recovery_id}
            </Typography.Text>
            <br />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {item.operation_id} · {item.source_owner || item.original_owner}
            </Typography.Text>
          </div>
          <Button size="small" disabled={busy} onClick={() => inspect(item.recovery_id)}>
            {zh ? '核验原操作' : 'Inspect original operation'}
          </Button>
        </div>
      ))}

      {error && (
        <Alert
          type="warning"
          showIcon
          title={zh ? '管理权保留，操作未完成' : 'Ownership retained; action incomplete'}
          description={<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0, fontSize: 12 }}>{typeof error === 'string' ? error : JSON.stringify(error, null, 2)}</pre>}
        />
      )}

      {receipt && (
        <details open>
          <summary style={{ cursor: 'pointer', fontSize: 12.5, fontWeight: 600 }}>
            {zh ? '管理操作详情' : 'Management operation details'}
          </summary>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto', margin: '8px 0 0', padding: 10, borderRadius: 8, background: 'var(--bg-subtle)', fontSize: 12 }}>
            {JSON.stringify(receipt, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
