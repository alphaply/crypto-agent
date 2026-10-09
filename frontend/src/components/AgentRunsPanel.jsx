import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Collapse, Descriptions, Drawer, Empty, Select, Space, Spin, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Text, Paragraph } = Typography;
const PAGE_SIZE = 20;
const blockStyle = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12, margin: 0, maxHeight: 560, overflow: 'auto' };
const formatBody = (value) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);

export default function AgentRunsPanel({ agents = [] }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [configId, setConfigId] = useState(undefined);
  const [purpose, setPurpose] = useState(undefined);
  const [page, setPage] = useState(1);
  const [runs, setRuns] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [retention, setRetention] = useState({ days: 30, per_config: 100, news_per_config: 12100 });
  const listSequence = useRef(0);
  const detailSequence = useRef(0);
  const unknown = zh ? '未知' : 'Unknown';
  const purposeLabels = {
    decision: zh ? '交易决策' : 'Trading decision',
    strategy_summary: zh ? '单轮策略压缩' : 'Per-round strategy summary',
    memory_review: zh ? '短期动态记忆' : 'Short-term memory',
    daily_summary: zh ? '日内归档' : 'Daily summary',
    chat: zh ? '聊天' : 'Chat',
    chat_summary: zh ? '聊天上下文压缩' : 'Chat compaction',
    news_score: zh ? '消息相关度评分' : 'News scoring',
    news_summary: zh ? '共享消息摘要' : 'News summary',
  };
  const statusLabels = {
    running: zh ? '调用中' : 'Running', success: zh ? '成功' : 'Success',
    error: zh ? '失败' : 'Error', cancelled: zh ? '已取消' : 'Cancelled',
  };
  const number = (value) => value == null ? unknown : value.toLocaleString();
  const time = (value) => value ? new Date(value).toLocaleString(zh ? 'zh-CN' : 'en-US') : '—';
  const taskName = (id) => id === 'news-intelligence' ? (zh ? '全局消息处理' : 'Shared news pipeline') : agents.find((agent) => agent.config_id === id)?.title || id;
  const statusTag = (status) => <Tag color={{ running: 'processing', success: 'success', error: 'error' }[status]}>{statusLabels[status] || status}</Tag>;
  const costLabel = (run) => run.cost == null ? unknown : <Space direction="vertical" size={0}><Text>{run.cost.toFixed(6)} {run.currency || ''}</Text><Text type="secondary">{run.cost_source === 'model_pricing' ? (zh ? '按配置费率估算' : 'Estimated from configured rates') : run.cost_source === 'provider' ? (zh ? '服务商返回' : 'Provider reported') : unknown}</Text></Space>;

  const load = useCallback(async () => {
    const sequence = ++listSequence.current;
    setLoading(true);
    setError('');
    try {
      const response = await api.get('/agent-runs', { params: { config_id: configId, purpose, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE } });
      if (sequence !== listSequence.current) return;
      setRuns(response.data.runs || []);
      setTotal(response.data.total || 0);
      setRetention(response.data.retention || { days: 30, per_config: 100 });
    } catch (err) {
      if (sequence === listSequence.current) setError(err.message);
    } finally {
      if (sequence === listSequence.current) setLoading(false);
    }
  }, [configId, purpose, page]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    const invalidate = () => { ++listSequence.current; };
    return () => { window.clearTimeout(timer); invalidate(); };
  }, [load]);

  useEffect(() => () => { ++detailSequence.current; }, []);

  const showDetail = async (runId) => {
    const sequence = ++detailSequence.current;
    setSelectedId(runId);
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      const response = await api.get(`/agent-runs/${encodeURIComponent(runId)}`);
      if (sequence === detailSequence.current) setDetail(response.data.run);
    } catch (err) {
      if (sequence === detailSequence.current) setDetailError(err.message);
    } finally {
      if (sequence === detailSequence.current) setDetailLoading(false);
    }
  };

  const columns = [
    { title: zh ? '时间 / 任务' : 'Time / task', key: 'time', render: (_, run) => <Space direction="vertical" size={0}><Text>{time(run.started_at)}</Text><Text type="secondary">{taskName(run.config_id)}</Text></Space> },
    { title: zh ? '用途 / 模型' : 'Purpose / model', key: 'model', render: (_, run) => <Space direction="vertical" size={0}><Text>{purposeLabels[run.purpose] || run.purpose}</Text><Text type="secondary">{run.model}</Text></Space> },
    { title: zh ? '状态' : 'Status', dataIndex: 'status', render: statusTag },
    { title: zh ? '输入字符' : 'Input chars', dataIndex: 'input_chars', render: number },
    { title: 'Token', dataIndex: 'total_tokens', render: number },
    { title: zh ? '费用' : 'Cost', key: 'cost', render: (_, run) => costLabel(run) },
    { title: '', key: 'open', render: (_, run) => <Button size="small" onClick={() => showDetail(run.run_id)}>{zh ? '查看输入 / 输出' : 'Inspect input / output'}</Button> },
  ];

  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Alert type="info" showIcon title={zh ? '交易 Agent 负责决策和执行，记忆模型只整理短期动态记忆。' : 'The trading agent decides and executes; the memory model consolidates short-term memory only.'}
      description={zh ? `这里展示实际发生的模型调用。当前任务保留最近 ${retention.per_config} 次，全局消息单独保留最多 ${retention.news_per_config || 12100} 次，最长 ${retention.days} 天；字符数包含消息和工具定义的 JSON，不等于 Token。费用优先使用服务商返回值，否则按调用时的渠道费率估算（含已报告的缓存用量）；缺少价格或用量显示未知。仅登录后可查看。` : `Actual model calls. Keeps the latest ${retention.per_config} calls per selected task and up to ${retention.news_per_config || 12100} shared news calls for ${retention.days} days. Character counts include message and tool JSON, not tokens. Costs use provider-reported values when available, otherwise configured rates at call time including reported cache usage. Missing prices or usage stay unknown. Login is required.`} />
    <Space wrap>
      <Select allowClear showSearch optionFilterProp="label" aria-label={zh ? '运行记录所属任务' : 'Run task'} value={configId} onChange={(value) => { setConfigId(value); setPage(1); }} placeholder={zh ? '全部任务' : 'All tasks'} style={{ minWidth: 220 }} options={[{ value: 'news-intelligence', label: zh ? '全局消息处理' : 'Shared news pipeline' }, ...agents.filter((agent) => agent.config_id !== 'news-intelligence').map((agent) => ({ value: agent.config_id, label: agent.title || agent.config_id }))]} />
      <Select allowClear aria-label={zh ? '调用用途' : 'Call purpose'} value={purpose} onChange={(value) => { setPurpose(value); setPage(1); }} placeholder={zh ? '全部用途' : 'All purposes'} style={{ minWidth: 190 }} options={Object.entries(purposeLabels).map(([value, label]) => ({ value, label }))} />
      <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>{zh ? '刷新记录' : 'Refresh'}</Button>
    </Space>
    {error ? <Alert type="error" showIcon title={error} /> : null}
    <Table rowKey="run_id" columns={columns} dataSource={runs} loading={loading} size="small" scroll={{ x: 860 }}
      pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: setPage }}
      locale={{ emptyText: <Empty description={zh ? '暂无记录。仅记录更新后发生的实际调用，历史 Prompt 无法补造。' : 'No records yet. Only actual calls after this update are recorded; historical prompts cannot be reconstructed.'} /> }} />
    <Drawer title={zh ? 'Agent 调用详情' : 'Agent call detail'} open={Boolean(selectedId)} width={Math.min(960, window.innerWidth)} onClose={() => { ++detailSequence.current; setSelectedId(null); }}>
      {detailLoading ? <Spin /> : null}
      {detailError ? <Alert type="error" showIcon title={detailError} /> : null}
      {detail ? <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Descriptions size="small" bordered column={2} items={[
          { key: 'purpose', label: zh ? '用途' : 'Purpose', children: purposeLabels[detail.purpose] || detail.purpose },
          { key: 'status', label: zh ? '状态' : 'Status', children: statusTag(detail.status) },
          { key: 'task', label: zh ? '任务' : 'Task', children: taskName(detail.config_id) },
          { key: 'model', label: zh ? '模型' : 'Model', children: detail.model },
          { key: 'start', label: zh ? '开始' : 'Started', children: time(detail.started_at) },
          { key: 'end', label: zh ? '完成' : 'Finished', children: time(detail.finished_at) },
          { key: 'input', label: zh ? '消息 / 工具字符' : 'Message / tool chars', children: `${number(detail.message_chars)} / ${number(detail.tool_chars)}` },
          { key: 'tokens', label: zh ? '输入 / 输出 Token' : 'Input / output tokens', children: `${number(detail.prompt_tokens)} / ${number(detail.completion_tokens)}` },
          { key: 'stop', label: zh ? '停止原因' : 'Stop reason', children: detail.details?.response_metadata?.finish_reason || detail.details?.response_metadata?.stop_reason || detail.details?.completion_status?.finish_reason || detail.details?.completion_status?.stop_reason || detail.details?.response_metadata?.status || detail.details?.completion_status?.status || '—' },
          { key: 'settings', label: zh ? '输出与推理配置' : 'Output / reasoning settings', children: Object.keys(detail.details?.request_settings || {}).length ? <pre style={blockStyle}>{formatBody(detail.details.request_settings)}</pre> : <Text type="secondary">{zh ? '未显式配置；使用模型或服务商默认值' : 'Not explicitly configured; model or provider defaults apply'}</Text> },
          { key: 'cost', label: zh ? '费用' : 'Cost', children: costLabel(detail) },
          { key: 'run', label: 'ID', children: <Text copyable style={{ overflowWrap: 'anywhere' }}>{detail.run_id}</Text> },
        ]} />
        {detail.error ? <Alert type="error" showIcon title={zh ? '调用错误' : 'Call error'} description={<pre style={blockStyle}>{detail.error}</pre>} /> : null}
        <Collapse style={{ width: '100%' }} defaultActiveKey={['messages', 'output']} items={[
          { key: 'messages', label: zh ? '实际输入消息（按发送顺序）' : 'Actual messages (in sent order)', children: <Space direction="vertical" style={{ width: '100%' }} size="middle">{(detail.messages || []).map((item, index) => <div key={index} style={{ width: '100%' }}><Tag>{index + 1} · {item.role || 'unknown'}{item.name ? ` · ${item.name}` : ''}</Tag><pre style={blockStyle}>{formatBody(item.content)}</pre>{item.tool_calls || item.tool_call_id || item.function_call ? <pre style={blockStyle}>{formatBody({ tool_calls: item.tool_calls, tool_call_id: item.tool_call_id, function_call: item.function_call })}</pre> : null}</div>)}</Space> },
          { key: 'tools', label: `${zh ? '本次可用工具' : 'Available tools'} (${detail.tools?.length || 0})`, children: <pre style={blockStyle}>{formatBody(detail.tools)}</pre> },
          { key: 'output', label: zh ? '模型输出' : 'Model output', children: detail.output ? <pre style={blockStyle}>{detail.output}</pre> : <Text type="secondary">{zh ? '本次无文本输出；工具调用可在详情中查看。' : 'No text output for this call; tool calls may appear in details.'}</Text> },
          { key: 'details', label: zh ? '响应元数据、推理与工具记录' : 'Response metadata, reasoning and tools', children: <pre style={blockStyle}>{formatBody(detail.details)}</pre> },
          { key: 'raw', label: zh ? '完整消息 JSON' : 'Complete message JSON', children: <Paragraph copyable={{ text: formatBody(detail.messages) }}><pre style={blockStyle}>{formatBody(detail.messages)}</pre></Paragraph> },
        ]} />
      </Space> : null}
    </Drawer>
  </Space>;
}
