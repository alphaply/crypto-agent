import React, { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Collapse, Drawer, Empty, Input, Progress, Select, Space, Spin, Steps, Table, Tag, Typography } from 'antd';
import { ExportOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { api } from '../lib/api';
import { NEWS_STAGES, durationLabel, pipelineCounts, pipelineProgress, safeTraceUrl } from '../lib/newsPipeline';
import './NewsPipelineMonitor.css';

const NewsPipelineCharts = lazy(() => import('./NewsPipelineCharts'));
const { Text } = Typography;
const clock = (value) => value ? dayjs(value).format('MM-DD HH:mm:ss') : '—';

export default function NewsPipelineMonitor({ status, history = [], error, refreshing, onRefresh, locale }) {
  const zh = locale === 'zh';
  const [selectedRun, setSelectedRun] = useState('current');
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [audit, setAudit] = useState(null);
  const [auditError, setAuditError] = useState('');
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const detailRequest = useRef(null);
  const auditRequest = useRef(null);
  useEffect(() => () => { detailRequest.current?.abort(); auditRequest.current?.abort(); }, []);

  const viewRun = async (id) => {
    detailRequest.current?.abort();
    setSelectedRun(id); setDetail(null); setDetailError(''); setFilter('all'); setQuery('');
    if (id === 'current') { setDetailLoading(false); return; }
    const controller = new AbortController(); detailRequest.current = controller; setDetailLoading(true);
    try {
      const { data } = await api.get(`/news/runs/${encodeURIComponent(id)}`, { signal: controller.signal, silent: true });
      if (!controller.signal.aborted) setDetail(data.run);
    } catch (err) { if (!controller.signal.aborted) setDetailError(err.message); }
    finally { if (!controller.signal.aborted) setDetailLoading(false); }
  };
  const viewAudit = async (id) => {
    auditRequest.current?.abort();
    const controller = new AbortController(); auditRequest.current = controller;
    setAudit(null); setAuditError(''); setAuditLoading(true); setAuditOpen(true);
    try {
      const { data } = await api.get(`/agent-runs/${encodeURIComponent(id)}`, { signal: controller.signal, silent: true });
      if (!controller.signal.aborted) setAudit(data.run);
    } catch (err) { if (!controller.signal.aborted) setAuditError(err.message); }
    finally { if (!controller.signal.aborted) setAuditLoading(false); }
  };
  const run = (selectedRun === 'current' ? status : detail) || {};
  const counts = pipelineCounts(run);
  const progress = pipelineProgress(run);
  const items = useMemo(() => run.items || [], [run.items]);
  const running = selectedRun === 'current' && run.running === true;
  const state = !run.enabled && run.enabled != null ? 'disabled' : run.status || 'idle';
  const states = zh ? { idle: '等待更新', disabled: '已停用', running: '处理中', success: '已发布', degraded: '部分失败，已发布', error: '处理失败', interrupted: '处理已中断' }
    : { idle: 'Waiting', disabled: 'Disabled', running: 'Processing', success: 'Published', degraded: 'Published with failures', error: 'Failed', interrupted: 'Interrupted' };
  const itemStates = zh ? { pending: '等待评分', scoring: '评分中', scored: '已评分', selected: '已入选', filtered: '已过滤', failed: '评分失败' }
    : { pending: 'Pending', scoring: 'Scoring', scored: 'Scored', selected: 'Selected', filtered: 'Filtered', failed: 'Failed' };
  const colors = { running: 'processing', scoring: 'processing', success: 'success', selected: 'success', failed: 'error', error: 'error', degraded: 'warning', interrupted: 'warning', scored: 'blue' };
  const stages = zh ? ['采集', '评分', '筛选', '摘要', '发布', '完成'] : ['Collect', 'Score', 'Filter', 'Summarize', 'Publish', 'Done'];
  const visible = items.filter((item) => (filter === 'all' || item.status === filter)
    && `${item.title || ''} ${item.source || ''} ${item.error || ''}`.toLowerCase().includes(query.toLowerCase()));
  const sourceHealth = run.source_health || run.sources || {};
  const sources = Array.isArray(sourceHealth) ? sourceHealth : Object.entries(sourceHealth).map(([id, value]) => ({ id, ...value }));
  const traceUrl = safeTraceUrl(run.trace_url);
  const stats = [
    [zh ? '候选消息' : 'Candidates', counts.candidates, zh ? `采集 ${counts.fetched} 条` : `${counts.fetched} collected`],
    [run.scoring_mode === 'off' ? (zh ? '跳过评分' : 'Scoring skipped') : (zh ? '评分成功' : 'Scored'), run.scoring_mode === 'off' ? run.counts?.skipped || 0 : counts.scored, zh ? `缓存命中 ${counts.cached} 条` : `${counts.cached} cached`],
    [zh ? '评分失败' : 'Failed', counts.failed, zh ? '可在下方查看原因' : 'See reasons below'],
    [zh ? '本轮入选' : 'Selected this round', counts.selected, zh ? `已过滤 ${counts.filtered} 条` : `${counts.filtered} filtered`],
    [zh ? '本轮耗时' : 'Duration', durationLabel(run.elapsed_ms), clock(run.started_at || run.last_attempt_at)],
  ];
  return <Card className="panel-card news-pipeline" title={zh ? '消息处理监控' : 'News processing monitor'} extra={<Button icon={<ReloadOutlined />} onClick={onRefresh} loading={refreshing} disabled={status?.running}>{status?.running ? (zh ? '处理中' : 'Processing') : (zh ? '立即更新' : 'Refresh now')}</Button>}>
    <div className="news-pipeline-toolbar">
      <Space wrap><Tag color={colors[state]}>{states[state] || state}</Tag><Text type="secondary">{running ? (zh ? '进度自动更新' : 'Live progress') : (zh ? '上次成功：' : 'Last published: ') + clock(status?.last_success_at)}</Text></Space>
      <Select aria-label={zh ? '选择消息处理批次' : 'Select processing run'} value={selectedRun} onChange={viewRun} className="news-run-select" options={[
        { value: 'current', label: zh ? '当前 / 最近一轮' : 'Current / latest run' },
        ...history.filter((item) => item.run_id).map((item) => ({ value: item.run_id, label: `${clock(item.started_at)} · ${states[item.status] || item.status}` })),
      ]} />
    </div>
    {error && <Alert type="warning" showIcon title={zh ? '状态读取失败，显示最近收到的数据' : 'Status refresh failed; showing last received data'} description={error} />}
    {detailError && <Alert type="error" showIcon title={detailError} />}
    {detailLoading ? <div className="news-pipeline-loading"><Spin /></div> : <>
      {run.summary_reused && <Alert type="info" showIcon title={run.summary_deferred ? (zh ? '新消息已采集，摘要等待最小生成间隔' : 'New articles collected; summary deferred by its interval') : (zh ? '摘要输入未变化，已复用，未调用摘要模型' : 'Summary inputs unchanged; reused without a model call')} description={`${zh ? '摘要生成于' : 'Summary generated at'} ${clock(run.summary_as_of)}`} />}
      {run.error && <Alert type={state === 'error' ? 'error' : 'warning'} showIcon title={state === 'error' ? (zh ? '本轮未发布，继续使用上次成功快照' : 'No new snapshot published; keeping the last successful result') : (zh ? '部分消息未完成处理' : 'Some items could not be processed')} description={state === 'degraded' && counts.failed ? (zh ? `${counts.failed} 条评分失败；已从 ${counts.scored} 条成功结果中筛选 ${counts.selected} 条生成摘要。下方可查看失败原因和调用记录。` : `${counts.failed} items failed scoring. ${counts.selected} of ${counts.scored} successful results were selected for the summary. See the item errors and call records below.`) : run.error} />}
      <div className="news-pipeline-stats">{stats.map(([label, value, caption], index) => <div key={label} className={`news-stat ${index === 2 && counts.failed ? 'news-stat-error' : ''}`}><span>{label}</span><strong>{value}</strong><small>{caption}</small></div>)}</div>
      {run.run_id || run.stage ? <div className="news-pipeline-stages">
        <Steps size="small" current={Math.max(0, NEWS_STAGES.indexOf(run.stage))} status={state === 'error' ? 'error' : run.stage === 'complete' ? 'finish' : 'process'} items={stages.map((title) => ({ title }))} />
        <div className="news-score-progress"><Text type="secondary">{stages[Math.max(0, NEWS_STAGES.indexOf(run.stage))]} · {progress.completed} / {progress.total}</Text><Progress percent={progress.percent} size="small" status={state === 'error' ? 'exception' : running ? 'active' : 'normal'} /></div>
        <Space wrap className="news-trace-links"><Text type="secondary">{zh ? '批次' : 'Run'} {run.run_id?.slice(0, 12) || '—'}</Text>{traceUrl ? <a href={traceUrl} target="_blank" rel="noreferrer"><ExportOutlined /> LangSmith</a> : <Text type="secondary">{zh ? 'LangSmith：' : 'LangSmith: '}{({ disabled: zh ? '未启用，可在运行设置配置' : 'Disabled; configure in runtime settings', error: zh ? '追踪发送失败，本地记录仍可查看' : 'Trace export failed; local records remain available', unavailable: zh ? '追踪暂不可用' : 'Unavailable' })[run.trace_status] || (zh ? '暂无远程追踪链接' : 'No remote trace link')}</Text>}</Space>
      </div> : null}
      {items.length > 0 ? <>
        <Suspense fallback={<div className="news-pipeline-loading"><Spin /></div>}><NewsPipelineCharts items={items} locale={locale} /></Suspense>
        <div className="news-item-toolbar"><Space wrap><Text strong>{zh ? '逐条处理明细' : 'Item processing details'}</Text><Tag>{visible.length} / {items.length}</Tag></Space><Space wrap><Select aria-label={zh ? '筛选消息状态' : 'Filter item status'} value={filter} onChange={setFilter} options={[{ value: 'all', label: zh ? '全部状态' : 'All statuses' }, ...Object.entries(itemStates).map(([value, label]) => ({ value, label }))]} /><Input allowClear prefix={<SearchOutlined />} placeholder={zh ? '搜索标题、来源或失败原因' : 'Search title, source or error'} value={query} onChange={(event) => setQuery(event.target.value)} /></Space></div>
        <Table size="small" rowKey={(row) => row.item_id} dataSource={visible} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 760 }} columns={[
          { title: zh ? '消息' : 'Item', key: 'title', width: 320, render: (_, item) => <div className="news-item-title"><Text>{item.title}</Text><Text type="secondary">{item.source || item.source_id}</Text></div> },
          { title: zh ? '结果' : 'Result', dataIndex: 'status', width: 105, render: (value, item) => <Space direction="vertical" size={2}><Tag color={colors[value]}>{itemStates[value] || value}</Tag>{item.cached && <Text type="secondary">{zh ? '缓存' : 'Cached'}</Text>}</Space> },
          { title: zh ? '相关度' : 'Score', dataIndex: 'score', width: 90, sorter: (a, b) => (a.score ?? -1) - (b.score ?? -1), render: (value) => value == null ? '—' : Number(value).toFixed(1) },
          { title: zh ? '详情 / 追踪' : 'Details / trace', key: 'details', render: (_, item) => <div className="news-item-detail">{item.error && <Text type="danger">{item.error}</Text>}<Space wrap>{item.run_id && <Button size="small" type="link" onClick={() => viewAudit(item.run_id)}>{zh ? '调用记录' : 'Call record'}</Button>}{safeTraceUrl(item.trace_url) && <a href={safeTraceUrl(item.trace_url)} target="_blank" rel="noreferrer">LangSmith <ExportOutlined /></a>}{!item.run_id && !item.error && !item.trace_url && <Text type="secondary">{item.status === 'filtered' ? (zh ? '低于阈值或超出入选上限' : 'Below threshold or selection limit') : '—'}</Text>}</Space></div> },
        ]} />
      </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={running ? (zh ? '正在采集消息，评分明细即将出现' : 'Collecting sources; scoring details will appear shortly') : (zh ? '完成一次更新后，即可查看评分分布、来源统计和逐条结果。' : 'Run a refresh to see score distributions, source statistics and item results.')} />}
      {sources.length > 0 && <Collapse className="news-source-health" items={[{ key: 'sources', label: zh ? `采集来源状态 · ${sources.length}` : `Source health · ${sources.length}`, children: <Table size="small" pagination={false} rowKey={(item) => item.id || item.source_id || item.name} dataSource={sources} columns={[
        { title: zh ? '来源' : 'Source', render: (_, item) => item.name || item.id || item.source_id },
        { title: zh ? '状态' : 'Status', dataIndex: 'status', render: (value) => <Tag color={value === 'ok' ? 'success' : value === 'error' ? 'error' : undefined}>{value}</Tag> },
        { title: zh ? '条数' : 'Items', render: (_, item) => item.item_count ?? item.count ?? '—' },
        { title: zh ? '详情' : 'Detail', render: (_, item) => item.error || item.message || '—' },
      ]} scroll={{ x: 500 }} /> }]} />}
    </>}
    <Drawer title={zh ? '消息评分调用记录' : 'News scoring call'} open={auditOpen} width={800} onClose={() => { auditRequest.current?.abort(); setAuditOpen(false); }}>
      {auditLoading && <Spin />}{auditError && <Alert type="error" showIcon title={auditError} />}
      {audit && <div className="settings-stack"><Space wrap><Tag>{audit.model}</Tag><Tag>{audit.status}</Tag><Text type="secondary">{clock(audit.started_at)}</Text></Space>{audit.error && <Alert type="error" showIcon title={audit.error} />}<Text strong>{zh ? '请求' : 'Request'}</Text><pre className="settings-code">{JSON.stringify(audit.messages || [], null, 2)}</pre><Text strong>{zh ? '响应 / 校验信息' : 'Response / validation'}</Text><pre className="settings-code">{audit.output || JSON.stringify(audit.details || {}, null, 2)}</pre></div>}
    </Drawer>
  </Card>;
}
