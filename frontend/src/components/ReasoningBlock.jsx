import React, { useEffect, useId, useRef, useState } from 'react';
import {
  DownOutlined,
  RightOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';
import { formatReasoningElapsed, formatReasoningSize, parseReasoningTimestamp } from '../lib/reasoning';

function ElapsedReasoningMetric({ startedAt, locale }) {
  const [snapshot, setSnapshot] = useState(null);

  useEffect(() => {
    const knownStartedAt = parseReasoningTimestamp(startedAt);
    const localStartedAt = Date.now();
    const updateClock = () => {
      setSnapshot({ now: Date.now(), startedAt: knownStartedAt ?? localStartedAt });
    };
    const initialTimer = window.setTimeout(updateClock, 0);
    const interval = window.setInterval(updateClock, 1000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(interval);
    };
  }, [startedAt]);

  const elapsedMs = snapshot ? snapshot.now - snapshot.startedAt : 0;
  return formatReasoningElapsed(elapsedMs, locale);
}

export default function ReasoningBlock({
  content,
  title,
  streaming = false,
  reasoningTokens = 0,
  startedAt,
}) {
  const { locale } = usePreferences();
  const isZh = locale === 'zh';
  const reasoning = String(content || '').trim();
  const contentId = useId();

  // Stream state tracking: default to expanded during active streaming thinking,
  // then auto-collapse when answer finishes streaming.
  const [expanded, setExpanded] = useState(streaming);
  const userInteractedRef = useRef(false);
  const prevStreamingRef = useRef(streaming);

  useEffect(() => {
    if (prevStreamingRef.current !== streaming) {
      if (!userInteractedRef.current) {
        // Auto-open during streaming thinking, auto-collapse on finish (Claude/ChatGPT pattern)
        setExpanded(streaming);
      }
      prevStreamingRef.current = streaming;
    }
  }, [streaming]);

  if (!reasoning && !streaming) return null;

  const toggleExpand = () => {
    userInteractedRef.current = true;
    setExpanded((prev) => !prev);
  };

  const sizeLabel = formatReasoningSize(reasoningTokens, reasoning.length, locale);
  const displayTitle = title || (isZh ? '深度思考' : 'Reasoning');

  return (
    <div className={`reasoning-block ${streaming ? 'is-streaming' : 'is-complete'} ${expanded ? 'is-expanded' : 'is-collapsed'}`}>
      <button
        type="button"
        className="reasoning-block__trigger"
        onClick={toggleExpand}
        aria-expanded={expanded}
        aria-controls={expanded && reasoning ? contentId : undefined}
      >
        <span className="reasoning-block__icon">
          {streaming ? <LoadingOutlined spin /> : (expanded ? <DownOutlined /> : <RightOutlined />)}
        </span>
        <span className="reasoning-block__status">
          {streaming ? (
            <span className="reasoning-block__live-text">
              {isZh ? '正在思考' : 'Thinking'}
              <span className="reasoning-block__dots" />
            </span>
          ) : (
            <span className="reasoning-block__done-text">
              {displayTitle}
            </span>
          )}
        </span>
        <span className="reasoning-block__metric-badge">
          {streaming ? (
            <ElapsedReasoningMetric startedAt={startedAt} locale={locale} />
          ) : (
            sizeLabel || (isZh ? '完成' : 'Done')
          )}
        </span>
      </button>

      {expanded && reasoning ? (
        <div id={contentId} className="reasoning-block__content" aria-live={streaming ? 'polite' : 'off'}>
          <div className="reasoning-block__body">
            <MarkdownBlock content={reasoning} streaming={streaming} />
          </div>
        </div>
      ) : null}
    </div>
  );
}
