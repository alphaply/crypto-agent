import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, InputNumber, Space, Switch, Tag, Typography } from 'antd';
import { ReloadOutlined, PlusOutlined, DeleteOutlined, LinkOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Text } = Typography;
const EXAMPLE = 'https://polymarket.com/zh/event/fed-decision-in-september-762';
const percentage = (value) => value == null ? '—' : `${(value * 100).toFixed(2)}%`;
const money = (value) => value == null ? '—' : new Intl.NumberFormat('en', { notation: 'compact', style: 'currency', currency: 'USD', maximumFractionDigits: 1 }).format(value);

export function PredictionEvents({ events = [] }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  return <div className="prediction-events">{events.map((event) => (
    <article className="prediction-event" key={event.slug}>
      <div className="prediction-event-heading">
        <a href={event.url} target="_blank" rel="noreferrer"><strong>{event.title}</strong> <LinkOutlined /></a>
        {event.closed && <Tag>{zh ? '已关闭' : 'Closed'}</Tag>}
        {event.stale && <Tag color="orange">{zh ? '缓存 · 非实时' : 'Stale cache'}</Tag>}
      </div>
      <div className="prediction-meta">24h {money(event.volume_24h)} · {zh ? '流动性' : 'Liquidity'} {money(event.liquidity)}</div>
      <div className="prediction-market-list">{event.markets.map((market) => {
        const primary = market.outcomes.find((o) => o.label.toLowerCase() === 'yes') || market.outcomes[0];
        return <div className="prediction-market" key={market.id || market.question}>
          <div className="prediction-market-heading"><span>{market.title}</span><strong>{primary?.label} {percentage(primary?.probability)}</strong></div>
          <div className="probability-track" aria-label={`${primary?.label}: ${percentage(primary?.probability)}`}><span style={{ width: `${(primary?.probability || 0) * 100}%` }} /></div>
          <div className="prediction-quote">
            <span>{market.closed ? (zh ? '已关闭' : 'Closed') : !market.active ? (zh ? '未激活' : 'Inactive') : `${market.outcomes[0]?.label || ''} ${zh ? '买 / 卖' : 'bid / ask'} ${percentage(market.best_bid)} / ${percentage(market.best_ask)}`}</span>
            <span>{market.change_24h == null ? '24h —' : `24h ${market.change_24h > 0 ? '+' : ''}${(market.change_24h * 100).toFixed(2)} pp`}</span>
          </div>
          {market.outcomes.length > 2 && <div className="prediction-meta">{market.outcomes.map((o) => `${o.label} ${percentage(o.probability)}`).join(' · ')}</div>}
        </div>;
      })}</div>
      <div className="prediction-meta">{zh ? '采集' : 'Fetched'} {event.fetched_at ? dayjs(event.fetched_at).format('MM-DD HH:mm:ss') : '—'}{event.end_date ? ` · ${zh ? '截止' : 'Ends'} ${dayjs(event.end_date).format('YYYY-MM-DD')}` : ''}</div>
    </article>
  ))}</div>;
}

export default function PolymarketPanel() {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    let timer;
    async function refresh() {
      setLoading(true);
      try {
        const response = await api.get('/public/polymarket');
        if (active) { setData(response.data); setError(''); }
      } catch (err) {
        if (active) { setData(null); setError(err.message); }
      } finally {
        if (active) { setLoading(false); timer = window.setTimeout(refresh, 60000); }
      }
    }
    refresh();
    return () => { active = false; window.clearTimeout(timer); };
  }, [revision]);
  const failed = Object.entries(data?.source_health || {}).filter(([, source]) => source.status !== 'ok');
  return <Card className="panel-card prediction-panel" title={<span><span className="signal-dot" /> Polymarket <Text type="secondary"> / {zh ? '市场预期' : 'Expectations'}</Text></span>} extra={<Button type="text" aria-label={zh ? '刷新预测市场' : 'Refresh prediction markets'} icon={<ReloadOutlined />} loading={loading} onClick={() => setRevision((v) => v + 1)} />}>
    <p className="prediction-meta">{zh ? '价格隐含概率，不代表已确认的事件结果。' : 'Market-implied probabilities, not confirmed outcomes.'}</p>
    {error && <Alert type="error" title={error} />}
    {failed.map(([key, source]) => <Alert key={key} type="warning" title={`${key.replace('polymarket:', '')}: ${source.status}`} description={source.error} />)}
    {data?.events?.length ? <PredictionEvents events={data.events} /> : !loading && !error ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data?.enabled ? (zh ? '暂无可用盘口，请检查消息源配置。' : 'No markets available. Check source settings.') : (zh ? '在控制台 → 消息源启用 Polymarket 监控' : 'Enable Polymarket in Console → News sources')} /> : null}
    {data?.enabled && <div className="prediction-meta">{zh ? '缓存刷新间隔' : 'Cache refresh interval'} {data.refresh_seconds}s · Gamma API</div>}
  </Card>;
}

export function PolymarketSettings({ value, onChange }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const settings = value || { enabled: false, events: [], refresh_seconds: 300 };
  const [url, setUrl] = useState('');
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  async function test() {
    setTesting(true); setError(''); setPreview(null);
    try { const response = await api.post('/config/polymarket/test', { event: url }); setPreview(response.data.event); }
    catch (err) { setError(err.message); }
    finally { setTesting(false); }
  }
  function add() {
    if (!preview || settings.events.includes(preview.slug) || settings.events.length >= 8) return;
    onChange({ ...settings, events: [...settings.events, preview.slug] });
    setPreview(null); setUrl('');
  }
  return <Card className="panel-card" title={zh ? '消息源 · Polymarket' : 'News sources · Polymarket'}>
    <Space orientation="vertical" size="large" style={{ width: '100%' }}>
      <Alert type="info" showIcon title={zh ? '公开盘口，无需 API Key 或钱包' : 'Public market data. No API key or wallet needed.'} description={zh ? '从服务器读取事件概率、买卖报价、24h 成交量和流动性，加入消息面与 Agent 分析。连接测试由实际后端发起，可验证新加坡服务器的访问情况。' : 'Fetch probabilities, quotes, volume and liquidity for news and agent analysis. Test the connection from your actual backend server.'} />
      <div className="prediction-settings-controls"><label>{zh ? '启用监控' : 'Enable monitoring'} <Switch checked={settings.enabled} onChange={(enabled) => onChange({ ...settings, enabled })} /></label><label>{zh ? '缓存刷新间隔（秒）' : 'Cache interval (seconds)'} <InputNumber min={60} max={3600} value={settings.refresh_seconds} onChange={(refresh_seconds) => onChange({ ...settings, refresh_seconds: refresh_seconds ?? 300 })} /></label></div>
      <Text type="secondary">{zh ? '看板与 Agent 请求时按缓存间隔采集；失败时最多使用 1 小时缓存并显示过期标记。监控列表会公开显示在看板，最多 8 个事件。' : 'Fetch on dashboard and agent requests using the cache interval. Failures may use a marked cache up to one hour old. Up to eight events, publicly visible on the dashboard.'}</Text>
      <div className="prediction-add-row"><Input aria-label={zh ? 'Polymarket 事件链接' : 'Polymarket event URL'} placeholder={EXAMPLE} value={url} onChange={(e) => { setUrl(e.target.value); setPreview(null); setError(''); }} /><Button onClick={test} loading={testing} disabled={!url.trim()}>{zh ? '测试连接' : 'Test connection'}</Button></div>
      <Button type="link" onClick={() => { setUrl(EXAMPLE); setPreview(null); }}>{zh ? '填入示例：9 月美联储利率决议' : 'Use example: September Fed decision'}</Button>
      {error && <Alert type="error" title={error} />}
      {preview && <><Alert type="success" title={zh ? '服务器连接成功' : 'Server connection succeeded'} /><PredictionEvents events={[preview]} /><Button type="primary" icon={<PlusOutlined />} disabled={settings.events.includes(preview.slug) || settings.events.length >= 8} onClick={add}>{zh ? '加入监控列表' : 'Add to watchlist'}</Button></>}
      <div className="prediction-watchlist">{settings.events.map((slug) => <div key={slug}><a href={`https://polymarket.com/event/${slug}`} target="_blank" rel="noreferrer">{slug}</a><Button type="text" danger icon={<DeleteOutlined />} aria-label={`${zh ? '移除' : 'Remove'} ${slug}`} onClick={() => onChange({ ...settings, events: settings.events.filter((item) => item !== slug) })} /></div>)}</div>
    </Space>
  </Card>;
}
