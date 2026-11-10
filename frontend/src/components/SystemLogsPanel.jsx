import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Collapse, Descriptions, Empty, Input, Select, Space, Spin, Switch, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Text, Paragraph } = Typography;
const blockStyle = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 480, overflow: 'auto', margin: 0, fontSize: 12 };
const bytes = (value) => `${((value || 0) / 1024 / 1024).toFixed(2)} MB`;

export default function SystemLogsPanel() {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [level, setLevel] = useState('INFO');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState({ min_level: 'INFO', query: '' });
  const [entries, setEntries] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [storage, setStorage] = useState(null);
  const [error, setError] = useState('');
  const [statusError, setStatusError] = useState('');
  const [loading, setLoading] = useState(false);
  const [scanLimited, setScanLimited] = useState(false);
  const [live, setLive] = useState(false);
  const request = useRef(null);
  const time = (value) => value ? new Date(value).toLocaleString(zh ? 'zh-CN' : 'en-US') : (zh ? '暂无记录' : 'No record');

  const load = useCallback(async (olderCursor = null) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true); setError('');
    const results = await Promise.allSettled([
      api.get('/logs', { params: { ...filter, cursor: olderCursor || undefined, limit: 100 }, signal: controller.signal, silent: true }),
      api.get('/logs/status', { signal: controller.signal, silent: true }),
    ]);
    if (controller.signal.aborted) return;
    const [logs, status] = results;
    if (logs.status === 'fulfilled') {
      const data = logs.value.data;
      setEntries((previous) => olderCursor ? [...previous, ...data.entries] : data.entries);
      setCursor(data.next_cursor); setScanLimited(data.scan_limited);
    } else setError(logs.reason.message);
    if (status.status === 'fulfilled') { setStorage(status.value.data); setStatusError(''); }
    else setStatusError(status.reason.message);
    setLoading(false);
  }, [filter]);

  useEffect(() => {
    const timer = window.setTimeout(() => load(), 0);
    return () => { window.clearTimeout(timer); request.current?.abort(); };
  }, [load]);
  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setInterval(() => load(), 10000);
    return () => window.clearInterval(timer);
  }, [live, load]);

  const maintenance = storage?.maintenance || {};
  const deleted = maintenance.deleted;
  const policy = storage?.policy;
  return <Space direction="vertical" size="large" style={{ width: '100%', minWidth: 0 }}>
    <Card className="panel-card" title={zh ? '系统日志' : 'System logs'} extra={<Button aria-label={zh ? '刷新日志' : 'Refresh logs'} icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>{zh ? '刷新' : 'Refresh'}</Button>}>
      <Paragraph type="secondary">{zh ? '查看服务器最近的运行记录。可按错误级别筛选，也可搜索模块名、批次编号或报错内容。展开记录可查看完整错误堆栈。' : 'Browse recent server activity. Filter severity or search module, run ID and error text. Expand an entry to inspect its stack trace.'}</Paragraph>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16, minWidth: 0 }}>
        <Select aria-label={zh ? '日志级别' : 'Log severity'} value={level} onChange={setLevel} style={{ width: 170 }} options={[
          { value: 'DEBUG', label: zh ? '全部级别' : 'All levels' },
          { value: 'INFO', label: zh ? '信息及以上' : 'Info and above' },
          { value: 'WARNING', label: zh ? '警告及错误' : 'Warnings and errors' },
          { value: 'ERROR', label: zh ? '仅错误' : 'Errors' },
        ]} />
        <Input aria-label={zh ? '搜索日志' : 'Search logs'} placeholder={zh ? '模块、批次编号或错误内容' : 'Module, run ID or error'} value={query} maxLength={200} onChange={(e) => setQuery(e.target.value)} onPressEnter={() => setFilter({ min_level: level, query })} style={{ flex: '1 1 220px', minWidth: 0, width: 'auto', maxWidth: 400 }} allowClear />
        <Button aria-label={zh ? '筛选日志' : 'Filter logs'} onClick={() => setFilter({ min_level: level, query })}>{zh ? '筛选' : 'Filter'}</Button>
        <Space><Switch checked={live} onChange={setLive} aria-label={zh ? '自动刷新日志' : 'Auto-refresh logs'} /><Text>{zh ? '每10秒刷新' : 'Refresh every 10s'}</Text></Space>
      </div>
      {error && <Alert type="error" showIcon title={zh ? '日志读取失败' : 'Unable to read logs'} description={error} style={{ marginBottom: 12 }} />}
      {scanLimited && <Alert type="info" showIcon title={zh ? '已达到本次扫描范围，可继续查看更早日志' : 'Scan limit reached; continue to older logs'} style={{ marginBottom: 12 }} />}
      {entries.length ? <Collapse items={entries.map((entry) => ({ key: entry.id, label: <Space wrap style={{ minWidth: 0 }}><Tag color={['ERROR', 'CRITICAL'].includes(entry.level) ? 'red' : entry.level === 'WARNING' ? 'orange' : 'default'}>{entry.level}</Tag><Text>{entry.timestamp}</Text><Text strong style={{ overflowWrap: 'anywhere' }}>{entry.module}</Text><Text ellipsis style={{ maxWidth: 'min(420px, 54vw)' }}>{entry.message.split('\n')[0]}</Text></Space>, children: <><Space wrap style={{ marginBottom: 8 }}><Text type="secondary">{entry.file}</Text><Text copyable={{ text: entry.message }}>{zh ? '复制日志' : 'Copy entry'}</Text></Space>{entry.truncated && <Alert type="info" title={zh ? '这条记录过长，页面仅显示前16000字符' : 'Long entry: showing the first 16,000 characters'} />}<pre style={blockStyle}>{entry.message}</pre></> }))} /> : !loading && !error ? <Empty description={zh ? '当前范围没有匹配的日志' : 'No matching entries in this range'} /> : null}
      {loading && <Spin style={{ marginTop: 12 }} />}
      {cursor && <Button style={{ marginTop: 16 }} disabled={loading} onClick={() => { setLive(false); load(cursor); }}>{zh ? '查看更早日志' : 'Load older entries'}</Button>}
    </Card>
    <Card className="panel-card" title={zh ? '自动清理与保留规则' : 'Automatic cleanup and retention'}>
      {statusError && <Alert type="error" showIcon title={statusError} />}
      {maintenance.error && <Alert type="error" showIcon title={maintenance.status === 'unavailable' ? (zh ? '清理状态读取失败' : 'Unable to read cleanup status') : (zh ? '自动清理未完成' : 'Cleanup did not complete')} description={maintenance.error} style={{ marginBottom: 12 }} />}
      <Descriptions column={{ xs: 1, md: 2 }} items={[
        { key: 'last', label: zh ? '上次清理成功' : 'Last successful cleanup', children: time(maintenance.last_success_at) },
        { key: 'attempt', label: zh ? '最近检查' : 'Last attempt', children: <Space wrap><Text>{time(maintenance.last_attempt_at)}</Text>{maintenance.status && <Tag color={maintenance.status === 'success' ? 'green' : maintenance.status === 'error' ? 'red' : undefined}>{({ success: zh ? '已完成' : 'Completed', error: zh ? '失败' : 'Failed', running: zh ? '执行中' : 'Running', unavailable: zh ? '无法读取' : 'Unavailable' })[maintenance.status] || maintenance.status}</Tag>}</Space> },
        { key: 'size', label: zh ? '文件日志占用' : 'File storage', children: storage ? `${bytes(storage.total_bytes)} · ${storage.files.length} ${zh ? '份' : 'files'}` : '—' },
        { key: 'removed', label: zh ? '最近清理结果' : 'Last cleanup result', children: deleted ? (zh ? `调用记录 ${deleted.agent_runs} 条，处理批次 ${deleted.news_processing_runs} 条，旧文件 ${deleted.log_backups} 份` : `${deleted.agent_runs} calls, ${deleted.news_processing_runs} runs, ${deleted.log_backups} files`) : '—' },
      ]} />
      {policy && <Paragraph type="secondary" style={{ marginBottom: 0 }}>{zh ? `文件每份最多 ${bytes(policy.max_file_bytes)}，保留 ${policy.backups} 份备份；模型调用记录保留 ${policy.agent_run_days} 天，每个普通任务最多 ${policy.agent_runs_per_config} 条，消息模型调用最多 ${policy.news_model_calls} 条；新闻处理记录保留最近 ${policy.news_processing_runs} 轮。后台每小时检查，运行记录持续写入时也按原规则清理。` : `Log files rotate at ${bytes(policy.max_file_bytes)}, with ${policy.backups} backups. Model calls: ${policy.agent_run_days} days, up to ${policy.agent_runs_per_config} per ordinary task and ${policy.news_model_calls} for news. Keep the latest ${policy.news_processing_runs} news runs. Background cleanup checks hourly; normal writes also enforce retention.`}</Paragraph>}
    </Card>
  </Space>;
}
