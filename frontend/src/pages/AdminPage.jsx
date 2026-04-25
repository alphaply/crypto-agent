import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { TextArea } = Input;
const { Title, Paragraph, Text } = Typography;

function buildBlankSecretMeta() {
  return { configured: false, masked_value: '', value: '', clear: false };
}

function buildBlankAgent(promptFiles = []) {
  const now = Date.now();
  return {
    config_id: `agent-${now}`,
    title: '',
    symbol: 'BTC/USDT',
    enabled: true,
    mode: 'STRATEGY',
    model: 'gpt-4o-mini',
    api_base: '',
    temperature: 0.3,
    prompt_file: promptFiles[0] || '',
    run_interval: 60,
    leverage: 10,
    exchange: 'binance',
    market_type: 'swap',
    dca_amount: 100,
    dca_freq: '1d',
    dca_time: '08:00',
    dca_weekday: 0,
    initial_cost: 0,
    initial_qty: 0,
    extra_body: {},
    summarizer: {
      model: '',
      api_base: '',
      temperature: 0.3,
    },
    secrets: {
      api_key: buildBlankSecretMeta(),
      secret: buildBlankSecretMeta(),
      passphrase: buildBlankSecretMeta(),
      binance_api_key: buildBlankSecretMeta(),
      binance_secret: buildBlankSecretMeta(),
      summarizer_api_key: buildBlankSecretMeta(),
    },
  };
}

function SecretField({ label, meta, onChange, onClear }) {
  const { t } = usePreferences();

  return (
    <div className="form-field">
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <label>{label}</label>
        <Tag color={meta?.configured ? 'green' : 'default'}>
          {meta?.configured ? t('configured') : t('notConfigured')}
        </Tag>
      </Space>
      <Space.Compact style={{ width: '100%' }}>
        <Input.Password
          value={meta?.value || ''}
          onChange={(event) => onChange(event.target.value)}
          placeholder={meta?.configured ? meta.masked_value || '******' : '******'}
        />
        <Button onClick={onClear}>{t('clear')}</Button>
      </Space.Compact>
    </div>
  );
}

export default function AdminPage() {
  const { t, locale } = usePreferences();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [payload, setPayload] = useState(null);
  const [selectedAgentId, setSelectedAgentId] = useState('');
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [promptContent, setPromptContent] = useState('');
  const [extraBodyDraft, setExtraBodyDraft] = useState('{}');
  const [extraBodyError, setExtraBodyError] = useState('');

  const loadAll = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await api.get('/config');
      setPayload(response.data);
      const firstAgentId = response.data.agents?.[0]?.config_id || '';
      setSelectedAgentId((prev) =>
        response.data.agents?.some((item) => item.config_id === prev) ? prev : firstAgentId,
      );
      const firstPrompt = response.data.prompts?.files?.[0] || '';
      setSelectedPrompt((prev) =>
        response.data.prompts?.files?.includes(prev) ? prev : firstPrompt,
      );
    } catch (err) {
      setError(err.message || 'Failed to load config');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  const selectedAgent = useMemo(
    () => payload?.agents?.find((item) => item.config_id === selectedAgentId) || null,
    [payload, selectedAgentId],
  );

  useEffect(() => {
    if (selectedAgent) {
      setExtraBodyDraft(JSON.stringify(selectedAgent.extra_body || {}, null, 2));
      setExtraBodyError('');
    }
  }, [selectedAgent]);

  useEffect(() => {
    let mounted = true;
    async function fetchPromptContent() {
      if (!selectedPrompt) {
        setPromptContent('');
        return;
      }
      try {
        const response = await api.get('/config/prompts/content', { params: { name: selectedPrompt } });
        if (mounted) {
          setPromptContent(response.data.content || '');
        }
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load prompt');
        }
      }
    }
    fetchPromptContent();
    return () => {
      mounted = false;
    };
  }, [selectedPrompt]);

  const updatePayload = (updater) => {
    setPayload((prev) => (prev ? updater(prev) : prev));
  };

  const updateGlobal = (field, value) => {
    updatePayload((prev) => ({
      ...prev,
      globals: {
        ...prev.globals,
        [field]: value,
      },
    }));
  };

  const updateGlobalSecret = (field, patch) => {
    updatePayload((prev) => ({
      ...prev,
      globals: {
        ...prev.globals,
        secrets: {
          ...prev.globals.secrets,
          [field]: {
            ...prev.globals.secrets[field],
            ...patch,
          },
        },
      },
    }));
  };

  const updateAgent = (configId, updater) => {
    updatePayload((prev) => ({
      ...prev,
      agents: prev.agents.map((agent) => (agent.config_id === configId ? updater(agent) : agent)),
    }));
  };

  const updateAgentField = (field, value) => {
    if (!selectedAgentId) {
      return;
    }
    updateAgent(selectedAgentId, (agent) => ({
      ...agent,
      [field]: value,
    }));
  };

  const updateAgentSecret = (field, patch) => {
    if (!selectedAgentId) {
      return;
    }
    updateAgent(selectedAgentId, (agent) => ({
      ...agent,
      secrets: {
        ...agent.secrets,
        [field]: {
          ...agent.secrets[field],
          ...patch,
        },
      },
    }));
  };

  const updateSummarizerField = (field, value) => {
    if (!selectedAgentId) {
      return;
    }
    updateAgent(selectedAgentId, (agent) => ({
      ...agent,
      summarizer: {
        ...(agent.summarizer || {}),
        [field]: value,
      },
    }));
  };

  const addAgent = () => {
    const nextAgent = buildBlankAgent(payload?.prompts?.files || []);
    updatePayload((prev) => ({
      ...prev,
      agents: [...prev.agents, nextAgent],
    }));
    setSelectedAgentId(nextAgent.config_id);
  };

  const duplicateAgent = () => {
    if (!selectedAgent) {
      return;
    }
    const now = Date.now();
    const clone = {
      ...selectedAgent,
      config_id: `${selectedAgent.config_id}-copy-${now}`,
      title: selectedAgent.title ? `${selectedAgent.title} Copy` : '',
      secrets: Object.fromEntries(
        Object.entries(selectedAgent.secrets || {}).map(([key]) => [key, buildBlankSecretMeta()]),
      ),
    };
    updatePayload((prev) => ({
      ...prev,
      agents: [...prev.agents, clone],
    }));
    setSelectedAgentId(clone.config_id);
  };

  const removeAgent = () => {
    if (!selectedAgentId) {
      return;
    }
    const nextId = payload?.agents?.find((item) => item.config_id !== selectedAgentId)?.config_id || '';
    updatePayload((prev) => ({
      ...prev,
      agents: prev.agents.filter((item) => item.config_id !== selectedAgentId),
    }));
    setSelectedAgentId(nextId);
  };

  const deleteAgentData = async () => {
    if (!selectedAgentId || !window.confirm('Delete this config and linked data?')) {
      return;
    }
    await api.delete(`/config/${selectedAgentId}`);
    await loadAll();
  };

  const handleExtraBodyChange = (value) => {
    setExtraBodyDraft(value);
    try {
      const parsed = value.trim() ? JSON.parse(value) : {};
      setExtraBodyError('');
      updateAgentField('extra_body', parsed);
    } catch (_) {
      setExtraBodyError('Invalid JSON');
    }
  };

  const saveConfig = async () => {
    if (!payload) {
      return;
    }
    setSaving(true);
    setError('');
    try {
      await api.put('/config', {
        globals: payload.globals,
        agents: payload.agents,
      });
      await loadAll();
    } catch (err) {
      setError(err.message || 'Failed to save config');
    } finally {
      setSaving(false);
    }
  };

  const savePrompt = async () => {
    if (!selectedPrompt) {
      return;
    }
    await api.put('/config/prompts', { name: selectedPrompt, content: promptContent });
    await loadAll();
  };

  const deletePrompt = async () => {
    if (!selectedPrompt) {
      return;
    }
    await api.delete('/config/prompts', { data: { name: selectedPrompt } });
    setSelectedPrompt('');
    setPromptContent('');
    await loadAll();
  };

  const addPricing = async (values) => {
    await api.post('/stats/pricing', values);
    await loadAll();
  };

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="hero-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Title level={2} style={{ margin: 0 }}>
              {t('config')}
            </Title>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              Database-backed runtime config with encrypted secrets and form-first editing.
            </Paragraph>
          </div>
          <Button type="primary" onClick={saveConfig} loading={saving}>
            {t('saveConfig')}
          </Button>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}
      <Alert
        type="info"
        showIcon
        message={locale === 'zh' ? '控制台登录密码不在这里修改' : 'Console login password is not edited here'}
        description={
          locale === 'zh'
            ? '请编辑 .env 中的 CHAT_PASSWORD 或 ADMIN_PASSWORD，然后重启后端。如果还想让当前已登录用户立即重新登录，再一并更换 JWT_SECRET。'
            : 'Update CHAT_PASSWORD or ADMIN_PASSWORD in .env, then restart the backend. Rotate JWT_SECRET as well if you want current sessions to be invalidated immediately.'
        }
      />

      {loading ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : payload ? (
        <Tabs
          items={[
            {
              key: 'runtime',
              label: t('runtimeConfig'),
              children: (
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <Card className="panel-card" title={t('globals')}>
                    <div className="field-grid">
                      <div className="form-field">
                        <label>Leverage</label>
                        <InputNumber min={1} value={payload.globals.leverage} onChange={(value) => updateGlobal('leverage', value ?? 1)} />
                      </div>
                      <div className="form-field">
                        <label>Scheduler</label>
                        <Switch checked={payload.globals.enable_scheduler} onChange={(checked) => updateGlobal('enable_scheduler', checked)} />
                      </div>
                      <div className="form-field">
                        <label>Trading Mode</label>
                        <Select value={payload.globals.trading_mode} options={(payload.options?.modes || []).map((value) => ({ label: value, value }))} onChange={(value) => updateGlobal('trading_mode', value)} />
                      </div>
                      <div className="form-field">
                        <label>LangChain Tracing</label>
                        <Switch checked={payload.globals.langchain_tracing} onChange={(checked) => updateGlobal('langchain_tracing', checked)} />
                      </div>
                      <div className="form-field">
                        <label>LangChain Project</label>
                        <Input value={payload.globals.langchain_project} onChange={(event) => updateGlobal('langchain_project', event.target.value)} />
                      </div>
                      <div className="form-field">
                        <label>LLM Timeout</label>
                        <InputNumber min={10} value={payload.globals.llm_timeout_seconds} onChange={(value) => updateGlobal('llm_timeout_seconds', value ?? 120)} />
                      </div>
                      <div className="form-field">
                        <label>LLM Retries</label>
                        <InputNumber min={0} value={payload.globals.llm_max_retries} onChange={(value) => updateGlobal('llm_max_retries', value ?? 0)} />
                      </div>
                      <div className="form-field">
                        <label>Summarizer Model</label>
                        <Input value={payload.globals.global_summarizer_model} onChange={(event) => updateGlobal('global_summarizer_model', event.target.value)} />
                      </div>
                      <div className="form-field field-span-2">
                        <label>Summarizer API Base</label>
                        <Input value={payload.globals.global_summarizer_api_base} onChange={(event) => updateGlobal('global_summarizer_api_base', event.target.value)} />
                      </div>
                    </div>
                    <div className="field-grid">
                      <SecretField label="Global Binance API Key" meta={payload.globals.secrets.global_binance_api_key} onChange={(value) => updateGlobalSecret('global_binance_api_key', { value, clear: false })} onClear={() => updateGlobalSecret('global_binance_api_key', { value: '', clear: true })} />
                      <SecretField label="Global Binance Secret" meta={payload.globals.secrets.global_binance_secret} onChange={(value) => updateGlobalSecret('global_binance_secret', { value, clear: false })} onClear={() => updateGlobalSecret('global_binance_secret', { value: '', clear: true })} />
                      <SecretField label="OKX API Key" meta={payload.globals.secrets.global_okx_api_key} onChange={(value) => updateGlobalSecret('global_okx_api_key', { value, clear: false })} onClear={() => updateGlobalSecret('global_okx_api_key', { value: '', clear: true })} />
                      <SecretField label="OKX Secret" meta={payload.globals.secrets.global_okx_secret} onChange={(value) => updateGlobalSecret('global_okx_secret', { value, clear: false })} onClear={() => updateGlobalSecret('global_okx_secret', { value: '', clear: true })} />
                      <SecretField label="OKX Passphrase" meta={payload.globals.secrets.global_okx_passphrase} onChange={(value) => updateGlobalSecret('global_okx_passphrase', { value, clear: false })} onClear={() => updateGlobalSecret('global_okx_passphrase', { value: '', clear: true })} />
                      <SecretField label="LangChain API Key" meta={payload.globals.secrets.langchain_api_key} onChange={(value) => updateGlobalSecret('langchain_api_key', { value, clear: false })} onClear={() => updateGlobalSecret('langchain_api_key', { value: '', clear: true })} />
                      <SecretField label="Global Summarizer API Key" meta={payload.globals.secrets.global_summarizer_api_key} onChange={(value) => updateGlobalSecret('global_summarizer_api_key', { value, clear: false })} onClear={() => updateGlobalSecret('global_summarizer_api_key', { value: '', clear: true })} />
                    </div>
                  </Card>

                  <div className="config-editor">
                    <Card
                      className="panel-card config-sidebar"
                      title={t('agentConfigs')}
                      extra={
                        <Space>
                          <Button onClick={duplicateAgent} disabled={!selectedAgent}>
                            {t('duplicateAgent')}
                          </Button>
                          <Button type="primary" onClick={addAgent}>
                            {t('addAgent')}
                          </Button>
                        </Space>
                      }
                    >
                      <List
                        dataSource={payload.agents}
                        locale={{ emptyText: <Empty description={t('emptyConfig')} /> }}
                        renderItem={(item) => (
                          <List.Item
                            className={`session-row ${item.config_id === selectedAgentId ? 'active' : ''}`}
                            onClick={() => setSelectedAgentId(item.config_id)}
                          >
                            <List.Item.Meta title={item.config_id} description={`${item.symbol} / ${item.mode}`} />
                          </List.Item>
                        )}
                      />
                    </Card>

                    <Card
                      className="panel-card"
                      title={selectedAgent ? selectedAgent.config_id : t('agentConfigs')}
                      extra={
                        selectedAgent ? (
                          <Space>
                            <Button onClick={removeAgent}>{t('removeAgent')}</Button>
                            <Button danger onClick={deleteAgentData}>
                              {t('deleteAgentData')}
                            </Button>
                          </Space>
                        ) : null
                      }
                    >
                      {selectedAgent ? (
                        <Collapse
                          defaultActiveKey={['basic', 'schedule', 'model']}
                          items={[
                            {
                              key: 'basic',
                              label: t('basicSettings'),
                              children: (
                                <div className="field-grid">
                                  <div className="form-field">
                                    <label>Config ID</label>
                                    <Input
                                      value={selectedAgent.config_id}
                                      onChange={(event) => {
                                        const nextId = event.target.value;
                                        updateAgentField('config_id', nextId);
                                        setSelectedAgentId(nextId);
                                      }}
                                    />
                                  </div>
                                  <div className="form-field">
                                    <label>Title</label>
                                    <Input value={selectedAgent.title || ''} onChange={(event) => updateAgentField('title', event.target.value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>{t('symbol')}</label>
                                    <Input value={selectedAgent.symbol} onChange={(event) => updateAgentField('symbol', event.target.value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Enabled</label>
                                    <Switch checked={selectedAgent.enabled} onChange={(checked) => updateAgentField('enabled', checked)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Mode</label>
                                    <Select value={selectedAgent.mode} options={(payload.options?.modes || []).map((value) => ({ label: value, value }))} onChange={(value) => updateAgentField('mode', value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Prompt File</label>
                                    <Select value={selectedAgent.prompt_file || undefined} options={(payload.options?.prompt_files || []).map((value) => ({ label: value, value }))} onChange={(value) => updateAgentField('prompt_file', value)} />
                                  </div>
                                </div>
                              ),
                            },
                            {
                              key: 'schedule',
                              label: t('scheduleSettings'),
                              children: (
                                <div className="field-grid">
                                  <div className="form-field">
                                    <label>Leverage</label>
                                    <InputNumber min={1} value={selectedAgent.leverage ?? 1} onChange={(value) => updateAgentField('leverage', value ?? 1)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Run Interval</label>
                                    <InputNumber min={15} value={selectedAgent.run_interval ?? 60} onChange={(value) => updateAgentField('run_interval', value ?? 60)} />
                                  </div>
                                  <div className="form-field">
                                    <label>DCA Amount</label>
                                    <InputNumber min={0} value={selectedAgent.dca_amount ?? 0} onChange={(value) => updateAgentField('dca_amount', value ?? 0)} />
                                  </div>
                                  <div className="form-field">
                                    <label>DCA Freq</label>
                                    <Select value={selectedAgent.dca_freq || undefined} options={(payload.options?.dca_freqs || []).map((value) => ({ label: value, value }))} onChange={(value) => updateAgentField('dca_freq', value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>DCA Time</label>
                                    <Input value={selectedAgent.dca_time || ''} onChange={(event) => updateAgentField('dca_time', event.target.value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>DCA Weekday</label>
                                    <InputNumber min={0} max={6} value={selectedAgent.dca_weekday ?? 0} onChange={(value) => updateAgentField('dca_weekday', value ?? 0)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Initial Cost</label>
                                    <InputNumber min={0} value={selectedAgent.initial_cost ?? 0} onChange={(value) => updateAgentField('initial_cost', value ?? 0)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Initial Qty</label>
                                    <InputNumber min={0} value={selectedAgent.initial_qty ?? 0} onChange={(value) => updateAgentField('initial_qty', value ?? 0)} />
                                  </div>
                                </div>
                              ),
                            },
                            {
                              key: 'model',
                              label: t('modelSettings'),
                              children: (
                                <div className="field-grid">
                                  <div className="form-field">
                                    <label>Model</label>
                                    <Input value={selectedAgent.model} onChange={(event) => updateAgentField('model', event.target.value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Temperature</label>
                                    <InputNumber min={0} max={2} step={0.1} value={selectedAgent.temperature ?? 0} onChange={(value) => updateAgentField('temperature', value ?? 0)} />
                                  </div>
                                  <div className="form-field field-span-2">
                                    <label>API Base</label>
                                    <Input value={selectedAgent.api_base || ''} onChange={(event) => updateAgentField('api_base', event.target.value)} />
                                  </div>
                                  <SecretField label="LLM API Key" meta={selectedAgent.secrets.api_key} onChange={(value) => updateAgentSecret('api_key', { value, clear: false })} onClear={() => updateAgentSecret('api_key', { value: '', clear: true })} />
                                </div>
                              ),
                            },
                            {
                              key: 'exchange',
                              label: t('exchangeSettings'),
                              children: (
                                <div className="field-grid">
                                  <div className="form-field">
                                    <label>Exchange</label>
                                    <Select value={selectedAgent.exchange || 'binance'} options={(payload.options?.exchanges || []).map((value) => ({ label: value, value }))} onChange={(value) => updateAgentField('exchange', value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Market Type</label>
                                    <Select value={selectedAgent.market_type || 'swap'} options={(payload.options?.market_types || []).map((value) => ({ label: value, value }))} onChange={(value) => updateAgentField('market_type', value)} />
                                  </div>
                                  <SecretField label="Agent Secret" meta={selectedAgent.secrets.secret} onChange={(value) => updateAgentSecret('secret', { value, clear: false })} onClear={() => updateAgentSecret('secret', { value: '', clear: true })} />
                                  <SecretField label="Passphrase" meta={selectedAgent.secrets.passphrase} onChange={(value) => updateAgentSecret('passphrase', { value, clear: false })} onClear={() => updateAgentSecret('passphrase', { value: '', clear: true })} />
                                  <SecretField label="Binance API Key" meta={selectedAgent.secrets.binance_api_key} onChange={(value) => updateAgentSecret('binance_api_key', { value, clear: false })} onClear={() => updateAgentSecret('binance_api_key', { value: '', clear: true })} />
                                  <SecretField label="Binance Secret" meta={selectedAgent.secrets.binance_secret} onChange={(value) => updateAgentSecret('binance_secret', { value, clear: false })} onClear={() => updateAgentSecret('binance_secret', { value: '', clear: true })} />
                                </div>
                              ),
                            },
                            {
                              key: 'summarizer',
                              label: t('summarizerSettings'),
                              children: (
                                <div className="field-grid">
                                  <div className="form-field">
                                    <label>Summarizer Model</label>
                                    <Input value={selectedAgent.summarizer?.model || ''} onChange={(event) => updateSummarizerField('model', event.target.value)} />
                                  </div>
                                  <div className="form-field">
                                    <label>Temperature</label>
                                    <InputNumber min={0} max={2} step={0.1} value={selectedAgent.summarizer?.temperature ?? 0.3} onChange={(value) => updateSummarizerField('temperature', value ?? 0.3)} />
                                  </div>
                                  <div className="form-field field-span-2">
                                    <label>API Base</label>
                                    <Input value={selectedAgent.summarizer?.api_base || ''} onChange={(event) => updateSummarizerField('api_base', event.target.value)} />
                                  </div>
                                  <SecretField label="Summarizer API Key" meta={selectedAgent.secrets.summarizer_api_key} onChange={(value) => updateAgentSecret('summarizer_api_key', { value, clear: false })} onClear={() => updateAgentSecret('summarizer_api_key', { value: '', clear: true })} />
                                </div>
                              ),
                            },
                            {
                              key: 'advanced',
                              label: t('advancedSettings'),
                              children: (
                                <div className="form-field">
                                  <label>extra_body (JSON)</label>
                                  <TextArea rows={10} value={extraBodyDraft} onChange={(event) => handleExtraBodyChange(event.target.value)} />
                                  {extraBodyError ? <Text type="danger">{extraBodyError}</Text> : null}
                                </div>
                              ),
                            },
                          ]}
                        />
                      ) : (
                        <Empty description={t('emptyConfig')} />
                      )}
                    </Card>
                  </div>
                </Space>
              ),
            },
            {
              key: 'prompts',
              label: t('prompts'),
              children: (
                <div className="config-editor">
                  <Card className="panel-card config-sidebar" title={t('promptEditor')}>
                    <List
                      dataSource={payload.prompts?.files || []}
                      renderItem={(item) => (
                        <List.Item className={`session-row ${item === selectedPrompt ? 'active' : ''}`} onClick={() => setSelectedPrompt(item)}>
                          {item}
                        </List.Item>
                      )}
                    />
                  </Card>
                  <Card className="panel-card" title={selectedPrompt || t('promptEditor')}>
                    <Space direction="vertical" style={{ width: '100%' }} size="middle">
                      <TextArea rows={18} value={promptContent} onChange={(event) => setPromptContent(event.target.value)} />
                      <Space>
                        <Button type="primary" onClick={savePrompt} disabled={!selectedPrompt}>
                          {t('savePrompt')}
                        </Button>
                        <Button danger onClick={deletePrompt} disabled={!selectedPrompt}>
                          {t('deletePrompt')}
                        </Button>
                      </Space>
                    </Space>
                  </Card>
                </div>
              ),
            },
            {
              key: 'pricing',
              label: t('modelPricing'),
              children: (
                <Card className="panel-card" title={t('pricingEditor')}>
                  <Space direction="vertical" size="large" style={{ width: '100%' }}>
                    <Form layout="inline" onFinish={addPricing}>
                      <Form.Item name="model" rules={[{ required: true, message: 'model is required' }]}>
                        <Input placeholder="model" />
                      </Form.Item>
                      <Form.Item name="input_price">
                        <Input placeholder="input price / 1M" />
                      </Form.Item>
                      <Form.Item name="output_price">
                        <Input placeholder="output price / 1M" />
                      </Form.Item>
                      <Button htmlType="submit" type="primary">
                        {t('save')}
                      </Button>
                    </Form>
                    <Table
                      rowKey={(row) => row.model}
                      dataSource={payload.pricing || []}
                      scroll={{ x: 760 }}
                      columns={[
                        { title: 'Model', dataIndex: 'model' },
                        { title: 'Input / M', dataIndex: 'input_price_per_m' },
                        { title: 'Output / M', dataIndex: 'output_price_per_m' },
                        { title: 'Currency', dataIndex: 'currency' },
                      ]}
                    />
                  </Space>
                </Card>
              ),
            },
          ]}
        />
      ) : null}
    </Space>
  );
}
