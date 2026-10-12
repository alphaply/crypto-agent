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
  diagnostics,
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

  if (typeof diagnostics === 'string') { try { diagnostics = JSON.parse(diagnostics); } catch { diagnostics = null; } }
  const diagnostic = Array.isArray(diagnostics) ? diagnostics.at(-1) : diagnostics;
  if (!reasoning && !streaming && !reasoningTokens && !diagnostic) return null;

  const state = reasoning ? 'text_available' : reasoningTokens > 0 ? 'usage_only' : diagnostic?.evidence_state;
  const evidenceLabels = isZh ? { text_available: '文本可见', usage_only: '仅返回用量', requested_no_evidence: '已请求 · 未返回证据', disabled: '已明确关闭', not_requested: '未请求推理' } : { text_available: 'Text available', usage_only: 'Usage only', requested_no_evidence: 'Requested · no evidence', disabled: 'Explicitly disabled', not_requested: 'Not requested' };
  const effort = diagnostic?.sent_parameters?.reasoning_effort || diagnostic?.sent_parameters?.output_config?.effort || diagnostic?.requested_effort;
  const noText = state === 'disabled'
    ? (isZh ? '本次请求已显式关闭推理。' : 'Reasoning was explicitly disabled for this request.')
    : state === 'requested_no_evidence'
      ? (isZh ? '请求已携带推理设置，但服务商未返回推理文本或正数推理用量；无法确认实际执行档位。' : 'Reasoning settings were sent, but no text or positive reasoning usage was returned. Actual effort is unverified.')
      : state === 'not_requested'
        ? (isZh ? '本次请求未携带推理设置。' : 'No reasoning settings were sent.')
        : (isZh ? '服务商仅返回推理 token 用量，未提供可展示的推理文本。' : 'The provider reported reasoning-token usage without displayable reasoning text.');

  const displayContent = reasoning || (streaming
    ? (isZh ? '等待服务商返回可展示的推理内容…' : 'Waiting for displayable reasoning from the provider…')
    : noText);

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
        aria-controls={expanded ? contentId : undefined}
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
            (!reasoning && !reasoningTokens ? evidenceLabels[state] : sizeLabel) || (isZh ? '完成' : 'Done')
          )}
        </span>
      </button>

      {expanded ? (
        <div id={contentId} className="reasoning-block__content" aria-live={streaming ? 'polite' : 'off'}>
          <div className="reasoning-block__body">
            {diagnostic && <p style={{ overflowWrap: 'anywhere', opacity: .75 }}>{diagnostic.model} · {diagnostic.protocol}{effort ? ` · effort=${effort}` : ''} · {evidenceLabels[state]}{diagnostic.configuration_warning ? ` · ${diagnostic.configuration_warning}` : ''}</p>}
            <MarkdownBlock content={displayContent} streaming={streaming} />
          </div>
        </div>
      ) : null}
    </div>
  );
}
