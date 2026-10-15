import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Typography } from 'antd';
import { api } from '../lib/api';

export default function DecisionMemoryStatus({ configId, refreshKey, locale = 'zh' }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const sequence = useRef(0);
  const inFlight = useRef(false);
  const zh = locale === 'zh';

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    const load = async () => {
      const id = ++sequence.current;
      try {
        const { data } = await api.get('/history/short-memories/projections/status', {
          params: { config_id: configId }, signal: controller.signal,
        });
        if (controller.signal.aborted || id !== sequence.current) return;
        setStatus(data);
        setError('');
        if (data.pending || data.not_generated) timer = window.setTimeout(load, 30000);
      } catch (err) {
        if (!controller.signal.aborted && id === sequence.current) setError(err.message);
      }
    };
    load();
    const invalidateRequests = () => { ++sequence.current; };
    return () => { controller.abort(); window.clearTimeout(timer); invalidateRequests(); };
  }, [configId, refreshKey, revision]);

  const retry = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const id = ++sequence.current;
    try {
      const { data } = await api.post('/history/short-memories/projections/retry', { config_id: configId });
      if (id !== sequence.current) return;
      setStatus(data);
      setError('');
      setRevision((value) => value + 1);
    } catch (err) {
      if (id === sequence.current) setError(err.message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  if (status?.enabled === false) return null;
  const published = status?.published || {};
  const asOf = (kind) => (published[kind] || []).map((item) => item.as_of).join(' / ') || (zh ? '尚未发布' : 'Not published');
  return <Alert data-testid="decision-memory-status" type={error || status?.failed || status?.read_errors?.length ? 'warning' : 'info'}
    showIcon title={zh ? '决策可用记忆' : 'Memory available to decisions'}
    description={<Space direction="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
      {status ? <>
        <Typography.Text>{zh ? '短期策略截至：' : 'Strategy as of: '}{asOf('short_memory')}</Typography.Text>
        <Typography.Text>{zh ? '近期已发布摘要截至：' : 'Published summaries as of: '}{asOf('strategy_summary')}</Typography.Text>
        <Typography.Text>{zh
          ? `待整理 ${status.pending} · 失败 ${status.failed}`
          : `Queued ${status.pending} · Failed ${status.failed}`}</Typography.Text>
      </> : <Typography.Text>{zh ? '正在读取状态…' : 'Loading status…'}</Typography.Text>}
      <Typography.Text type="secondary">{zh
        ? '整理和重试期间继续使用有效的已发布内容。下方保留完整历史原文。'
        : 'Valid published content remains available during consolidation and retries. Full original history is retained below.'}</Typography.Text>
      {error ? <Typography.Text role="alert">{error}</Typography.Text> : null}
      {status?.read_errors?.length ? <Typography.Text role="alert">{zh ? '部分来源读取失败：' : 'Some sources could not be read: '}{status.read_errors.join(', ')}</Typography.Text> : null}
      {status?.errors?.length ? <details><summary>{zh ? '查看整理错误' : 'Consolidation errors'}</summary>
        {status.errors.map((item) => <div key={`${item.source_kind}:${item.source_id}`}>{item.as_of} · {item.error}</div>)}
      </details> : null}
      {status?.historical_errors?.length ? <details><summary>{zh ? '历史格式转换记录（已停用）' : 'Retired conversion history'}</summary>
        {status.historical_errors.map((item) => <div key={`${item.source_kind}:${item.source_id}`}>{item.as_of} · {item.error}</div>)}
      </details> : null}
      <Space wrap>
        <Button size="small" disabled={busy} onClick={() => setRevision((value) => value + 1)}>{zh ? '刷新状态' : 'Refresh status'}</Button>
        {status?.failed > 0 ? <Button size="small" loading={busy} onClick={retry}>{zh ? '重试失败整理' : 'Retry failed consolidation'}</Button> : null}
      </Space>
    </Space>} />;
}
