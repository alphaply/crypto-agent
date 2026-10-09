import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, InputNumber, Select, Space, Switch, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

export function PriceSyncPanel({ value = {}, onChange }) {
  const { locale } = usePreferences(); const zh = locale === 'zh';
  const [status, setStatus] = useState(null); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  useEffect(() => { let active = true; api.get('/pricing/sync/status').then((r) => { if (active) setStatus(r.data); }).catch((e) => { if (active) setError(e.message); }); return () => { active = false; }; }, []);
  const sync = async () => { setBusy(true); setError(''); try { const r = await api.post('/pricing/sync'); setStatus(r.data); } catch (e) { setError(e.message); } finally { setBusy(false); } };
  return <Card className="panel-card" title={zh ? '模型价格同步' : 'Model price synchronization'} extra={<Button icon={<ReloadOutlined />} loading={busy} onClick={sync}>{zh ? '同步 models.dev' : 'Sync models.dev'}</Button>}>
    <Space wrap><Switch checked={value.enabled !== false} onChange={(enabled) => onChange({ ...value, enabled })}/><Typography.Text>{zh ? '自动同步' : 'Auto sync'}</Typography.Text><InputNumber min={1} max={168} value={(value.interval_seconds || 21600) / 3600} onChange={(v) => onChange({ ...value, interval_seconds: (v || 6) * 3600 })}/><Typography.Text>{zh ? '小时' : 'hours'}</Typography.Text><Tag>{status?.status || '—'}</Tag></Space>
    <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>{zh ? '按渠道映射 models.dev 服务商与模型。人工价格优先；同步只影响后续调用，历史费用保留调用时价格。未知价格显示未定价。修改映射后请先保存配置，再同步。' : 'Map each local provider to a models.dev provider and model. Manual prices take priority; historical calls retain their original rates. Save mappings before syncing.'}</Typography.Paragraph>
    {status?.last_success_at && <Typography.Text type="secondary">{new Date(status.last_success_at).toLocaleString()}</Typography.Text>}
    {(error || status?.error) && <Alert type="warning" showIcon title={error || status.error}/>}
    {status?.unmatched_provider_ids?.length > 0 && <Alert style={{ marginTop: 12 }} type="info" title={(zh ? '未匹配渠道：' : 'Unmatched providers: ') + status.unmatched_provider_ids.join(', ')}/>}
  </Card>;
}

export function ProviderPricingFields({ value, onChange }) {
  const { locale } = usePreferences(); const zh = locale === 'zh';
  const [catalog, setCatalog] = useState([]); const [search, setSearch] = useState(''); const [error, setError] = useState(''); const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (value.pricing_mode !== 'models_dev') return;
    let active = true;
    const timer = setTimeout(() => { setLoading(true); api.get('/pricing/catalog', { params: { q: search || value.models_dev_model_id || value.model, limit: 100 } }).then((r) => { if (active) { setCatalog(r.data.models || []); setError(r.data.error || ''); } }).catch((e) => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); }); }, 300);
    return () => { active = false; clearTimeout(timer); };
  }, [search, value.pricing_mode, value.model, value.models_dev_model_id]);
  const fields = [['input_price_per_m', zh ? '输入' : 'Input'], ['output_price_per_m', zh ? '输出' : 'Output'], ['cache_read_price_per_m', zh ? '缓存读取' : 'Cache read'], ['cache_write_price_per_m', zh ? '缓存写入' : 'Cache write']];
  return <div className="settings-stack">
    <div className="form-field"><label>{zh ? '价格来源' : 'Price source'}</label><Select value={value.pricing_mode || 'manual'} options={[{ value: 'manual', label: zh ? '人工覆盖' : 'Manual override' }, { value: 'models_dev', label: 'models.dev' }]} onChange={(pricing_mode) => onChange({ pricing_mode })}/></div>
    {value.pricing_mode === 'models_dev' ? <>
      <Select showSearch filterOption={false} loading={loading} value={value.models_dev_provider_id && value.models_dev_model_id ? `${value.models_dev_provider_id}::${value.models_dev_model_id}` : undefined} placeholder={zh ? '搜索服务商与模型目录' : 'Search providers and models'} onSearch={setSearch} options={catalog.map((item) => ({ value: `${item.provider_id}::${item.model_id}`, label: `${item.provider_name || item.provider_id} / ${item.model_id}` }))} onChange={(selected) => { const item = catalog.find((row) => `${row.provider_id}::${row.model_id}` === selected); if (item) onChange({ models_dev_provider_id: item.provider_id, models_dev_model_id: item.model_id }); }}/>
      <div className="field-grid"><label className="form-field">models.dev provider<Input value={value.models_dev_provider_id || ''} onChange={(e) => onChange({ models_dev_provider_id: e.target.value })}/></label><label className="form-field">models.dev model<Input value={value.models_dev_model_id || ''} onChange={(e) => onChange({ models_dev_model_id: e.target.value })}/></label></div>
      {error && <Alert type="warning" title={error}/>}
      <Typography.Text type="secondary">{zh ? '当前生效价格：' : 'Effective rates: '}{fields.map(([key, label]) => `${label} ${value.effective_prices?.[key] ?? (zh ? '未定价' : 'Unpriced')}`).join(' · ')} {value.effective_prices?.currency || 'USD'} / 1M</Typography.Text>
    </> : <>
      <label className="form-field">{zh ? '计价货币' : 'Currency'}<Select value={value.pricing_currency || 'USD'} options={['USD', 'CNY'].map((currency) => ({ value: currency, label: currency }))} onChange={(pricing_currency) => onChange({ pricing_currency })}/></label>
      <div className="field-grid">{fields.map(([key, label]) => <label key={key} className="form-field">{label} / 1M tokens<InputNumber min={0} step={0.01} value={value[key]} placeholder={zh ? '未定价' : 'Unpriced'} onChange={(rate) => onChange({ [key]: rate })} style={{ width: '100%' }}/></label>)}</div>
    </>}
  </div>;
}
