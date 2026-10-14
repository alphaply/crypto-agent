import React from 'react';
import { Alert, AutoComplete, Button, Space, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { discoveryIssue, modelMatches } from '../lib/providerModelDiscovery';

export default function ProviderModelSelect({ value, onChange, state, onRefresh, locale }) {
  const zh = locale === 'zh';
  return <div className="form-field" data-testid="provider-model-discovery">
    <label htmlFor="provider-model-id">Model *</label>
    <AutoComplete id="provider-model-id" aria-label="Model" value={value || ''} onChange={onChange}
      style={{ width: '100%' }} placeholder={zh ? '搜索候选或手动填写模型 ID' : 'Search models or enter a model ID'}
      options={state.models.map((model) => ({ value: model.id, model,
        label: <div style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
          <div>{model.name}</div>{model.name !== model.id && <Typography.Text type="secondary">{model.id}</Typography.Text>}
        </div> }))}
      filterOption={(input, option) => modelMatches(input, option.model)}
      defaultActiveFirstOption={false} backfill={false} />
    <Space wrap>
      <Button icon={<ReloadOutlined />} loading={state.loading} disabled={!state.ready} onClick={onRefresh}>
        {zh ? '刷新模型' : 'Refresh models'}
      </Button>
      <Typography.Text type="secondary">{zh ? '来自当前供应商，可随时手动填写。' : 'From this supplier. Manual entry is always available.'}</Typography.Text>
    </Space>
    <div aria-live="polite" role="status">
      {!state.ready && <Typography.Text type="secondary">{discoveryIssue({ code: state.incomplete || 'missing_credentials' }, locale)}</Typography.Text>}
      {state.loading && <Typography.Text type="secondary">{zh ? '正在获取模型列表…' : 'Fetching models…'}</Typography.Text>}
      {state.loaded && <Typography.Paragraph type="secondary" style={{ margin: '4px 0', overflowWrap: 'anywhere' }}>
        {state.stale ? (zh ? '上次结果（已过期）' : 'Previous result (stale)') : (zh ? '获取时间' : 'Fetched')}: {state.fetchedAt ? new Date(state.fetchedAt).toLocaleTimeString() : '—'}
        {state.source?.url && <> · {state.source.url}</>}
      </Typography.Paragraph>}
      {state.loaded && !state.models.length && !state.loading && <Typography.Text type="secondary">
        {zh ? '供应商未返回可用模型，可以手动填写模型 ID。' : 'No models returned. You can enter a model ID manually.'}
      </Typography.Text>}
      {!state.complete && <Alert type="warning" showIcon title={zh ? '部分模型列表' : 'Partial model list'} description={discoveryIssue(state.warning, locale)} />}
      {state.error && <Alert type="warning" showIcon title={discoveryIssue(state.error, locale)} />}
    </div>
  </div>;
}
