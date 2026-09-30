import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Empty, Grid, Pagination, Select, Space, Spin, Statistic, Tag, Typography } from 'antd';
import EquityCompareChart from '../components/EquityCompareChart';
import MarkdownBlock from '../components/MarkdownBlock';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Title, Paragraph, Text } = Typography;
const { useBreakpoint } = Grid;

function HistoryReview({ record, defaultOpen = false, locale }) {
  const preview = String(record.content || '').replace(/[#*>`]/g, '').replace(/\s+/g, ' ').trim();
  return (
    <details className="history-review-item" open={defaultOpen}>
      <summary className="history-review-summary">
        <span className="history-review-meta">
          <strong>{record.timestamp || '-'}</strong>
          <span className="history-review-tags">
            <Tag>{record.config_id || 'ALL'}</Tag>
            {record.agent_name ? <Tag color="blue">{record.agent_name}</Tag> : null}
            {record.timeframe ? <Tag>{record.timeframe}</Tag> : null}
          </span>
        </span>
        <span className="history-review-preview">{preview.slice(0, 180)}{preview.length > 180 ? '…' : ''}</span>
        <span className="history-review-toggle">
          <span className="when-collapsed">{locale === 'zh' ? '查看复盘' : 'Read review'}</span>
          <span className="when-expanded">{locale === 'zh' ? '收起复盘' : 'Collapse review'}</span>
        </span>
      </summary>
      <div className="history-review-content"><MarkdownBlock content={record.content || ''} /></div>
    </details>
  );
}

export default function HistoryPage() {
  const { t, locale, selectedSymbol, setSelectedSymbol } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [configId, setConfigId] = useState('ALL');
  const [compareIds, setCompareIds] = useState([]);
  const [pagination, setPagination] = useState({ symbol: selectedSymbol, page: 1 });
  const page = pagination.symbol === selectedSymbol ? pagination.page : 1;
  const setPage = (value) => setPagination({ symbol: selectedSymbol, page: value });
  const changeCompareIds = (value) => {
    setPage(1);
    setCompareIds(value);
  };

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const requestedSymbol = selectedSymbol;
        const bootstrap = await api.get('/public/dashboard', { params: requestedSymbol ? { symbol: requestedSymbol } : {} });
        const nextSymbol = requestedSymbol || bootstrap.data.current_symbol;
        const response = await api.get('/public/history', {
          params: {
            symbol: nextSymbol,
            config_id: configId,
            page,
            compare_ids: compareIds.join(','),
          },
        });
        if (!mounted) {
          return;
        }
        if (nextSymbol && nextSymbol !== selectedSymbol) {
          setSelectedSymbol(nextSymbol);
        }
        setPayload({
          symbols: bootstrap.data.symbols,
          history: response.data,
        });
        setCompareIds((prev) => {
          const allowed = new Set(response.data.active_agents || []);
          const filtered = prev.filter((item) => allowed.has(item));
          return filtered.length === prev.length ? prev : filtered;
        });
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load history');
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      mounted = false;
    };
  }, [selectedSymbol, configId, compareIds, page, setSelectedSymbol]);

  const compareSeries = useMemo(() => payload?.history?.history_compare_series || [], [payload]);

  const summaries = useMemo(() => payload?.history?.summaries || [], [payload]);

  // 横向对比：按 config_id 分组，PC端多列展示
  const groupedSummaries = useMemo(() => {
    if (compareIds.length < 2 || isMobile) return null;
    const groups = {};
    compareIds.forEach((id) => { groups[id] = []; });
    summaries.forEach((record) => {
      const key = record.config_id;
      if (groups[key]) groups[key].push(record);
    });
    return groups;
  }, [compareIds, summaries, isMobile]);

  return (
    <div className="boxed-page history-page">
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Card className="admin-hero">
          <Space className="admin-hero__inner history-toolbar" align="start" wrap>
            <div>
              <Title level={2} style={{ margin: 0 }}>
                {t('history')}
              </Title>
              <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                {t('historyPageDesc')}
              </Paragraph>
            </div>
            <div className="history-filter-grid">
              <label className="form-field">
                <span>{locale === 'zh' ? '复盘任务' : 'Review task'}</span>
              <Select
                aria-label={locale === 'zh' ? '复盘任务' : 'Review task'}
                value={configId}
                options={[
                  { label: locale === 'zh' ? '全部任务' : 'All tasks', value: 'ALL' },
                  ...((payload?.history?.active_agents || []).map((item) => ({ label: item, value: item })) || []),
                ]}
                onChange={(value) => {
                  setPage(1);
                  setConfigId(value);
                }}
              />
              </label>
              <label className="form-field">
                <span>{locale === 'zh' ? '对比任务' : 'Compare tasks'}</span>
              <Select
                mode="multiple"
                aria-label={locale === 'zh' ? '对比任务' : 'Compare tasks'}
                maxTagCount="responsive"
                allowClear
                value={compareIds}
                options={((payload?.history?.compare_candidates || []).map((item) => ({
                  label: `${item.label || item.config_id} · ${item.mode || '-'}`,
                  value: item.config_id,
                })) || [])}
                onChange={changeCompareIds}
                placeholder={t('compare')}
              />
              </label>
            </div>
          </Space>
        </Card>

        {error ? <Alert type="error" message={error} showIcon /> : null}

        {loading ? (
          <Card className="panel-card loading-card">
            <Spin />
          </Card>
        ) : null}

        {!loading && payload?.history ? (
          <>
            <div className="metric-grid">
              <Card className="panel-card metric-card">
                <Statistic title={t('totalTrades')} value={payload.history.pnl_stats?.total_trades || 0} />
              </Card>
              <Card className="panel-card metric-card">
                <Statistic title={t('totalPnl')} value={payload.history.pnl_stats?.total_pnl || 0} precision={2} />
              </Card>
              <Card className="panel-card metric-card">
                <Statistic title={t('winRate')} suffix="%" value={payload.history.pnl_stats?.win_rate || 0} precision={2} />
              </Card>
              <Card className="panel-card metric-card">
                <Statistic title={t('symbolMode')} value={payload.history.agent_mode || '-'} />
              </Card>
            </div>

            <Card className="panel-card" title={t('equityCompare')}>
              <EquityCompareChart series={compareSeries} selectedIds={compareIds} onSelectedIdsChange={changeCompareIds} />
            </Card>

            <Card className="panel-card history-review-card" title={t('historyReview')}>
              {summaries.length ? (
                groupedSummaries ? (
                  // PC 端横向对比布局
                  <div
                    className="history-compare-grid"
                    style={{ gridTemplateColumns: `repeat(${Math.min(compareIds.length, 3)}, 1fr)` }}
                  >
                    {compareIds.map((cid) => (
                      <div key={cid} className="history-compare-col">
                        <div className="history-compare-col-header">
                          <Tag color="blue">{cid}</Tag>
                        </div>
                        {(groupedSummaries[cid] || []).length ? (
                          (groupedSummaries[cid] || []).map((record, index) => (
                            <HistoryReview key={record.id} record={record} defaultOpen={index === 0} locale={locale} />
                          ))
                        ) : (
                          <Empty description={t('noData')} />
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  // 默认竖向列表（移动端或单选）
                  <div className="history-review-list">
                    {summaries.map((record, index) => (
                      <HistoryReview key={record.id} record={record} defaultOpen={index === 0} locale={locale} />
                    ))}
                  </div>
                )
              ) : (
                <Empty description={t('emptyHistory')} />
              )}
              <div className="history-pagination">
                <Pagination
                  current={payload.history.current_page}
                  total={payload.history.total_count}
                  pageSize={20}
                  onChange={setPage}
                  showSizeChanger={false}
                  simple={isMobile}
                  hideOnSinglePage={false}
                />
              </div>
            </Card>
          </>
        ) : null}
      </Space>
    </div>
  );
}
