import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Select, Space, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { savedScorerConnection } from '../lib/scorerProvider';

export default function ScorerProviderFields({ value, saved, onChange, locale }) {
  const zh = locale === 'zh';
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const request = useRef(null);
  const canDiscover = savedScorerConnection(value, saved);
  const official = value.decisions_api === 'typesafe';
  useEffect(() => () => request.current?.abort(), []);
  const loadModels = async () => {
    if (!canDiscover || loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    try {
      const { data } = await api.get('/news/scorer-models', { params: { provider_id: value.provider_id }, signal: controller.signal });
      if (!controller.signal.aborted) { setModels(data.models || []); setLoaded(true); }
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  return <div className="settings-stack">
    <div className="form-field">
      <label>{zh ? 'Jev 服务商' : 'Jev service'}</label>
      <Select value={value.decisions_api || 'bai'} options={[
        { value: 'typesafe', label: zh ? 'TypeSafe 官方 · System One API' : 'TypeSafe official · System One API' },
        { value: 'bai', label: 'B.AI · Decisions API' },
      ]} onChange={(decisions_api) => onChange({ decisions_api, api_base: decisions_api === 'typesafe' ? 'https://api.typesafe.ai/v1' : 'https://api.b.ai/v1', thinking_enabled: false })} />
    </div>
    <Alert type="info" showIcon title={zh ? '专用于消息相关度评分' : 'For news relevance scoring'} description={zh
      ? `${official ? '使用 TypeSafe 官方 API Key，通过 /v1/systemone 调用。' : '使用 B.AI API Key，通过 /v1/decisions 调用。'} Jev 返回结构化评分，摘要与对话请另选生成模型。`
      : `${official ? 'Uses a TypeSafe API key and /v1/systemone.' : 'Uses a B.AI API key and /v1/decisions.'} Jev returns structured scores. Select a generative model for summaries and chat.`} />
    <div className="form-field">
      <label>{zh ? '账户可用模型' : 'Models available to this account'}</label>
      <Space wrap>
        <Button icon={<ReloadOutlined />} loading={loading} disabled={!canDiscover} onClick={loadModels}>{zh ? '获取模型列表' : 'Fetch models'}</Button>
        <Typography.Link href={official ? 'https://api.typesafe.ai/docs' : 'https://docs.b.ai/'} target="_blank" rel="noreferrer">{zh ? '接口文档' : 'API documentation'}</Typography.Link>
      </Space>
      {!canDiscover && <Typography.Text type="secondary">{zh ? '先保存该渠道及页面配置，再读取账户模型列表。也可在上方手动填写模型名称。' : 'Save the provider and page settings before fetching models. You can also enter a model name above.'}</Typography.Text>}
      {models.length > 0 && <Select showSearch optionFilterProp="label" value={models.some((model) => model.id === value.model) ? value.model : undefined} placeholder={zh ? '选择可用的 Jev 模型' : 'Select an available Jev model'} options={models.map((model) => ({ value: model.id, label: model.name || model.id, title: model.description }))} onChange={(model) => onChange({ model })} />}
      {!loading && canDiscover && loaded && models.length === 0 && !error && <Typography.Text type="secondary">{zh ? '该账户未返回可用模型，请检查渠道权限。' : 'No models returned for this account. Check provider permissions.'}</Typography.Text>}
      {error && <Alert type="error" showIcon title={error} />}
    </div>
  </div>;
}
