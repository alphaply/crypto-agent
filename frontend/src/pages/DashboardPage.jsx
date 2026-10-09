import { useEffect, useState } from 'react';
import { Alert, Button, Card, Skeleton, Space, Table, Tag, Typography } from 'antd';
import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import EquityCompareChart from '../components/EquityCompareChart';
import SharedNewsPanel from '../components/SharedNewsPanel';
import PolymarketPanel from '../components/PolymarketPanel';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { usePreferences } from '../app/usePreferences';
import { api } from '../lib/api';
import './DashboardPage.css';

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
        if (active) { setSeries(data.series || []); setCompareError(''); }
      } catch (err) {
        if (active) setCompareError(err.message);
      } finally {
        if (active) setCompareLoading(false);
      }
    }
    load();
    return () => { active = false; controller.abort(); };
  }, [revision, marketRefresh]);
  const metrics = dashboard?.overview_metrics || {};
  const runs = metrics.runs_24h || {};
  const today = dashboard?.usage_today || {};
  const facts = [
    [zh ? '启用 Agent' : 'Enabled agents', `${metrics.enabled_count ?? 0} / ${metrics.agent_count ?? 0}`, zh ? '查看任务运行、行情和执行详情' : 'Inspect execution and markets'],
    [zh ? '近 24 小时完成' : 'Completed in 24h', runs.finished ?? 0, zh ? `${runs.running || 0} 进行中 · ${runs.failed || 0} 失败` : `${runs.running || 0} running · ${runs.failed || 0} failed`],
    [zh ? '今日已知模型费用' : 'Known model cost today', Object.entries(today.costs_by_currency || {}).map(([currency, cost]) => `${currency} ${Number(cost).toFixed(4)}`).join(' · ') || '—', zh ? `${today.unpriced_calls || 0} 次调用未定价` : `${today.unpriced_calls || 0} unpriced calls`],
    [zh ? '共享消息更新' : 'Intelligence updated', dashboard?.news_snapshot?.timestamp ? dayjs(dashboard.news_snapshot.timestamp).format('HH:mm') : '—', dashboard?.timezone || '—'],
  ];
  return <div className="boxed-page dashboard-page dashboard-v2 overview-page">
    <section className="dashboard-command-center">
      <div className="dashboard-command-top">
        <div><div className="dashboard-eyebrow">PORTFOLIO OVERVIEW</div><Typography.Title level={2}>{zh ? '数据总览' : 'Overview'}</Typography.Title><Space wrap><Tag color={dashboard?.scheduler_enabled ? 'green' : 'default'}>{zh ? (dashboard?.scheduler_enabled ? '自动调度已启用' : '自动调度已暂停') : (dashboard?.scheduler_enabled ? 'Scheduler enabled' : 'Scheduler paused')}</Tag><Typography.Text type="secondary">{zh ? '统计、权益与市场消息' : 'Performance, equity and market intelligence'}</Typography.Text></Space></div>
        <Space wrap><Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>{zh ? '刷新' : 'Refresh'}</Button><Button type="primary" icon={<ArrowRightOutlined />} onClick={() => navigate('/agents')}>{zh ? '查看 Agent 运行' : 'Explore agents'}</Button></Space>
      </div>
      <div className="overview-metrics">{facts.map(([label, value, note]) => <div className="overview-metric" key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></div>)}</div>
      <Typography.Text type="secondary">{zh ? '数据更新于 ' : 'Updated '}{dashboard?.generated_at ? dayjs(dashboard.generated_at).format('HH:mm:ss') : '—'}</Typography.Text>
    </section>
    {error && <Alert type={dashboard ? 'warning' : 'error'} showIcon title={error} description={dashboard ? (zh ? '保留上次成功的数据。' : 'Showing the last successful snapshot.') : undefined} />}
    <Card className="panel-card" title={t('equityCompare')} loading={compareLoading && !series.length}>
      {compareError && <Alert type="warning" showIcon title={compareError} />}
      <EquityCompareChart series={series} selectedIds={compareIds} onSelectedIdsChange={setCompareIds} />
    </Card>
    <Card className="panel-card" title={zh ? '任务数据' : 'Agent data'} extra={<Button type="link" onClick={() => navigate('/agents')}>{zh ? '运行工作台' : 'Agent workspace'} <ArrowRightOutlined /></Button>}>
      <Skeleton active loading={loading && !dashboard}>
        <Table size="small" rowKey="config_id" dataSource={dashboard?.agent_summaries || []} pagination={{ pageSize: 8, hideOnSinglePage: true }} scroll={{ x: 760 }} columns={[
          { title: 'Agent', key: 'name', render: (_, agent) => <Button type="link" className="overview-agent-link" onClick={() => navigate(`/agents?agent=${encodeURIComponent(agent.config_id)}`)}>{agent.display_name || agent.title || agent.config_id}</Button> },
          { title: zh ? '模式' : 'Mode', dataIndex: 'mode', render: (mode) => <Tag>{mode}</Tag> },
          { title: zh ? '标的' : 'Markets', key: 'symbols', render: (_, agent) => (agent.symbols || [agent.symbol]).filter(Boolean).join(' · ') || '—' },
          { title: zh ? '订单记录' : 'Order records', dataIndex: 'order_total', render: (value) => value ?? 0 },
          { title: zh ? '最近分析' : 'Last analysis', dataIndex: 'timestamp', render: (value) => value || '—' },
          { title: zh ? '模型' : 'Model', dataIndex: 'model', render: (value) => value || '—' },
        ]} />
      </Skeleton>
    </Card>
    <SharedNewsPanel snapshot={dashboard?.news_snapshot} />
    <details className="content-disclosure"><summary>{zh ? '预测市场观察' : 'Prediction market watch'}</summary><PolymarketPanel /></details>
  </div>;
}
