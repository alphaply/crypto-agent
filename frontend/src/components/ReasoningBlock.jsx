import React, { useMemo } from 'react';
import { Collapse, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/preferences';

const { Text } = Typography;

export function splitThinkingContent(content = '', explicitReasoning = '') {
  const text = String(content || '');
  const match = text.match(/<thinking>\s*([\s\S]*?)\s*<\/thinking>/i);
  if (!match) {
    return { content: text, reasoning: explicitReasoning || '' };
  }
  const cleaned = text.replace(match[0], '').trim();
  const reasoning = explicitReasoning || match[1].trim();
  return { content: cleaned, reasoning };
}

export default function ReasoningBlock({ content, title = 'Reasoning' }) {
  const { t } = usePreferences();
  const reasoning = String(content || '').trim();
  const preview = useMemo(() => {
    if (!reasoning) return '';
    return reasoning.length > 180 ? `${reasoning.slice(0, 180)}...` : reasoning;
  }, [reasoning]);

  if (!reasoning) {
    return null;
  }

  return (
    <Collapse
      className="reasoning-block"
      items={[
        {
          key: 'reasoning',
          label: (
            <span className="reasoning-block__label">
              <Text strong>{title}</Text>
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
