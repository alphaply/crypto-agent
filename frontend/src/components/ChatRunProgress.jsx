import React, { useEffect, useState } from 'react';
import { Popover } from 'antd';
import { LoadingOutlined, InfoCircleOutlined, ClockCircleOutlined } from '@ant-design/icons';

export default function ChatRunProgress({ run, streaming, locale }) {
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    if (!streaming) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [streaming]);

  if (!run || (!streaming && !run.events?.length)) return null;

  const zh = locale === 'zh';
  const elapsed = Math.max(0, Math.floor(((run.endedAt || now) - run.startedAt) / 1000));
  const latest = run.events.at(-1);

  // When completed, show an unobtrusive tiny chip; when streaming, show active subtle pill
  const timelineContent = (
    <div className="chat-run-popover-content">
      <div className="chat-run-popover-header">
        <strong>{zh ? '请求事件追踪' : 'Request Event Trace'}</strong>
        <span>{elapsed}s · {run.characters.toLocaleString()} {zh ? '字符' : 'chars'}</span>
      </div>
      <ol className="chat-run-timeline">
        {run.events.map((event, index) => (
          <li key={index}>
            <time>+{((event.at - run.startedAt) / 1000).toFixed(1)}s</time>
            <span>{event.label}</span>
          </li>
        ))}
      </ol>
      <div className="chat-run-note">
        {zh ? '显示实际请求事件及服务商返回的推理摘要。未提供的内部思考不会被补写。' : 'Actual request events and provider reasoning summaries only.'}
      </div>
    </div>
  );

  return (
    <div className={`chat-run-progress ${streaming ? 'is-streaming' : 'is-idle'}`} role="status" aria-live="polite">
      <Popover content={timelineContent} trigger="click" placement="top">
        <button type="button" className="chat-run-pill">
          {streaming ? (
            <span className="chat-run-pill__pulse">
              <LoadingOutlined spin />
            </span>
          ) : (
            <InfoCircleOutlined className="chat-run-pill__info" />
          )}
          <span className="chat-run-pill__text">
            {latest?.label || (zh ? '准备就绪' : 'Ready')}
          </span>
          <span className="chat-run-pill__time">
            <ClockCircleOutlined /> {elapsed}s
          </span>
        </button>
      </Popover>
    </div>
  );
}
