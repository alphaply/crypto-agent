import { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Segmented, Skeleton, Space, Tag, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { WorkspacePanel } from '../components/AgentWorkspace';
import ScheduleDetails from '../components/ScheduleDetails';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { useChartTimeframe } from '../hooks/useChartTimeframe';
import { usePreferences } from '../app/usePreferences';
import { api } from '../lib/api';
import { filterAgents, selectAgentId, workspaceSignature } from '../lib/dashboard';
import './AgentsPage.css';

function AgentStatus({ agent, zh }) {
  const status = agent.execution?.status;
  const active = ['RUNNING', 'QUEUED'].includes(status);
  const text = status === 'RUNNING' ? (zh ? '执行中' : 'Running') : status === 'QUEUED' ? (zh ? '排队中' : 'Queued') : status === 'FAILED' ? (zh ? '最近失败' : 'Last run failed') : agent.enabled === false ? (zh ? '已暂停' : 'Paused') : (zh ? '待调度' : 'Scheduled');
  return <Tag color={active ? 'processing' : status === 'FAILED' ? 'error' : agent.enabled === false ? 'default' : 'success'}>{text}</Tag>;
}

export default function AgentsPage() {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const { dashboard, loading, error, refresh, revision, marketRefresh } = useDashboardSnapshot();
  const [timeframe, setTimeframe] = useChartTimeframe();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [chartSymbols, setChartSymbols] = useState({});
  const [workspaces, setWorkspaces] = useState({});
  const [requestState, setRequestState] = useState(null);
  const agents = dashboard?.agent_summaries || [];
  const activeId = selectAgentId(agents, params.get('agent'));
  const agent = agents.find((item) => item.config_id === activeId);
  const visibleAgents = filterAgents(agents, search, filter);
  const signature = workspaceSignature(agent ? [agent] : []);
  const chartSymbol = (agent?.symbols || [agent?.symbol]).includes(chartSymbols[activeId]) ? chartSymbols[activeId] : agent?.symbol;
  const requestKey = JSON.stringify([signature, timeframe, chartSymbol, revision, marketRefresh]);
  const workspaceLoading = Boolean(activeId && requestState?.key !== requestKey);
  const workspaceError = requestState?.key === requestKey ? requestState.error : '';

  useEffect(() => {
    if (!activeId) return undefined;
    let active = true;
    const controller = new AbortController();
    api.get(`/public/workspace/${encodeURIComponent(activeId)}`, {
      params: { timeframe, symbol: chartSymbol }, signal: controller.signal, timeout: 30000, silent: true,
    }).then(({ data }) => {
      if (active) {
        setWorkspaces((previous) => ({ ...previous, [activeId]: data }));
        setRequestState({ key: requestKey, error: '' });
      }
    }).catch((err) => {
      if (active) setRequestState({ key: requestKey, error: err.message || 'Workspace unavailable' });
    });
    return () => { active = false; controller.abort(); };
  }, [activeId, timeframe, chartSymbol, requestKey]);

  const selectAgent = (id) => setParams((previous) => { const next = new URLSearchParams(previous); next.set('agent', id); return next; }, { replace: true });
  const workspace = workspaces[activeId];
  const running = agents.filter((item) => ['RUNNING', 'QUEUED'].includes(item.execution?.status)).length;
  const authenticated = Boolean(localStorage.getItem('crypto-agent-token'));
  return <div className="boxed-page dashboard-page dashboard-v2 agents-page">
    <section className="agents-heading">
      <div><div className="dashboard-eyebrow">AGENT WORKSPACE</div><Typography.Title level={2}>{zh ? 'Agent 运行' : 'Agent activity'}</Typography.Title><Typography.Text type="secondary">{zh ? `${agents.length} 个任务 · ${running} 个进行中 · 选择任务查看入选标的与运行结果` : `${agents.length} agents · ${running} active · Select an agent to inspect its markets and execution`}</Typography.Text></div>
      <Space wrap><Button icon={<ReloadOutlined />} loading={loading || workspaceLoading} onClick={refresh}>{zh ? '刷新' : 'Refresh'}</Button><Button icon={<SettingOutlined />} onClick={() => navigate('/console/config')}>{zh ? '管理任务' : 'Manage agents'}</Button></Space>
    </section>
    {error && <Alert type="warning" showIcon title={error} />}
    {loading && !dashboard ? <Card><Skeleton active /></Card> : !agents.length ? <Card><Empty description={zh ? '尚未配置 Agent' : 'No agents configured'}><Button type="primary" onClick={() => navigate('/console/config')}>{zh ? '创建任务' : 'Create an agent'}</Button></Empty></Card> : <div className="agents-layout">
      <aside className="agent-directory" aria-label={zh ? 'Agent 列表' : 'Agent list'}>
        <Input allowClear prefix={<SearchOutlined />} value={search} onChange={(event) => setSearch(event.target.value)} placeholder={zh ? '搜索任务、标的或模型' : 'Search agents, symbols or models'} aria-label={zh ? '搜索 Agent' : 'Search agents'} />
        <Segmented block value={filter} onChange={setFilter} options={[{ value: 'all', label: zh ? '全部' : 'All' }, { value: 'active', label: zh ? '运行中' : 'Active' }, { value: 'enabled', label: zh ? '已启用' : 'Enabled' }]} />
        <div className="agent-directory-list">{visibleAgents.length ? visibleAgents.map((item) => <button type="button" className={`agent-directory-item${item.config_id === activeId ? ' is-selected' : ''}`} key={item.config_id} aria-pressed={item.config_id === activeId} onClick={() => selectAgent(item.config_id)}>
          <span className="agent-directory-title">{item.display_name || item.title || item.config_id}</span><span><AgentStatus agent={item} zh={zh} /><Tag>{item.mode}</Tag></span><span className="agent-directory-symbols">{(item.symbols || [item.symbol]).filter(Boolean).join(' · ')}</span><small>{zh ? '下次 ' : 'Next '}{item.next_run || '—'}</small>
        </button>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '没有匹配的任务' : 'No matching agents'} />}</div>
      </aside>
      <section className="agent-detail" aria-label={zh ? 'Agent 详情' : 'Agent details'}>
        {agent && <>
          <div className="agent-detail-heading"><div><Typography.Title level={3}>{agent.display_name || agent.title || activeId}</Typography.Title><Typography.Text type="secondary">{agent.model || '—'} · {agent.config_id}</Typography.Text></div><Space wrap><AgentStatus agent={agent} zh={zh} /><Tag>{agent.mode}</Tag></Space></div>
          <ScheduleDetails agents={[agent]} activeTab={activeId} locale={locale} />
          {workspaceError && <Alert type="warning" showIcon title={zh ? '持仓或行情未能更新' : 'Unable to update positions or markets'} description={workspaceError} action={<Button onClick={refresh}>{zh ? '重试' : 'Retry'}</Button>} />}
          {workspace ? <WorkspacePanel key={activeId} workspace={{ ...workspace, agent }} timeframe={timeframe} setTimeframe={setTimeframe} authenticated={authenticated} chartLoading={workspaceLoading} chartError={Boolean(workspaceError)} chartSymbol={chartSymbol} setChartSymbol={(symbol) => setChartSymbols((previous) => ({ ...previous, [activeId]: symbol }))} /> : <Card><Skeleton active loading={workspaceLoading}><Empty description={zh ? '行情暂时不可用，请重试。' : 'Market data unavailable. Please retry.'} /></Skeleton></Card>}
        </>}
      </section>
    </div>}
    {location.pathname.startsWith('/console') && <Typography.Text type="secondary">{zh ? '此页面与公开 Agent 页面展示相同的运行数据。' : 'This workspace shares its runtime data with the public Agents page.'}</Typography.Text>}
  </div>;
}
