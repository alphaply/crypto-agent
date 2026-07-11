import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Collapse, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';

const { Text } = Typography;

export default function ReasoningBlock({ content, title = 'Reasoning', streaming = false }) {
  const { t } = usePreferences();
  const reasoning = String(content || '').trim();
  const [activeKeys, setActiveKeys] = useState(streaming ? ['reasoning'] : []);
  const manualPreference = useRef(false);
  const previousStreaming = useRef(streaming);
  const preview = useMemo(() => {
    if (!reasoning) return '';
    return reasoning.length > 180 ? `${reasoning.slice(0, 180)}...` : reasoning;
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
              <Text strong>{title}</Text>
              {streaming ? <Tag color="processing">Streaming</Tag> : null}
              <Tag>{reasoning.length} {t('characters')}</Tag>
              <Text type="secondary" className="reasoning-block__preview">
                {preview}
              </Text>
            </span>
          ),
          children: <MarkdownBlock content={reasoning} />,
        },
      ]}
    />
  );
}
