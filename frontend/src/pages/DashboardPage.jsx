import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Empty, Skeleton, Space, Tag, Typography } from 'antd';
import {
  ArrowRightOutlined,
  ClockCircleOutlined,
  DollarOutlined,
  FundOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import EquityCompareChart from '../components/EquityCompareChart';
import SharedNewsPanel from '../components/SharedNewsPanel';
import PolymarketPanel from '../components/PolymarketPanel';
import { AgentStatus } from '../components/AgentRuntimeStatus';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { usePreferences } from '../app/usePreferences';
import { api } from '../lib/api';
import './DashboardPage.css';

function formatSymbol(symbol) {
  return String(symbol || '').split(':')[0].trim();
}

function modeLabel(mode, zh) {
  if (mode === 'SPOT_DCA') return zh ? '现货定投' : 'Spot DCA';
  if (mode === 'REAL') return zh ? '合约实盘' : 'Live Futures';
  if (mode === 'STRATEGY') return zh ? '策略模拟' : 'Strategy Sim';
  return mode || '—';
}

function modeTone(mode) {
  if (mode === 'REAL') return 'is-real';
  if (mode === 'SPOT_DCA') return 'is-spot';
  return 'is-strategy';
}

function formatMoney(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function extractLatestEquity(item) {
  if (!item) return null;
  if (Number.isFinite(Number(item.latest_equity))) return Number(item.latest_equity);
  const points = Array.isArray(item.points) ? item.points : [];
  for (let i = points.length - 1; i >= 0; i -= 1) {
    const val = Number(points[i]?.equity);
    if (Number.isFinite(val)) return val;
  }
  return null;
}

function extractReturnPct(item) {
  if (!item) return null;
  if (Number.isFinite(Number(item.return_pct))) return Number(item.return_pct);
  const points = Array.isArray(item.points) ? item.points : [];
  for (let i = points.length - 1; i >= 0; i -= 1) {
    const val = Number(points[i]?.holding_return_pct ?? points[i]?.return_pct);
    if (Number.isFinite(val)) return val;
  }
  return null;
}

export default function DashboardPage() {
  const { locale, t } = usePreferences();
  const zh = locale === 'zh';
  const navigate = useNavigate();
  const { dashboard, loading, error, refresh, revision, marketRefresh } = useDashboardSnapshot();
  const [series, setSeries] = useState([]);
  const [compareIds, setCompareIds] = useState([]);
  const [compareLoading, setCompareLoading] = useState(true);
  const [compareError, setCompareError] = useState('');

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    async function load() {
      setCompareLoading(true);
      try {
        const { data } = await api.get('/public/compare', { signal: controller.signal, timeout: 25000 });
        if (active) {
          setSeries(data.series || []);
          setCompareError('');
        }
      } catch (err) {
        if (active) setCompareError(err.message);
      } finally {
        if (active) setCompareLoading(false);
      }
    }
    load();
    return () => {
      active = false;
      controller.abort();
    };
  }, [revision, marketRefresh]);

  const metrics = dashboard?.overview_metrics || {};
  const runs = metrics.runs_24h || {};
  const today = dashboard?.usage_today || {};
  const agents = dashboard?.agent_summaries || [];

  const seriesByConfig = useMemo(() => {
    const map = new Map();
    (series || []).forEach((item) => {
      if (item?.config_id) map.set(item.config_id, item);
    });
    return map;
  }, [series]);

  const equitySummary = useMemo(() => {
    const readySeries = (series || []).filter((item) => item?.data_state === 'ready' || extractLatestEquity(item) !== null);
    let totalEquity = 0;
    let equityCount = 0;
    let returnSum = 0;
    let returnCount = 0;
    readySeries.forEach((item) => {
      const eq = extractLatestEquity(item);
      if (eq !== null) {
        totalEquity += eq;
        equityCount += 1;
      }
      const ret = extractReturnPct(item);
      if (ret !== null) {
        returnSum += ret;
        returnCount += 1;
      }
    });
    return {
      totalEquity: equityCount > 0 ? totalEquity : null,
      equityCount,
      avgReturnPct: returnCount > 0 ? returnSum / returnCount : null,
    };
  }, [series]);

  const todayCostText = Object.entries(today.costs_by_currency || {})
    .map(([currency, cost]) => `${currency} ${Number(cost).toFixed(4)}`)
    .join(' · ') || '—';

  const kpiCards = [
    {
      key: 'equity',
      icon: <FundOutlined />,
      label: zh ? '追踪总权益' : 'Tracked Equity',
      value: equitySummary.totalEquity !== null ? `${formatMoney(equitySummary.totalEquity)} USDT` : '—',
      tone: equitySummary.avgReturnPct > 0 ? 'is-positive' : equitySummary.avgReturnPct < 0 ? 'is-negative' : '',
      note: equitySummary.avgReturnPct !== null
        ? (zh
          ? `${equitySummary.equityCount} 条曲线 · 均值 ${equitySummary.avgReturnPct >= 0 ? '+' : ''}${equitySummary.avgReturnPct.toFixed(2)}%`
          : `${equitySummary.equityCount} series · avg ${equitySummary.avgReturnPct >= 0 ? '+' : ''}${equitySummary.avgReturnPct.toFixed(2)}%`)
        : (zh ? '按已同步的权益曲线汇总' : 'Aggregated from synced equity curves'),
    },
    {
      key: 'agents',
      icon: <RobotOutlined />,
      label: zh ? '活跃任务' : 'Active Agents',
      value: `${metrics.enabled_count ?? agents.filter((a) => a.enabled !== false).length} / ${metrics.agent_count ?? agents.length}`,
      tone: '',
      note: zh ? '点击下方卡片进入对应交易工作台' : 'Click any agent card to open its workbench',
    },
    {
      key: 'runs',
      icon: <ThunderboltOutlined />,
      label: zh ? '近 24h 完成' : '24h Completed',
      value: `${runs.finished ?? 0}`,
      tone: (runs.failed || 0) > 0 ? 'is-warning' : '',
      note: zh
        ? `${runs.running || 0} 运行中 · ${runs.failed || 0} 失败`
        : `${runs.running || 0} running · ${runs.failed || 0} failed`,
    },
    {
      key: 'cost',
      icon: <DollarOutlined />,
      label: zh ? '今日已知费用' : 'Known Cost Today',
      value: todayCostText,
      tone: '',
      note: zh
        ? `${today.unpriced_calls || 0} 次未定价 · 消息 ${dashboard?.news_snapshot?.timestamp ? dayjs(dashboard.news_snapshot.timestamp).format('HH:mm') : '—'}`
        : `${today.unpriced_calls || 0} unpriced · news ${dashboard?.news_snapshot?.timestamp ? dayjs(dashboard.news_snapshot.timestamp).format('HH:mm') : '—'}`,
    },
  ];

  return (
    <div className="boxed-page dashboard-page dashboard-v2 overview-page">
      <section className="dashboard-command-center">
        <div className="dashboard-command-top">
          <div className="dashboard-command-identity">
            <div className="dashboard-eyebrow">PORTFOLIO OVERVIEW</div>
            <div className="dashboard-title-row">
              <Typography.Title level={2}>{zh ? '数据总览' : 'Overview'}</Typography.Title>
              <Tag color={dashboard?.scheduler_enabled ? 'green' : 'default'}>
                {zh
                  ? (dashboard?.scheduler_enabled ? '自动调度运行中' : '自动调度已暂停')
                  : (dashboard?.scheduler_enabled ? 'Scheduler active' : 'Scheduler paused')}
              </Tag>
            </div>
            <Typography.Text type="secondary" className="dashboard-command-subtitle">
              {zh ? '一屏掌握组合权益、任务状态与市场消息面' : 'Track portfolio equity, live agent status, and market intelligence at a glance'}
            </Typography.Text>
          </div>
          <div className="dashboard-command-actions">
            <Space wrap>
              <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
                {zh ? '刷新' : 'Refresh'}
              </Button>
              <Button type="primary" icon={<ArrowRightOutlined />} onClick={() => navigate('/agents')}>
                {zh ? '进入交易工作台' : 'Open workbench'}
              </Button>
            </Space>
            <span className="dashboard-updated-pill">
              <ClockCircleOutlined />
              {zh ? '更新于 ' : 'Updated '}
              {dashboard?.generated_at ? dayjs(dashboard.generated_at).format('HH:mm:ss') : '—'}
              {dashboard?.timezone ? ` · ${dashboard.timezone}` : ''}
            </span>
          </div>
        </div>

        <div className="overview-metrics">
          {kpiCards.map((item) => (
            <div className={`overview-metric ${item.tone}`} key={item.key}>
              <div className="overview-metric-head">
                <span className="overview-metric-icon">{item.icon}</span>
                <span>{item.label}</span>
              </div>
              <strong>{item.value}</strong>
              <small>{item.note}</small>
            </div>
          ))}
        </div>
      </section>

      {error && (
        <Alert
          type={dashboard ? 'warning' : 'error'}
          showIcon
          title={error}
          description={dashboard ? (zh ? '保留上次成功的数据。' : 'Showing the last successful snapshot.') : undefined}
        />
      )}

      <Card
        className="panel-card dashboard-equity-card"
        title={
          <div className="dashboard-card-title-row">
            <span>{t('equityCompare')}</span>
            {series.length > 0 && (
              <Tag bordered={false} color="blue">
                {zh ? `${series.length} 条任务曲线` : `${series.length} curves`}
              </Tag>
            )}
          </div>
        }
        loading={compareLoading && !series.length}
      >
        {compareError && <Alert type="warning" showIcon title={compareError} style={{ marginBottom: 12 }} />}
        <EquityCompareChart series={series} selectedIds={compareIds} onSelectedIdsChange={setCompareIds} />
      </Card>

      <Card
        className="panel-card dashboard-agents-card"
        title={
          <div className="dashboard-card-title-row">
            <span>{zh ? '任务状态一览' : 'Agent Status'}</span>
            <Tag bordered={false}>{zh ? `共 ${agents.length} 个任务` : `${agents.length} total`}</Tag>
          </div>
        }
        extra={
          <Button type="link" onClick={() => navigate('/agents')}>
            {zh ? '全部工作台' : 'All workbenches'} <ArrowRightOutlined />
          </Button>
        }
      >
        <Skeleton active loading={loading && !dashboard} paragraph={{ rows: 3 }}>
          {agents.length ? (
            <div className="dashboard-agent-grid">
              {agents.map((agent) => {
                const symbols = (agent.symbols || [agent.symbol]).map(formatSymbol).filter(Boolean);
                const visibleSymbols = symbols.slice(0, 3);
                const extraSymbols = symbols.length - visibleSymbols.length;
                const matchedSeries = seriesByConfig.get(agent.config_id);
                const latestEquity = extractLatestEquity(matchedSeries);
                const returnPct = extractReturnPct(matchedSeries);
                const paused = agent.enabled === false;
                return (
                  <button
                    type="button"
                    key={agent.config_id}
                    className={`dashboard-agent-card${paused ? ' is-paused' : ''}`}
                    onClick={() => navigate(`/agents?agent=${encodeURIComponent(agent.config_id)}`)}
                  >
                    <div className="dashboard-agent-card-top">
                      <div className="dashboard-agent-name-wrap">
                        <strong className="dashboard-agent-name">
                          {agent.display_name || agent.title || agent.config_id}
                        </strong>
                        <div className="dashboard-agent-badges">
                          <span className={`dashboard-mode-pill ${modeTone(agent.mode)}`}>
                            {modeLabel(agent.mode, zh)}
                          </span>
                          {agent.model ? (
                            <span className="dashboard-model-pill" title={agent.model}>
                              {agent.model}
                            </span>
                          ) : null}
                        </div>
                      </div>
                      <AgentStatus agent={agent} zh={zh} />
                    </div>

                    <div className="dashboard-agent-symbols" title={symbols.join(' · ')}>
                      {visibleSymbols.length ? (
                        <>
                          {visibleSymbols.map((sym) => (
                            <span key={sym} className="dashboard-symbol-chip">{sym}</span>
                          ))}
                          {extraSymbols > 0 && (
                            <span className="dashboard-symbol-chip is-more">+{extraSymbols}</span>
                          )}
                        </>
                      ) : (
                        <span className="dashboard-symbol-chip is-more">—</span>
                      )}
                    </div>

                    <div className="dashboard-agent-kpis">
                      <div>
                        <span>{zh ? '最新权益' : 'Equity'}</span>
                        <strong>{latestEquity !== null ? formatMoney(latestEquity) : '—'}</strong>
                      </div>
                      <div>
                        <span>{zh ? '区间收益' : 'Return'}</span>
                        <strong className={returnPct > 0 ? 'is-positive' : returnPct < 0 ? 'is-negative' : ''}>
                          {returnPct !== null ? `${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%` : '—'}
                        </strong>
                      </div>
                      <div>
                        <span>{zh ? '订单记录' : 'Orders'}</span>
                        <strong>{agent.order_total ?? 0}</strong>
                      </div>
                    </div>

                    <div className="dashboard-agent-footer">
                      <span className="dashboard-agent-time">
                        {zh ? '最近分析 ' : 'Last run '}
                        {agent.timestamp || '—'}
                      </span>
                      <span className="dashboard-agent-cta">
                        {zh ? '进入工作台' : 'Open'} <ArrowRightOutlined />
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <Empty description={zh ? '尚未配置交易任务' : 'No agents configured yet'}>
              <Button type="primary" onClick={() => navigate('/console/config')}>
                {zh ? '前往创建任务' : 'Create an agent'}
              </Button>
            </Empty>
          )}
        </Skeleton>
      </Card>

      <SharedNewsPanel snapshot={dashboard?.news_snapshot} />

      <details className="content-disclosure dashboard-prediction-disclosure">
        <summary>{zh ? '预测市场观察' : 'Prediction market watch'}</summary>
        <PolymarketPanel />
      </details>
    </div>
  );
}
