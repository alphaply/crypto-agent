import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Collapse, Tag, Typography } from 'antd';
import { BulbOutlined, CheckCircleOutlined, LoadingOutlined } from '@ant-design/icons';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';

const { Text } = Typography;

export default function ReasoningBlock({ content, title = 'Reasoning', streaming = false }) {
  const { t, locale } = usePreferences();
  const reasoning = String(content || '').trim();
  const [activeKeys, setActiveKeys] = useState(streaming ? ['reasoning'] : []);
  const manualPreference = useRef(false);
  const previousStreaming = useRef(streaming);
  const expanded = activeKeys.includes('reasoning');
  const preview = useMemo(() => {
    if (!reasoning) return '';
    const compact = reasoning.replace(/\s+/g, ' ').trim();
    return compact.length > 140 ? `${compact.slice(0, 140)}…` : compact;
  }, [reasoning]);

  useEffect(() => {
    if (previousStreaming.current !== streaming && !manualPreference.current) {
      setActiveKeys(streaming ? ['reasoning'] : []);
    }
    previousStreaming.current = streaming;
  }, [streaming]);

  if (!reasoning) {
    return null;
  }

  return (
    <Collapse
      className="reasoning-block"
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
                  {locale === 'zh' ? '正在思考' : 'Thinking'}
                </Tag>
              ) : (
                <Tag color="success" icon={<CheckCircleOutlined />}>
                  {locale === 'zh' ? '思考完成' : 'Complete'}
                </Tag>
              )}
              <Tag className="reasoning-block__count">{reasoning.length} {t('characters')}</Tag>
              {!expanded ? (
                <Text type="secondary" className="reasoning-block__preview">
                  {preview}
                </Text>
              ) : null}
            </span>
          ),
          children: (
            <div className="reasoning-block__content" aria-live={streaming ? 'polite' : 'off'}>
              <MarkdownBlock content={reasoning} />
            </div>
          ),
        },
      ]}
    />
  );
}
