import React from 'react';
import { Alert, Card, Empty, Space, Tag, Typography } from 'antd';
import { RadarChartOutlined, ClockCircleOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';
const { Text } = Typography;

export default function SharedNewsPanel({ snapshot }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const raw = snapshot?.raw || snapshot || {};
  const items = raw.items || [];
  const digest = raw.digest || raw.summary || '';
  const timestamp = snapshot?.timestamp || raw.generated_at;
  const unavailable = Object.entries(raw.source_health || {}).filter(([,state]) => !['ok','disabled'].includes(state.status));
  const safeLink = (value) => /^https?:\/\//i.test(value || '');
  return <Card className="panel-card shared-intelligence" title={<Space><RadarChartOutlined /><span>{zh ? '共享市场情报' : 'Shared market intelligence'}</span><Tag color="blue">1h</Tag></Space>} extra={<Text type="secondary">{timestamp ? dayjs(timestamp).format('MM-DD HH:mm') : (zh ? '等待首次更新' : 'Awaiting first update')}</Text>}>
    <div className="intelligence-status"><Text type="secondary"><ClockCircleOutlined /> {zh ? '所有任务使用同一份消息快照' : 'One snapshot shared by every task'}</Text><Space wrap><Tag>{zh ? '纳入' : 'Included'} {items.length}</Tag>{raw.filtered_count != null && <Tag>{zh ? '已评分' : 'Scored'} {raw.scored_count ?? '—'}</Tag>}{raw.next_update_at && <Text type="secondary">{zh ? '下次更新 ' : 'Next '}{dayjs(raw.next_update_at).format('HH:mm')}</Text>}</Space></div>
    {raw.stale || raw.error ? <Alert type="warning" showIcon title={zh ? '当前显示上次成功的情报快照' : 'Showing the last successful snapshot'} description={raw.error || undefined} style={{ marginBottom: 16 }} /> : null}
    {unavailable.length > 0 && <Alert style={{marginBottom:16}} type="warning" showIcon title={zh?'部分消息来源缺失':'Some sources are unavailable'} description={unavailable.map(([id,state])=>`${id}: ${state.error || state.status}`).join(' · ')}/>}
    <div className="intelligence-grid">
      <div className="intelligence-digest">{digest ? <MarkdownBlock content={digest} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '在系统设置中配置评分与摘要模型，完成后将每小时更新。' : 'Configure scoring and summary models in Settings to begin hourly updates.'} />}</div>
      <div className="intelligence-sources">
        {items.slice(0, 8).map((item, index) => <article key={item.id || item.url || index}><div className="intelligence-source-meta"><Text type="secondary">{item.source || item.category || 'News'}</Text>{(item.scoring?.score ?? item.relevance_score) != null && <Tag color="cyan">{Number(item.scoring?.score ?? item.relevance_score).toFixed(1)}</Tag>}</div>{safeLink(item.url) ? <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a> : <Text>{item.title}</Text>}</article>)}
        {items.length > 8 && <details><summary>{zh ? '更多消息' : 'More sources'} ({items.length - 8})</summary>{items.slice(8).map((item, index) => <article key={item.id || item.url || index}>{safeLink(item.url) ? <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a> : item.title}</article>)}</details>}
      </div>
    </div>
  </Card>;
}
