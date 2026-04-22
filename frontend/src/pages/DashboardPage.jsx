import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Descriptions, Empty, Row, Select, Space, Spin, Statistic, Table, Tabs, Tag, Typography } from 'antd';
import { api } from '../lib/api';
import KlineChart from '../components/KlineChart';

const { Paragraph, Text } = Typography;

function AgentDetail({ agent }) {
  const [positionData, setPositionData] = useState(null);
  const [klineData, setKlineData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const [positionResponse, klineResponse] = await Promise.all([
          api.get(`/stats/position/${agent.config_id}`),
          api.get(`/stats/kline/${agent.config_id}`, { params: { timeframe: '1h' } }),
        ]);
        if (!mounted) {
          return;
        }
        setPositionData(positionResponse.data);
        setKlineData(klineResponse.data);
      } catch (error) {
        if (mounted) {
          setPositionData({ success: false, message: error.message });
          setKlineData({ success: false, message: error.message });
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
  }, [agent.config_id]);

  if (loading) {
    return <Spin />;
  }

  return (
    <Tabs
      items={[
        {
          key: 'summary',
          label: 'Summary',
          children: (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions column={2} bordered size="small">
                <Descriptions.Item label="Config ID">{agent.config_id}</Descriptions.Item>
                <Descriptions.Item label="Next Run">{agent.next_run}</Descriptions.Item>
                <Descriptions.Item label="Mode">{agent.mode}</Descriptions.Item>
                <Descriptions.Item label="Freq">{agent.freq}</Descriptions.Item>
                <Descriptions.Item label="Leverage">{agent.leverage}</Descriptions.Item>
                <Descriptions.Item label="Enabled">{agent.enabled ? 'Yes' : 'No'}</Descriptions.Item>
              </Descriptions>
              <Paragraph>{agent.content || 'No analysis content yet.'}</Paragraph>
              <Paragraph type="secondary">{agent.strategy_logic || 'No strategy logic yet.'}</Paragraph>
              {agent.dca_stats ? (
                <Descriptions column={3} bordered size="small">
                  <Descriptions.Item label="Invested">{agent.dca_stats.total_invested}</Descriptions.Item>
                  <Descriptions.Item label="Qty">{agent.dca_stats.total_qty}</Descriptions.Item>
                  <Descriptions.Item label="Avg Cost">{agent.dca_stats.avg_cost}</Descriptions.Item>
                </Descriptions>
              ) : null}
            </Space>
          ),
        },
        {
          key: 'orders',
          label: 'Orders',
          children: (
            <Table
              size="small"
              pagination={false}
              rowKey={(row) => row.id || row.order_id || `${row.timestamp}-${row.side}`}
              dataSource={agent.all_orders || []}
              columns={[
                { title: 'Time', dataIndex: 'timestamp' },
                { title: 'Side', dataIndex: 'side' },
                { title: 'Entry', dataIndex: 'entry_price' },
                { title: 'Amount', dataIndex: 'amount' },
                { title: 'Status', dataIndex: 'status' },
              ]}
            />
          ),
        },
        {
          key: 'position',
          label: 'Position',
          children: positionData?.success === false ? (
            <Alert type="warning" message={positionData.message || 'Failed to load positions'} showIcon />
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions column={3} bordered size="small">
                <Descriptions.Item label="Mode">{positionData?.mode || '-'}</Descriptions.Item>
                <Descriptions.Item label="Balance">{positionData?.balance ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="Trades">{positionData?.summary?.total_trades ?? '-'}</Descriptions.Item>
              </Descriptions>
              <Table
                size="small"
                pagination={false}
                rowKey={(row) => `${row.side}-${row.entry_price}-${row.amount}`}
                dataSource={positionData?.positions || []}
                locale={{ emptyText: <Empty description="No active positions" /> }}
                columns={[
                  { title: 'Side', dataIndex: 'side' },
                  { title: 'Entry', dataIndex: 'entry_price' },
                  { title: 'Qty', dataIndex: 'amount' },
                  { title: 'PnL', dataIndex: 'unrealized_pnl' },
                  { title: 'ROI %', dataIndex: 'roi_pct' },
                ]}
              />
            </Space>
          ),
        },
        {
          key: 'kline',
          label: 'Kline',
          children: klineData?.success === false ? (
            <Alert type="error" message={klineData.message || 'Failed to load kline'} showIcon />
          ) : (
            <div className="chart-wrap">
              <KlineChart payload={klineData} />
            </div>
          ),
        },
      ]}
    />
  );
}

export default function DashboardPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [symbol, setSymbol] = useState('');
  const [payload, setPayload] = useState(null);

  useEffect(() => {
    if (!symbol && payload?.current_symbol) {
      setSymbol(payload.current_symbol);
    }
  }, [payload, symbol]);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const response = await api.get('/dashboard/overview', { params: symbol ? { symbol } : {} });
        if (mounted) {
          setPayload(response.data);
          if (!symbol) {
            setSymbol(response.data.current_symbol);
          }
        }
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load dashboard');
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
  }, [symbol]);

  const summaryCards = useMemo(() => {
    if (!payload) {
      return [];
    }
    return [
      { title: 'Scheduler', value: payload.scheduler_enabled ? 'On' : 'Off' },
      { title: 'Symbol Mode', value: payload.symbol_mode || '-' },
      { title: 'Symbol Freq', value: payload.symbol_freq || '-' },
      { title: 'Agents', value: payload.agent_summaries?.length || 0 },
    ];
  }, [payload]);

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="glass-card">
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
            <div>
              <Typography.Title level={3} style={{ margin: 0 }}>
                Dashboard
              </Typography.Title>
              <Text type="secondary">查看策略摘要、订单、持仓和 K 线。</Text>
            </div>
            <Select
              style={{ minWidth: 220 }}
              value={symbol || undefined}
              options={(payload?.symbols || []).map((item) => ({ label: item, value: item }))}
              onChange={setSymbol}
            />
          </Space>
          <div className="metric-grid">
            {summaryCards.map((item) => (
              <Card key={item.title} size="small">
                <Statistic title={item.title} value={item.value} />
              </Card>
            ))}
          </div>
          {error ? <Alert type="error" message={error} showIcon /> : null}
        </Space>
      </Card>

      {loading ? (
        <Card className="glass-card">
          <Spin />
        </Card>
      ) : null}

      {!loading &&
        (payload?.agent_summaries || []).map((agent) => (
          <Card
            key={agent.config_id}
            className="glass-card"
            title={
              <Space wrap>
                <Text strong>{agent.display_name}</Text>
                <Tag color={agent.enabled ? 'green' : 'default'}>{agent.mode}</Tag>
                <Tag color="blue">{agent.model}</Tag>
              </Space>
            }
            extra={<Text type="secondary">{agent.timestamp}</Text>}
          >
            <AgentDetail agent={agent} />
          </Card>
        ))}

      {!loading && !(payload?.agent_summaries || []).length ? (
        <Card className="glass-card">
          <Empty description="No agents configured for this symbol" />
        </Card>
      ) : null}
    </Space>
  );
}
