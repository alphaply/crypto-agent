import { useState } from 'react';
import { Alert, Button, Space, Typography } from 'antd';
import { api } from '../lib/api';

export default function AuditDisclosure({ url, zh, title, views = true }) {
  const [page, setPage] = useState(null);
  const [view, setView] = useState('original');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const load = async (offset = 0, nextView = view, version = page?.version) => {
    setLoading(true);
    try {
      const { data } = await api.get(url, { params: { view: nextView, offset, length: 2048, encoding: 'text', ...(version ? { version } : {}) } });
      setPage(data); setView(nextView); setError('');
    } catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setLoading(false); }
  };
  const text = page?.text ?? (page?.content ? new TextDecoder().decode(Uint8Array.from(atob(page.content), char => char.charCodeAt(0))) : '');
  return <details className="content-disclosure" onToggle={(event) => {
    if (event.currentTarget.open && !page && !loading) load(0, 'original', null);
  }}>
    <summary>{title || (zh ? '完整回执详情' : 'Complete receipt evidence')}</summary>
    <Space wrap style={{ marginBlock: 8 }}>
      {views && <><Button size="small" disabled={loading || view === 'original'} onClick={() => load(0, 'original', null)}>{zh ? '原始版本' : 'Original'}</Button>
        <Button size="small" disabled={loading || view === 'latest'} onClick={() => load(0, 'latest', null)}>{zh ? '最新版本' : 'Latest'}</Button></>}
      <Button size="small" disabled={loading || !page || page.offset === 0} onClick={() => load(Math.max(0, page.offset - 2048))}>{zh ? '上一页' : 'Previous'}</Button>
      <Button size="small" disabled={loading || !page?.has_more} onClick={() => load(page.next_offset)}>{zh ? '下一页' : 'Next'}</Button>
      <Button size="small" disabled={loading} onClick={() => load(0, view, null)}>{zh ? '重新读取' : 'Reload'}</Button>
      {loading && <Typography.Text type="secondary">{zh ? '读取中' : 'Loading'}</Typography.Text>}
    </Space>
    {error && <Alert type="error" title={error} />}
    {page && <>
      <Typography.Paragraph type="secondary">{zh ? '只读审计 · 当前页偏移' : 'Read-only evidence · page offset'}: {page.offset} · {zh ? '固定版本' : 'Fixed version'}: {page.version}
        {page.observed_at ? ` · ${new Date(page.observed_at * 1000).toLocaleString()}` : ''}</Typography.Paragraph>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflow: 'auto' }}>{text}</pre>
    </>}
  </details>;
}
