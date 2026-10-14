import React from 'react';
import { Alert, Select, Typography } from 'antd';

export default function ScorerProviderFields({ value, onChange, locale }) {
  const zh = locale === 'zh';
  const official = value.decisions_api === 'typesafe';
  return <div className="settings-stack">
    <div className="form-field">
      <label>{zh ? 'Jev 服务商' : 'Jev service'}</label>
      <Select aria-label={zh ? 'Jev 服务商' : 'Jev service'} value={value.decisions_api || 'bai'} options={[
        { value: 'typesafe', label: zh ? 'TypeSafe 官方 · System One API' : 'TypeSafe official · System One API' },
        { value: 'bai', label: 'B.AI · Decisions API' },
      ]} onChange={(decisions_api) => onChange({ decisions_api, api_base: decisions_api === 'typesafe' ? 'https://api.typesafe.ai/v1' : 'https://api.b.ai/v1', thinking_enabled: false })} />
    </div>
    <Alert type="info" showIcon title={zh ? '专用于消息相关度评分' : 'For news relevance scoring'} description={zh
      ? `${official ? '使用 TypeSafe 官方 API Key，通过 /v1/systemone 调用。' : '使用 B.AI API Key，通过 /v1/decisions 调用。'} Jev 返回结构化评分，摘要与对话请另选生成模型。`
      : `${official ? 'Uses a TypeSafe API key and /v1/systemone.' : 'Uses a B.AI API key and /v1/decisions.'} Jev returns structured scores. Select a generative model for summaries and chat.`} />
    <Typography.Link href={official ? 'https://api.typesafe.ai/docs' : 'https://docs.b.ai/'} target="_blank" rel="noreferrer">{zh ? '接口文档' : 'API documentation'}</Typography.Link>
  </div>;
}
