import { useEffect, useState } from 'react';
import { Collapse, Tag } from 'antd';
import { CheckCircleOutlined, LoadingOutlined, ClockCircleOutlined } from '@ant-design/icons';

export default function ChatRunProgress({ run, streaming, locale }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!streaming) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [streaming]);
  if (!run) return null;
  const zh = locale === 'zh';
  const elapsed = Math.max(0, Math.floor(((run.endedAt || now) - run.startedAt) / 1000));
  const latest = run.events.at(-1);
  return <div className="chat-run-progress" role="status" aria-live="polite">
    <Collapse size="small" items={[{
      key: 'request',
      label: <span className="chat-run-summary">
        {streaming ? <LoadingOutlined spin /> : <CheckCircleOutlined />}
        <strong>{latest?.label || (zh ? '准备请求' : 'Preparing request')}</strong>
        <span className="chat-run-time"><ClockCircleOutlined /> {elapsed}s</span>
        <Tag>{run.characters.toLocaleString()} {zh ? '字符' : 'chars'}</Tag>
      </span>,
      children: <ol className="chat-run-timeline">{run.events.map((event, index) => <li key={index}>
        <time>+{((event.at - run.startedAt) / 1000).toFixed(1)}s</time><span>{event.label}</span>
      </li>)}<li className="chat-run-note">{zh ? '显示实际请求事件及服务商返回的推理摘要。未提供的内部思考不会被补写。' : 'Actual request events and provider reasoning summaries only.'}</li></ol>,
    }]} />
  </div>;
}
