import { useState } from 'react';
import { Alert, Button, Card, Empty, Input, Segmented, Select, Skeleton, Space, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { WorkspacePanel } from '../components/AgentWorkspace';
import ScheduleDetails from '../components/ScheduleDetails';
import { AgentStatus } from '../components/AgentRuntimeStatus';
import { isAgentActive } from '../lib/agentRuntime';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { useChartTimeframe } from '../hooks/useChartTimeframe';
import { useWorkspaceSections } from '../hooks/useWorkspaceSections';
import { usePreferences } from '../app/usePreferences';
import { filterAgents, selectAgentId, workspaceSignature } from '../lib/dashboard';
import './AgentsPage.css';


function formatDirectorySymbol(symbol) {
  return String(symbol || '').split(':')[0].trim();
}

function modeBadgeLabel(mode, zh) {
  if (mode === 'SPOT_DCA') return zh ? '现货' : 'Spot';
  if (mode === 'REAL') return zh ? '实盘' : 'Live';
  return zh ? '模拟' : 'Sim';
}

export default function AgentsPage({ authenticated = false }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { dashboard, loading, error, refresh, revision, marketRefresh } = useDashboardSnapshot();
  const [timeframe, setTimeframe] = useChartTimeframe();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [chartSymbols, setChartSymbols] = useState({});
  const [memoryEdits, setMemoryEdits] = useState({});
  const agents = dashboard?.agent_summaries || [];
  const activeId = selectAgentId(agents, params.get('agent'));
  const agent = agents.find((item) => item.config_id === activeId);
  const visibleAgents = filterAgents(agents, search, filter);
  const signature = workspaceSignature(agent ? [agent] : []);
  const chartSymbol = (agent?.symbols || [agent?.symbol]).includes(chartSymbols[activeId]) ? chartSymbols[activeId] : agent?.symbol;
  const { workspace: sectionWorkspace, context, account, chart } = useWorkspaceSections(activeId, `${signature}:${revision}`, timeframe, chartSymbol, marketRefresh);
  const workspaceLoading = context.loading || account.loading || chart.loading;

  const selectAgent = (id) => setParams((previous) => { const next = new URLSearchParams(previous); next.set('agent', id); return next; }, { replace: true });
  const currentMemoryEdit = memoryEdits[activeId];
  const workspace = {
    ...sectionWorkspace,
    short_memories:
      currentMemoryEdit && currentMemoryEdit.updatedAt === context.updatedAt
        ? currentMemoryEdit.data
        : sectionWorkspace.short_memories,
  };
  const running = agents.filter(isAgentActive).length;
  const onMemorySaved = (saved) => setMemoryEdits((previous) => {
    const current = workspace.short_memories || {};
    return {
      ...previous,
      [saved.config_id]: {
        updatedAt: context.updatedAt,
        data: {
          ...current,
          short_memories: (current.short_memories || []).map(row =>
            row.bucket_start === saved.bucket_start ? { ...row, market_summary: saved.market_summary } : row
          ),
        },
      },
    };
  });
  return <div className="boxed-page dashboard-page dashboard-v2 agents-page">
    {error && <Alert type="warning" showIcon title={error} action={<Button onClick={refresh} loading={loading}>{zh ? '重试' : 'Retry'}</Button>} />}
    {loading && !dashboard ? <Card><Skeleton active /></Card> : !agents.length ? <Card><Empty description={zh ? '尚未配置 Agent' : 'No agents configured'}><Button type="primary" onClick={() => navigate('/console/config')}>{zh ? '创建任务' : 'Create an agent'}</Button></Empty></Card> : <div className={`agents-layout${agents.length === 1 ? ' is-single' : ''}`}>
      {agents.length > 1 && <Select className="agent-mobile-selector" aria-label={zh ? '选择任务' : 'Select task'} value={activeId} onChange={selectAgent} options={agents.map(item => ({ value: item.config_id, label: item.display_name || item.title || item.config_id }))} />}
      {agents.length > 1 && <aside className="agent-directory" aria-label={zh ? 'Agent 列表' : 'Agent list'}>
        <div className="agent-directory-header">
          <span className="agent-directory-heading">{zh ? '任务列表' : 'Agents'}</span>
          <span className="agent-directory-count">{zh ? `${running}/${agents.length} 运行中` : `${running}/${agents.length} active`}</span>
        </div>
        <Input allowClear prefix={<SearchOutlined />} value={search} onChange={(event) => setSearch(event.target.value)} placeholder={zh ? '搜索任务、标的或模型' : 'Search agents, symbols or models'} aria-label={zh ? '搜索 Agent' : 'Search agents'} />
        <Segmented block value={filter} onChange={setFilter} options={[{ value: 'all', label: zh ? '全部' : 'All' }, { value: 'active', label: zh ? '运行中' : 'Active' }, { value: 'enabled', label: zh ? '已启用' : 'Enabled' }]} />
        <div className="agent-directory-list">{visibleAgents.length ? visibleAgents.map((item) => {
          const modeKey = String(item.mode || 'STRATEGY').toLowerCase();
          const symbolList = (item.symbols || [item.symbol]).map(formatDirectorySymbol).filter(Boolean);
          const visibleSymbols = symbolList.slice(0, 3);
          const extraCount = symbolList.length - visibleSymbols.length;
          return (
            <button
              type="button"
              className={`agent-directory-item${item.config_id === activeId ? ' is-selected' : ''}${item.enabled === false ? ' is-paused' : ''}`}
              key={item.config_id}
              aria-pressed={item.config_id === activeId}
              onClick={() => selectAgent(item.config_id)}
            >
              <div className="agent-directory-item-top">
                <span className="agent-directory-title">{item.display_name || item.title || item.config_id}</span>
                <AgentStatus agent={item} zh={zh} />
              </div>
              <div className="agent-directory-meta">
                <span className={`agent-directory-mode is-${modeKey}`}>{modeBadgeLabel(item.mode, zh)}</span>
                {item.model ? <span className="agent-directory-model" title={item.model}>{item.model}</span> : null}
              </div>
              {symbolList.length > 0 && (
                <div className="agent-directory-symbols" title={symbolList.join(' · ')}>
                  {visibleSymbols.map((sym) => <span key={sym} className="agent-directory-symbol-chip">{sym}</span>)}
                  {extraCount > 0 && <span className="agent-directory-symbol-chip is-more">+{extraCount}</span>}
                </div>
              )}
            </button>
          );
        }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '没有匹配的任务' : 'No matching agents'} />}</div>
      </aside>}
      <section className="agent-detail" aria-label={zh ? 'Agent 详情' : 'Agent details'}>
        {agent && <>
          <header className="agent-overview-header">
            <div className="agent-detail-heading">
              <div className="agent-detail-identity">
                <div className="workspace-eyebrow">{zh ? '任务工作台' : 'AGENT WORKSPACE'}</div>
                <div className="agent-title-row"><Typography.Title level={2}>{agent.display_name || agent.title || activeId}</Typography.Title><AgentStatus agent={agent} zh={zh} /></div>
                <div className="agent-detail-meta"><span>{agent.model || '—'}</span><span>{({ REAL: zh ? '合约实盘' : 'Live futures', SPOT_DCA: zh ? '现货' : 'Spot', STRATEGY: zh ? '策略分析' : 'Analysis' })[agent.mode] || agent.mode}</span>{agent.timestamp && <span>{zh ? '最近运行 ' : 'Last run '}{agent.timestamp}</span>}</div>
              </div>
              <Space className="agent-heading-actions"><Button icon={<ReloadOutlined />} loading={loading || workspaceLoading} onClick={refresh}>{zh ? '刷新' : 'Refresh'}</Button><Button icon={<SettingOutlined />} onClick={() => navigate('/console/config')}>{zh ? '管理任务' : 'Manage agents'}</Button></Space>
            </div>
            <ScheduleDetails agents={[agent]} activeTab={activeId} locale={locale} compact />
          </header>
          {context.error && <Alert type="warning" showIcon title={zh ? '决策与记忆未能更新' : 'Unable to update decisions and memory'} description={context.error} action={<Button onClick={refresh}>{zh ? '重试' : 'Retry'}</Button>} />}
          <WorkspacePanel key={activeId} compact workspace={{ ...workspace, agent: { ...agent, ...workspace.agent, execution: agent.execution, schedule: agent.schedule } }} timeframe={timeframe} setTimeframe={setTimeframe} authenticated={authenticated} onMemorySaved={onMemorySaved} chartLoading={chart.loading} chartError={Boolean(chart.error)} chartSymbol={chartSymbol} setChartSymbol={(symbol) => setChartSymbols((previous) => ({ ...previous, [activeId]: symbol }))} />
        </>}
      </section>
    </div>}
  </div>;
}
