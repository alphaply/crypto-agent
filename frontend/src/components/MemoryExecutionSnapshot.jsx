import { useState } from 'react';
import { Button, Popconfirm, Space, Typography, message } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import { api } from '../lib/api';

export default function MemoryExecutionSnapshot({ memory, authenticated, locale, onCleared }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [cleared, setCleared] = useState(null);
  const zh = locale === 'zh';
  const content = memory.position_summary || '';
  const matchesCleared = cleared?.config_id === memory.config_id
    && cleared?.bucket_start === memory.bucket_start && cleared?.content === content;
  if (!content || matchesCleared) return null;

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
      <summary>{zh ? '成交事实快照' : 'Execution snapshot'}</summary>
      <Space direction="vertical" size={12} style={{ width: '100%', minWidth: 0 }}>
        <Typography.Text type="secondary">
          {zh ? '整理时保存的历史证据副本，非实时状态。清除仅移除此快照，保留策略记忆和原始交易记录；后续新记忆仍会保存新的快照。'
            : 'Historical evidence saved during consolidation, not live state. Clearing removes only this snapshot; strategy memory and original trade records remain. New memories may save new snapshots.'}
        </Typography.Text>
        {authenticated ? (
          <Popconfirm
            title={zh ? '清除此条记忆的成交事实快照？' : 'Clear this memory’s execution snapshot?'}
            description={zh ? '策略记忆正文和原始交易记录会保留。' : 'Strategy memory and original trade records will be retained.'}
            okText={zh ? '清除快照' : 'Clear snapshot'} cancelText={zh ? '取消' : 'Cancel'}
            onConfirm={clear} disabled={busy}
          >
            <Button danger size="small" loading={busy}>{zh ? '清除快照' : 'Clear snapshot'}</Button>
          </Popconfirm>
        ) : null}
        {open ? <div className="memory-snapshot-content" tabIndex={0} role="region"
          aria-label={zh ? '完整成交事实快照' : 'Full execution snapshot'}><MarkdownBlock content={content} /></div> : null}
      </Space>
    </details>
  );
}
