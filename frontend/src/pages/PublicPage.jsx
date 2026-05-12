import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Empty, Space, Spin, Statistic, Table, Typography } from 'antd';
import LineChart from '../components/LineChart';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Title, Paragraph } = Typography;

export default function PublicPage() {
  const { t } = usePreferences();
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const response = await api.get('/public/usage');
        if (mounted) {
          setPayload(response.data);
        }
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load usage');
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
  }, []);

  const dailyTokensSeries = useMemo(
    () => [
      {
        name: t('dailyTokens'),
        data: [...(payload?.daily || [])]
          .reverse()
          .map((item) => ({ name: item.day, value: item.total })),
      },
    ],
    [payload, t],
  );

  const dailyCostSeries = useMemo(
    () => [
      {
        name: t('dailyCost'),
        data: [...(payload?.daily || [])]
          .reverse()
          .map((item) => ({ name: item.day, value: item.cost })),
      },
    ],
    [payload, t],
  );

  const summary = payload?.summary || {};

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="hero-card">
        <Title level={2} style={{ margin: 0 }}>
          {t('usageHeadline')}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          {t('usageSubhead')}
        </Paragraph>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : null}

      {!loading && payload ? (
        <>
          <div className="metric-grid">
            <Card className="panel-card metric-card">
              <Statistic title={t('totalTokens')} value={summary.total_tokens_14d || 0} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('totalCost')} value={summary.total_cost || 0} precision={4} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('trackedModels')} value={summary.tracked_models || 0} />
            </Card>
            <Card className="panel-card metric-card">
              <Statistic title={t('trackedAgents')} value={summary.tracked_agents || 0} />
            </Card>
          </div>

          <div className="split-grid">
            <Card className="panel-card" title={t('dailyTokens')}>
              <div className="chart-wrap">
                {payload.daily?.length ? <LineChart series={dailyTokensSeries} yName="Tokens" /> : <Empty description={t('noData')} />}
              </div>
            </Card>
            <Card className="panel-card" title={t('dailyCost')}>
              <div className="chart-wrap">
                {payload.daily?.length ? <LineChart series={dailyCostSeries} yName="USD" area /> : <Empty description={t('noData')} />}
              </div>
            </Card>
          </div>

          <Card className="panel-card" title={t('modelUsage')}>
            <Table
              rowKey={(row) => row.model}
              dataSource={payload.models || []}
              pagination={false}
              scroll={{ x: 900 }}
              columns={[
                { title: t('modelColumn'), dataIndex: 'model' },
                { title: t('promptColumn'), dataIndex: 'prompt' },
                { title: t('completionColumn'), dataIndex: 'completion' },
                { title: t('totalColumn'), dataIndex: 'total' },
                { title: t('costColumn'), dataIndex: 'cost' },
              ]}
            />
          </Card>

          <Card className="panel-card" title={t('agentUsage')}>
            <Table
              rowKey={(row) => row.config_id}
              dataSource={payload.agents || []}
              pagination={false}
              scroll={{ x: 900 }}
              columns={[
                { title: t('configIdColumn'), dataIndex: 'config_id' },
                { title: t('symbol'), dataIndex: 'symbol' },
                { title: t('promptColumn'), dataIndex: 'prompt' },
                { title: t('completionColumn'), dataIndex: 'completion' },
                { title: t('totalColumn'), dataIndex: 'total' },
              ]}
            />
          </Card>
        </>
      ) : null}
    </Space>
  );
}
