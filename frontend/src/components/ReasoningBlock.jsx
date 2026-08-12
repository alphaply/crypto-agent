import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Collapse, Tag, Typography } from 'antd';
import {
  BulbOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';
import { formatReasoningElapsed, formatReasoningSize, parseReasoningTimestamp } from '../lib/reasoning';

const { Text } = Typography;

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
  title = 'Reasoning',
  streaming = false,
  reasoningTokens = 0,
  startedAt,
}) {
  const { locale } = usePreferences();
  const reasoning = String(content || '').trim();
  const [activeKeys, setActiveKeys] = useState(streaming ? ['reasoning'] : []);
  const manualPreference = useRef(false);
  const previousStreaming = useRef(streaming);
  const expanded = activeKeys.includes('reasoning');
  const preview = useMemo(() => {
    if (!reasoning) return '';
    const compact = reasoning.replace(/\s+/g, ' ').trim();
    return compact.length > 140 ? `${compact.slice(0, 140)}\u2026` : compact;
  }, [reasoning]);

  useEffect(() => {
    if (previousStreaming.current !== streaming && !manualPreference.current) {
      setActiveKeys(streaming ? ['reasoning'] : []);
    }
    previousStreaming.current = streaming;
  }, [streaming]);

  if (!reasoning && !streaming) return null;

  const sizeLabel = formatReasoningSize(reasoningTokens, reasoning.length, locale);

  return (
    <Collapse
      className={`reasoning-block ${streaming ? 'is-streaming' : 'is-complete'}`}
      activeKey={activeKeys}
      onChange={(keys) => {
        manualPreference.current = true;
        setActiveKeys(keys);
      }}
      items={[
        {
          key: 'reasoning',
          label: (
            <span className="reasoning-block__label">
              <span className="reasoning-block__title">
                <BulbOutlined />
                <Text strong>{title}</Text>
              </span>
              {streaming ? (
                <Tag color="processing" icon={<LoadingOutlined spin />}>
                  {locale === 'zh' ? '\u601d\u8003\u4e2d' : 'Thinking'}
                </Tag>
              ) : (
                <Tag color="success" icon={<CheckCircleOutlined />}>
                  {locale === 'zh' ? '\u601d\u8003\u5b8c\u6210' : 'Complete'}
                </Tag>
              )}
              <Tag className="reasoning-block__metric" icon={streaming ? <ClockCircleOutlined /> : null}>
                {streaming ? <ElapsedReasoningMetric startedAt={startedAt} locale={locale} /> : sizeLabel}
              </Tag>
              {!expanded && reasoning ? (
                <Text type="secondary" className="reasoning-block__preview">
                  {preview}
                </Text>
              ) : null}
            </span>
          ),
          children: reasoning ? (
            <div className="reasoning-block__content" aria-live={streaming ? 'polite' : 'off'}>
              <MarkdownBlock content={reasoning} streaming={streaming} />
            </div>
          ) : null,
        },
      ]}
    />
  );
}
