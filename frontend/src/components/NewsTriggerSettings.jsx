import React from 'react';
import { Alert, Button, Card, InputNumber, Select, Space, Switch, Tag, Typography } from 'antd';

export default function NewsTriggerSettings({ rules = [], onChange, agents = [], providers = [], sources = [], status = [], zh }) {
  const update = (index, patch) => onChange(rules.map((rule, i) => i === index ? { ...rule, ...patch } : rule));
  const available = agents.filter((a) => a.mode !== 'SPOT_DCA' && !a.mcp_profile);
  const modelOptions = providers.filter((p) => (p.api_protocol || 'chat') === 'chat').map((p) => ({ value: p.provider_id, label: `${p.name || p.provider_id} · ${p.model}` }));
  return <Card className="panel-card" title={zh ? '消息触发 Agent' : 'News-triggered agents'}>
    <Alert type="info" showIcon title={zh ? '先匹配规则，再运行指定模型；默认关闭' : 'Match rules before running the selected model; disabled by default'} description={zh ? '不同筛选项之间为“且”，同一项内为“或”。只处理新鲜且未处理的新闻，合并一轮消息；首次启用或修改规则只建立基线。运行中、冷却期、每日限额、常规定时运行会抑制额外运行。不会额外触发固定定投任务。' : 'Filter groups use AND; values within a group use OR. Only fresh, unseen news triggers a batched run. Enabling/editing a rule establishes a baseline. Busy tasks, cooldowns, daily limits and regular runs suppress extra runs. Fixed DCA schedules are excluded.'} />
    {rules.map((rule, index) => {
      const state = status.find((s) => s.config_id === rule.config_id);
      return <Card size="small" key={rule.config_id} style={{ marginTop: 16 }} title={<Space><Switch checked={rule.enabled} onChange={(enabled) => update(index, { enabled })} />{agents.find((a) => a.config_id === rule.config_id)?.name || rule.config_id}</Space>} extra={<Button danger onClick={() => onChange(rules.filter((_, i) => i !== index))}>{zh ? '删除' : 'Remove'}</Button>}>
        <div className="settings-form-grid">
          <label>{zh ? '本次触发专用模型' : 'Model for triggered runs'}<Select showSearch optionFilterProp="label" value={rule.provider_id || undefined} options={modelOptions} onChange={(provider_id) => update(index, { provider_id })} /></label>
          <label>{zh ? '关键词（任意一个）' : 'Keywords (any)'}<Select mode="tags" tokenSeparators={[',', '，']} value={rule.keywords || []} onChange={(keywords) => update(index, { keywords })} placeholder="CPI, FOMC, ETF" /></label>
          <label>{zh ? '来源（空=全部）' : 'Sources (empty=all)'}<Select mode="multiple" value={rule.source_ids || []} options={sources.map((s) => ({ value: s.id, label: s.name }))} onChange={(source_ids) => update(index, { source_ids })} /></label>
          <label>{zh ? '类别（空=全部，可输入）' : 'Categories (empty=all)'}<Select mode="tags" value={rule.categories || []} options={['crypto', 'critical', 'exchange_announcement', 'macro_policy', 'macro_market', 'policy', 'geopolitical'].map((v) => ({ value: v, label: v }))} onChange={(categories) => update(index, { categories })} /></label>
          <label>{zh ? '最低相关度（留空不检查）' : 'Minimum score (optional)'}<InputNumber min={0} max={100} value={rule.min_score} onChange={(min_score) => update(index, { min_score })} /></label>
          <label>{zh ? '冷却时间（分钟）' : 'Cooldown (minutes)'}<InputNumber min={1} max={1440} value={(rule.cooldown_seconds ?? 1800) / 60} onChange={(v) => update(index, { cooldown_seconds: (v ?? 30) * 60 })} /></label>
          <label>{zh ? '每日次数上限（UTC 日）' : 'Daily cap (UTC day)'}<InputNumber min={1} max={100} value={rule.max_runs_per_day ?? 4} onChange={(v) => update(index, { max_runs_per_day: v ?? 4 })} /></label>
          <label>{zh ? '新闻最长时效（分钟）' : 'Max news age (minutes)'}<InputNumber min={1} max={60} value={(rule.max_age_seconds ?? 600) / 60} onChange={(v) => update(index, { max_age_seconds: (v ?? 10) * 60 })} /></label>
        </div>
        {state && <Space style={{ marginTop: 12 }}><Tag>{state.status || '—'}</Tag><Typography.Text type="secondary">{zh ? '已预留/执行次数' : 'Reserved/runs'}: {state.count || 0} · {state.day || '—'}</Typography.Text></Space>}
      </Card>;
    })}
    <Select style={{ width: '100%', marginTop: 16 }} showSearch optionFilterProp="label" value={undefined} placeholder={zh ? '选择任务，添加触发规则' : 'Select a task to add a rule'} options={available.filter((a) => !rules.some((r) => r.config_id === a.config_id)).map((a) => ({ value: a.config_id, label: `${a.name || a.config_id} · ${a.symbol}` }))} onChange={(config_id) => onChange([...rules, { config_id, enabled: false, provider_id: '', keywords: [], categories: [], source_ids: [], min_score: null, cooldown_seconds: 1800, max_runs_per_day: 4, max_age_seconds: 600 }])} />
    <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>{zh ? '在模型渠道中添加便宜的模型，再在这里选择。使用渠道实际提供的模型 ID；本次运行不会回退到任务原来的昂贵模型。任务原有交易权限和风控仍然有效，启用后可能执行真实交易。次数上限限制运行次数，不等于固定金额预算。' : 'Add a lower-cost provider with its actual model ID, then select it here. Triggered runs do not fall back to the original expensive model. Task permissions and risk limits remain active; enabled tasks may trade. The cap limits runs, not monetary cost.'}</Typography.Paragraph>
  </Card>;
}
