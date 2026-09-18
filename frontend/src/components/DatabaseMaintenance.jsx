import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Popconfirm, Select, Space, Table, Typography, Upload, message } from 'antd';
import { api } from '../lib/api';

const retentionDate = (days) => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
};

const bytes = (n) => n == null ? '未知' : `${(n / 1024 / 1024).toFixed(2)} MiB`;

export default function DatabaseMaintenance() {
  const [report, setReport] = useState(null);
  const [imported, setImported] = useState(null);
  const [tables, setTables] = useState([]);
  const [before, setBefore] = useState('');
  const [configId, setConfigId] = useState('');
  const [targets, setTargets] = useState([]);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const chooseRetention = (days) => {
    setBefore(retentionDate(days));
    setTables((report?.tables || []).filter((row) => row.cleanable && ['scheduler_runs', 'news_snapshots', 'token_usage'].includes(row.table)).map((row) => row.table));
    setConfigId('');
    setPreview(null);
  };
  const fail = (e) => message.error(e.response?.data?.detail || e.message);
  const load = async () => setReport((await api.get('/database/analysis')).data);
  useEffect(() => {
    let mounted = true;
    api.get('/database/cleanup-targets').then((response) => { if (mounted) setTargets(response.data.targets || []); }).catch(fail);
    api.get('/database/analysis').then((response) => { if (mounted) setReport(response.data); }).catch(fail);
    return () => { mounted = false; };
  }, []);
  const run = async (operation) => {
    setBusy(true);
    try { await operation(); } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const payload = { tables, before: `${before} 00:00:00`, config_id: configId.trim() || null };
  const shown = imported || report;
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <Alert type="info" showIcon message="数据库诊断与保留策略"
      description="清理只处理选定截止时间之前的数据，至少保留最近 7 天。成交、订单关联和保护证据保留。操作前自动备份，备份另占磁盘空间。" />
    <Space wrap>
      <Button loading={busy} onClick={() => run(async () => { setImported(null); await load(); })}>刷新当前库</Button>
      <Upload showUploadList={false} accept=".db,.sqlite" beforeUpload={(file) => {
        run(async () => {
          setImported((await api.post('/database/analyze-import', file, { headers: { 'Content-Type': 'application/octet-stream' } })).data);
        });
        return false;
      }}><Button disabled={busy}>导入副本，仅分析</Button></Upload>
      <Popconfirm title="回收当前数据库空闲页？数据库繁忙时可能失败，可稍后重试。"
        onConfirm={() => run(async () => { await api.post('/database/compact'); await load(); message.success('空间回收完成'); })}>
        <Button disabled={busy || !!imported}>回收空闲空间</Button>
      </Popconfirm>
      <Popconfirm title="备份后从本地成交重建持仓周期？不连接交易所，不更改原始成交。" onConfirm={() => run(async () => {
        const result = (await api.post('/database/rebuild-history')).data;
        await load(); message.success(`重建完成：${result.results.reduce((n, r) => n + r.completed, 0)} 个周期；备份 ${result.backup}`, 10);
      })}><Button disabled={busy || !!imported}>重建持仓历史</Button></Popconfirm>
    </Space>
    {shown && <Card title={imported ? '导入副本分析（未合并到当前库）' : '当前数据库'}>
      <Typography.Paragraph style={{ overflowWrap: 'anywhere' }}>数据库：{shown.database_path || '未知'}</Typography.Paragraph>
      <Space wrap style={{ marginBottom: 16 }}>
        <Typography.Text>完整性：{shown.integrity}</Typography.Text>
        <Typography.Text>文件：{bytes(shown.file_bytes)}</Typography.Text>
        <Typography.Text>可回收：{bytes(shown.reclaimable_bytes)}</Typography.Text>
        <Typography.Text>WAL：{bytes(shown.wal_bytes)}</Typography.Text>
        <Typography.Text>未关联成交：{shown.unlinked_fills ?? '未知'}</Typography.Text>
      </Space>
      <Table size="small" rowKey="table" dataSource={shown.tables} scroll={{ x: 760 }} columns={[
        { title: '数据表', dataIndex: 'table' }, { title: '记录数', dataIndex: 'rows', sorter: (a, b) => a.rows - b.rows },
        { title: '占用', dataIndex: 'bytes', render: bytes }, { title: '最早', dataIndex: 'oldest' },
        { title: '最新', dataIndex: 'newest' }, { title: '可清理', dataIndex: 'cleanable', render: (v) => v ? '是' : '保留' },
      ]} />
      <Typography.Text type="secondary">{shown.size_note}</Typography.Text>
    </Card>}
    {!imported && <Card title="清理当前库历史数据">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Paragraph type="secondary">快捷选择旧任务运行记录、新闻缓存与用量明细。先预览，再备份清除。</Typography.Paragraph>
        <Space wrap>{[7, 30, 90].map((days) => <Button key={days} disabled={busy || !report} onClick={() => chooseRetention(days)}>保留最近 {days} 天</Button>)}</Space>
        <Select mode="multiple" placeholder="选择数据类别" value={tables} onChange={(v) => { setTables(v); setPreview(null); }} style={{ width: '100%' }}
          options={(report?.tables || []).filter((r) => r.cleanable).map((r) => ({ value: r.table, disabled: !!configId && !r.task_scoped, label: `${r.table}（${r.rows} 条）` }))} />
        <Space wrap>
          <Input type="date" aria-label="清理截止日期" value={before} onChange={(e) => { setBefore(e.target.value); setPreview(null); }} />
          <Select showSearch optionFilterProp="label" allowClear placeholder="全部任务 / 选择历史遗留任务" style={{ minWidth: 220, maxWidth: "100%" }} value={configId || undefined} options={targets.map((target) => ({ value: target.config_id, label: `${target.orphaned ? "历史遗留 · " : ""}${target.config_id}（${target.rows} 条）` }))} onChange={(value) => { setConfigId(value || ""); if (value) setTables((current) => current.filter((table) => report?.tables?.find((row) => row.table === table)?.task_scoped)); setPreview(null); }} />
          <Button disabled={busy || !tables.length || !before} onClick={() => run(async () => {
            setPreview((await api.post('/database/cleanup-preview', payload)).data);
          })}>预览影响</Button>
        </Space>
        {preview && <>
          <Alert type="warning" message={preview.notice} description={Object.entries(preview.counts).map(([t, n]) => `${t}: ${n} 条`).join('；')} />
          <Popconfirm title="按此预览备份并删除？" onConfirm={() => run(async () => {
            const result = (await api.post('/database/cleanup', { ...payload, preview_token: preview.preview_token })).data;
            setPreview(null); await load(); message.success(`已清理。备份：${result.backup}`, 10);
          })}><Button danger loading={busy} disabled={busy || !Object.values(preview.counts).some((count) => count > 0)}>备份并删除所选历史</Button></Popconfirm>
        </>}
      </Space>
    </Card>}
  </Space>;
}
