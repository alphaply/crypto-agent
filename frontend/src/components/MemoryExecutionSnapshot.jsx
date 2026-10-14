import { useEffect, useState } from 'react';
import { Alert, Button, Popconfirm, Space, Typography, message } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import { api } from '../lib/api';

export default function MemoryExecutionSnapshot({ memory, authenticated, locale, onCleared }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [cleared, setCleared] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const zh = locale === 'zh';
  const content = detail?.position_summary || '';
  useEffect(() => {
    if (!open || !authenticated || !memory.id) return undefined;
    const controller = new AbortController();
    api.get(`/history/short-memories/${encodeURIComponent(memory.id)}/details`, { signal: controller.signal, silent: true }).then(({ data }) => { setDetail(data.short_memory); setError(''); }).catch((err) => { if (err.code !== 'ERR_CANCELED') setError(err.message); });
    return () => controller.abort();
  }, [open, authenticated, memory.id, retry]);
  const matchesCleared = cleared?.config_id === memory.config_id
    && cleared?.bucket_start === memory.bucket_start && cleared?.content === content;
  if (!authenticated || !memory.id) return null;

  const clear = async () => {
    setBusy(true);
    try {
      const identity = { config_id: memory.config_id, bucket_start: memory.bucket_start };
      await api.delete('/history/short-memories/execution-snapshot', { data: identity });
      setCleared({ ...identity, content });
      onCleared?.(identity);
      window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
      message.success(zh ? '成交事实快照已清除，策略记忆已保留' : 'Snapshot cleared. Strategy memory retained.');
    } catch (error) {
      const detail = error.response?.data?.detail;
      message.error(typeof detail === 'string' ? detail : error.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="content-disclosure memory-execution-snapshot" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{zh ? '历史原文与审计详情' : 'Historical text and audit details'}</summary>
      <Space direction="vertical" size={12} style={{ width: '100%', minWidth: 0 }}>
        <Typography.Text type="secondary">
          {zh ? '历史原文仅供审计，不是当前持仓，也不进入新的策略记忆。后台负责对账与恢复。' : 'Historical text is for audit, not current exposure, and is excluded from new strategy memory. Reconciliation and recovery run in the background.'}
        </Typography.Text>
        {content && !matchesCleared ? (
          <Popconfirm
            title={zh ? '清除此条记忆的成交事实快照？' : 'Clear this memory’s execution snapshot?'}
            description={zh ? '策略记忆正文和原始交易记录会保留。' : 'Strategy memory and original trade records will be retained.'}
            okText={zh ? '清除快照' : 'Clear snapshot'} cancelText={zh ? '取消' : 'Cancel'}
            onConfirm={clear} disabled={busy}
          >
            <Button danger size="small" loading={busy}>{zh ? '清除快照' : 'Clear snapshot'}</Button>
          </Popconfirm>
        ) : null}
        {open && (error ? <Alert type="error" title={error} action={<Button onClick={() => setRetry((old) => old + 1)}>{zh ? '重试' : 'Retry'}</Button>} /> : !detail ? <Typography.Text>{zh ? '正在读取历史详情…' : 'Loading historical details…'}</Typography.Text> : <div className="memory-snapshot-content" tabIndex={0} role="region" aria-label={zh ? '历史记忆原文' : 'Original historical memory'}><Typography.Text type="secondary">{detail.window_start || detail.bucket_start} → {detail.window_end || detail.bucket_end}</Typography.Text><MarkdownBlock content={detail.market_summary || ''} />{!matchesCleared && <MarkdownBlock content={content} />}</div>)}
      </Space>
    </details>
  );
}
