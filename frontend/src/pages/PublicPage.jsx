import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Segmented, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import LineChart from '../components/LineChart';
import { api } from '../lib/api';
import { costEntries, dailyCostSeries } from '../lib/usage';
import { usePreferences } from '../app/usePreferences';

const { Title, Paragraph, Text } = Typography;
const purposeNames = {
  decision: '交易决策', strategy_summary: '运行摘要', memory_review: '短期记忆',
  chat: '聊天', chat_summary: '聊天压缩', news_score: '消息评分',
  news_summary: '消息摘要', daily_summary: '历史每日归档', legacy: '历史统计',
};

export default function PublicPage() {
  const { t, locale } = usePreferences();
  const zh = locale === 'zh';
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [dimension, setDimension] = useState('providers');
  const [query, setQuery] = useState('');
  const [currency, setCurrency] = useState('USD');
  const unknown = zh ? '未知' : 'Unknown';

  useEffect(() => {
    const controller = new AbortController();
    api.get('/public/usage', { signal: controller.signal }).then((response) => {
      setPayload(response.data);
      setError('');
    }).catch((err) => {
      if (!controller.signal.aborted) setError(err.message || 'Failed to load usage');
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [revision]);

  const daily = payload?.daily || [];
  const summary = payload?.summary || {};
  const today = payload?.today || {};
  const currencies = [...new Set(daily.flatMap((row) => costEntries(row).map(([unit]) => unit)))].sort();
  const selectedCurrency = currencies.includes(currency) ? currency : currencies[0];
  const tokenSeries = [{ name: t('dailyTokens'), data: [...daily].reverse().map((row) => ({ name: row.day, value: row.total })) }];
  const costSeries = dailyCostSeries(daily, selectedCurrency);
  const field = { providers: 'provider_id', models: 'model', agents: 'config_id', purposes: 'purpose' }[dimension];
  const rows = (payload?.[dimension] || []).filter((row) => `${row[field]} ${purposeNames[row[field]] || ''}`.toLowerCase().includes(query.toLowerCase()));

  const renderCost = (row) => {
    const entries = costEntries(row);
    return <Space direction="vertical" size={0}>
      {entries.length ? entries.map(([unit, value]) => <Text key={unit}>{Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })} <Text type="secondary">{unit}</Text></Text>) : <Text type="secondary">{unknown}</Text>}
      {row.unpriced_calls > 0 ? <Text type="secondary" style={{ fontSize: 12 }}>{row.unpriced_calls} {zh ? '次费用未知' : 'unpriced calls'}</Text> : null}
    </Space>;
  };
  const number = (value) => value == null ? unknown : Number(value).toLocaleString();

  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <Card className="hero-card">
      <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
        <div>
          <Text type="secondary">{zh ? '使用与成本' : 'USAGE & COSTS'}</Text>
          <Title level={2} style={{ margin: '6px 0' }}>{zh ? '模型用量' : 'Model usage'}</Title>
          <Paragraph type="secondary" style={{ margin: 0 }}>{zh ? '按渠道、模型、任务与用途查看调用量，费用按调用时的价格保存。' : 'Explore calls by provider, model, task, and purpose. Costs retain the rates used at call time.'}</Paragraph>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => { setLoading(true); setRevision((value) => value + 1); }}>{zh ? '刷新' : 'Refresh'}</Button>
      </Space>
    </Card>
    {error ? <Alert type="error" title={error} showIcon /> : null}
    {loading && !payload ? <Card className="panel-card loading-card"><Spin /></Card> : null}
    {payload ? <>
      <div className="metric-grid">
        <Card className="panel-card metric-card"><Statistic title={zh ? '今日调用' : 'Calls today'} value={today.calls || 0} /><Text type="secondary">{number(today.total || 0)} tokens</Text></Card>
        <Card className="panel-card metric-card"><Text type="secondary">{zh ? '今日已知费用' : 'Known cost today'}</Text><div style={{ marginTop: 12 }}>{renderCost(today)}</div></Card>
        <Card className="panel-card metric-card"><Statistic title={t('totalTokens')} value={summary.total_tokens_14d || 0} /><Text type="secondary">{summary.tracked_models || payload.models?.length || 0} {zh ? '个模型' : 'models'}</Text></Card>
        <Card className="panel-card metric-card"><Text type="secondary">{zh ? '累计已知费用' : 'Total known cost'}</Text><div style={{ marginTop: 12 }}>{renderCost(summary)}</div></Card>
      </div>
      <div className="split-grid">
        <Card className="panel-card" title={t('dailyTokens')}><div className="chart-wrap">{daily.length ? <LineChart series={tokenSeries} yName="Tokens" /> : <Empty description={t('noData')} />}</div></Card>
        <Card className="panel-card" title={zh ? '每日已知费用' : 'Known daily cost'} extra={currencies.length ? <Select aria-label={zh ? '计价币种' : 'Cost currency'} value={selectedCurrency} onChange={setCurrency} options={currencies.map((value) => ({ label: value, value }))} style={{ width: 90 }} /> : null}>
          <div className="chart-wrap">{selectedCurrency ? <LineChart series={costSeries} yName={selectedCurrency} area /> : <Empty description={zh ? '暂无已计价用量' : 'No priced usage yet'} />}</div>
        </Card>
      </div>
      <Card className="panel-card" title={zh ? '用量明细' : 'Usage breakdown'}>
        <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 20 }} wrap>
          <Segmented value={dimension} onChange={setDimension} options={[
            { value: 'providers', label: zh ? '渠道' : 'Providers' }, { value: 'models', label: zh ? '模型' : 'Models' },
            { value: 'agents', label: zh ? '任务' : 'Tasks' }, { value: 'purposes', label: zh ? '用途' : 'Purposes' },
          ]} />
          <Input.Search aria-label={zh ? '筛选用量' : 'Filter usage'} placeholder={zh ? '搜索名称' : 'Search name'} value={query} onChange={(event) => setQuery(event.target.value)} allowClear style={{ width: 240 }} />
        </Space>
        <Table rowKey={field} dataSource={rows} pagination={{ pageSize: 12, hideOnSinglePage: true }} scroll={{ x: 950 }} columns={[
          { title: zh ? '名称' : 'Name', dataIndex: field, render: (value) => <Space>{zh ? purposeNames[value] || (value === 'unknown' ? '未归属渠道' : value) : value}{value === 'legacy' ? <Tag>{zh ? '迁移' : 'Migrated'}</Tag> : null}</Space> },
          { title: zh ? '调用' : 'Calls', dataIndex: 'calls', render: number, sorter: (a, b) => a.calls - b.calls },
          { title: t('promptColumn'), dataIndex: 'prompt', render: number },
          { title: t('completionColumn'), dataIndex: 'completion', render: number },
          { title: zh ? '缓存读取 / 写入' : 'Cache read / write', render: (_, row) => `${number(row.cache_read_tokens)} / ${number(row.cache_write_tokens)}` },
          { title: zh ? '失败' : 'Failed', dataIndex: 'failed_calls', render: number },
          { title: zh ? '已知费用' : 'Known cost', render: (_, row) => renderCost(row) },
        ]} />
        <Paragraph type="secondary" style={{ margin: '20px 0 0', fontSize: 12 }}>{zh ? '费用优先采用服务商返回值，否则按调用时的渠道费率估算；未知费用单独计数，不作为免费。不同币种分别展示。旧数据缺少渠道和调用时费率，标记为历史估算；原始调用详情的清理不影响累计统计。' : 'Provider-reported costs take precedence; otherwise costs use the channel rates saved with each call. Unknown costs are counted separately, currencies stay separate, and migrated records are historical estimates. Deleting raw call traces does not erase accounting totals.'}</Paragraph>
      </Card>
    </> : null}
  </Space>;
}
