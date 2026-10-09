import { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Segmented, Skeleton, Space, Tag, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
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
    {error && <Alert type="warning" showIcon title={error} action={<Button onClick={refresh} loading={loading}>{zh ? '重试' : 'Retry'}</Button>} />}
    {loading && !dashboard ? <Card><Skeleton active /></Card> : !agents.length ? <Card><Empty description={zh ? '尚未配置 Agent' : 'No agents configured'}><Button type="primary" onClick={() => navigate('/console/config')}>{zh ? '创建任务' : 'Create an agent'}</Button></Empty></Card> : <div className={`agents-layout${agents.length === 1 ? ' is-single' : ''}`}>
      {agents.length > 1 && <aside className="agent-directory" aria-label={zh ? 'Agent 列表' : 'Agent list'}>
        <Typography.Text type="secondary">{zh ? `${agents.length} 个任务 · ${running} 个进行中` : `${agents.length} agents · ${running} active`}</Typography.Text>
        <Input allowClear prefix={<SearchOutlined />} value={search} onChange={(event) => setSearch(event.target.value)} placeholder={zh ? '搜索任务、标的或模型' : 'Search agents, symbols or models'} aria-label={zh ? '搜索 Agent' : 'Search agents'} />
        <Segmented block value={filter} onChange={setFilter} options={[{ value: 'all', label: zh ? '全部' : 'All' }, { value: 'active', label: zh ? '运行中' : 'Active' }, { value: 'enabled', label: zh ? '已启用' : 'Enabled' }]} />
        <div className="agent-directory-list">{visibleAgents.length ? visibleAgents.map((item) => <button type="button" className={`agent-directory-item${item.config_id === activeId ? ' is-selected' : ''}`} key={item.config_id} aria-pressed={item.config_id === activeId} onClick={() => selectAgent(item.config_id)}>
          <span className="agent-directory-title">{item.display_name || item.title || item.config_id}</span><span><AgentStatus agent={item} zh={zh} /></span><span className="agent-directory-symbols">{(item.symbols || [item.symbol]).filter(Boolean).join(' · ')}</span>
        </button>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '没有匹配的任务' : 'No matching agents'} />}</div>
      </aside>}
      <section className="agent-detail" aria-label={zh ? 'Agent 详情' : 'Agent details'}>
        {agent && <>
          <header className="agent-overview-header">
            <div className="agent-detail-heading">
              <div className="agent-detail-identity">
                <div className="agent-title-row"><Typography.Title level={2}>{agent.display_name || agent.title || activeId}</Typography.Title><AgentStatus agent={agent} zh={zh} /></div>
                <div className="agent-detail-meta"><span>{agent.model || '—'}</span><span>{({ REAL: zh ? '合约实盘' : 'Live futures', SPOT_DCA: zh ? '现货' : 'Spot', STRATEGY: zh ? '策略分析' : 'Analysis' })[agent.mode] || agent.mode}</span>{agent.timestamp && <span>{zh ? '最近运行 ' : 'Last run '}{agent.timestamp}</span>}</div>
              </div>
              <Space className="agent-heading-actions"><Button icon={<ReloadOutlined />} loading={loading || workspaceLoading} onClick={refresh}>{zh ? '刷新' : 'Refresh'}</Button><Button icon={<SettingOutlined />} onClick={() => navigate('/console/config')}>{zh ? '管理任务' : 'Manage agents'}</Button></Space>
            </div>
            <ScheduleDetails agents={[agent]} activeTab={activeId} locale={locale} compact />
          </header>
          {workspaceError && <Alert type="warning" showIcon title={zh ? '持仓或行情未能更新' : 'Unable to update positions or markets'} description={workspaceError} action={<Button onClick={refresh}>{zh ? '重试' : 'Retry'}</Button>} />}
          {workspace ? <WorkspacePanel key={activeId} compact workspace={{ ...workspace, agent }} timeframe={timeframe} setTimeframe={setTimeframe} authenticated={authenticated} chartLoading={workspaceLoading} chartError={Boolean(workspaceError)} chartSymbol={chartSymbol} setChartSymbol={(symbol) => setChartSymbols((previous) => ({ ...previous, [activeId]: symbol }))} /> : <Card><Skeleton active loading={workspaceLoading}><Empty description={zh ? '行情暂时不可用，请重试。' : 'Market data unavailable. Please retry.'} /></Skeleton></Card>}
        </>}
      </section>
    </div>}
  </div>;
}
