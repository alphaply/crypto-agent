import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Grid,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, HolderOutlined, FileTextOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import { DailySummaryPanel, ShortMemoryPanel } from './DashboardPage';

const { TextArea } = Input;
const { Title, Paragraph, Text } = Typography;
const { useBreakpoint } = Grid;
const ADMIN_TAB_STORAGE_KEY = 'crypto-agent-admin-active-tab';
const ADMIN_TAB_KEYS = ['runtime', 'tasks', 'providers', 'exchanges', 'memory', 'prompts', 'importexport'];

const DEFAULT_STRATEGY_PROMPT = '请把以下单轮交易分析压缩成一段中文策略记忆，150字以内。保留趋势判断、关键价位、风险点、持仓/挂单意图和下一步动作。只输出总结文本。\n\n内容：\n{content}';
const DEFAULT_DAILY_PROMPT = '请把以下一整天的交易推理压缩成一段中文日内记忆，300字以内。保留趋势演变、关键价位、决策变化、执行动作和风险结论。只输出总结文本。\n\n内容：\n{content}';
const DEFAULT_SHORT_MEMORY_PROMPT = '请把以下最近4小时的市场与持仓信息整理成中文短期记忆，300字以内。包含市场状态、近期决策、持仓/挂单变化、已实现盈亏和风险提醒。只输出总结文本。\n\n内容：\n{content}';

const DEFAULT_PROMPT_FILE_CONTENT = `Role: Crypto trading strategy analyst
Time: {current_time}
Next Run: {next_run_time}
Target: {symbol}
Leverage: {leverage}x
Current Price: {current_price}
Available Balance: {balance:.2f} USDT
DCA Budget: {dca_budget}
DCA Period: {dca_period_text}
15m ATR: {atr_15m:.2f}

Positions:
{positions_text}

Open Orders:
{orders_text}

Market Data:
{formatted_market_data}

Short Memory:
{short_memory_text}

Daily History:
{history_text}

Write a concise Markdown decision. If action is needed, call the matching trading tool after the analysis.`;

const MARKET_TIMEFRAME_OPTIONS = ['15m', '30m', '1h', '4h', '1d', '1w', '1M'];

const LLM_PROVIDER_PRESETS = [
  { key: 'openai', label: 'OpenAI / Codex', api_base: 'https://api.openai.com/v1', model: 'gpt-5.6', compatibility_mode: 'openai', thinking_enabled: true, reasoning_effort: 'medium' },
  { key: 'deepseek', label: 'DeepSeek', api_base: 'https://api.deepseek.com', model: 'deepseek-v4-pro', compatibility_mode: 'deepseek', thinking_enabled: true, reasoning_effort: 'high' },
  { key: 'bai-claude', label: 'BAI · Claude', api_base: 'https://api.bankofai.io/v1', model: 'claude-sonnet-5', compatibility_mode: 'openai', thinking_enabled: true, reasoning_effort: 'high' },
];

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
    model: '',
    api_base: '',
    temperature: 0.3,
    prompt_file: promptFiles[0] || '',
    market_timeframes: [...MARKET_TIMEFRAME_OPTIONS],
    run_interval: 60,
    leverage: 10,
    exchange: '',
    market_type: 'swap',
    dca_amount: 100,
    dca_freq: '1d',
    dca_time: '08:00',
    dca_weekday: 0,
    initial_cost: 0,
    initial_qty: 0,
    manual_avg_cost: 0,
    extra_body: {},
    llm_provider_id: '',
    summarizer_provider_id: '',
    exchange_profile_id: '',
    strategy_prompt: DEFAULT_STRATEGY_PROMPT,
    daily_prompt: DEFAULT_DAILY_PROMPT,
    short_memory_prompt: DEFAULT_SHORT_MEMORY_PROMPT,
    system_prompt_role: 'system',
    summarizer: {
      model: '',
      api_base: '',
      temperature: 0.3,
      strategy_prompt: DEFAULT_STRATEGY_PROMPT,
      daily_prompt: DEFAULT_DAILY_PROMPT,
      short_memory_prompt: DEFAULT_SHORT_MEMORY_PROMPT,
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

function buildBlankProvider() {
  return {
    provider_id: `llm-${Date.now()}`,
    name: '',
    model: '',
    api_base: '',
    temperature: 0.5,
    input_price_per_m: 0,
    output_price_per_m: 0,
    pricing_currency: 'USD',
    extra_body: {},
    compatibility_mode: 'auto',
    thinking_enabled: null,
    reasoning_effort: '',
    system_prompt_role: 'system',
    secrets: { api_key: buildBlankSecretMeta() },
  };
}

function normalizeApiBase(value) {
  return String(value || '').trim().replace(/\/+$/, '');
}

function apiBaseGroupKey(value) {
  return normalizeApiBase(value).toLowerCase() || '__unassigned__';
}

function apiBaseDisplayName(value, locale = 'en') {
  const normalized = normalizeApiBase(value);
  if (!normalized) return locale === 'zh' ? '未设置 API Base' : 'No API Base configured';
  try {
    const url = new URL(normalized);
    return url.host.replace(/^api\./i, '') || normalized;
  } catch {
    return normalized;
  }
}

function providerHasApiKey(provider) {
  const apiKey = provider?.secrets?.api_key || {};
  return Boolean(apiKey.configured || apiKey.value);
}

function buildBlankProfile() {
  return {
    profile_id: `exchange-${Date.now()}`,
    name: '',
    exchange: 'binance',
    market_type: 'swap',
    secrets: {
      api_key: buildBlankSecretMeta(),
      secret: buildBlankSecretMeta(),
      passphrase: buildBlankSecretMeta(),
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

function ProviderSelect({ providers, value, onChange, allowEmpty, emptyLabel }) {
  const { t } = usePreferences();
  const options = (providers || []).map((p) => ({
    label: `${p.name || p.provider_id} (${p.model || '-'})`,
    value: p.provider_id,
  }));
  if (allowEmpty) {
    options.unshift({ label: emptyLabel || t('noProvider'), value: '' });
  }
  return <Select value={value || ''} options={options} onChange={onChange} style={{ width: '100%' }} />;
}

function ProfileSelect({ profiles, value, onChange, allowEmpty, emptyLabel }) {
  const { t } = usePreferences();
  const options = (profiles || []).map((p) => ({
    label: `${p.name || p.profile_id} (${p.exchange}/${p.market_type})`,
    value: p.profile_id,
  }));
  if (allowEmpty) {
    options.unshift({ label: emptyLabel || t('noProfile'), value: '' });
  }
  return <Select value={value || ''} options={options} onChange={onChange} style={{ width: '100%' }} />;
}

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function highlightPrompt(value) {
  const escaped = escapeHtml(value || '');
  return escaped.replace(/(\{[a-zA-Z_][a-zA-Z0-9_]*(?::[^}]*)?\})/g, '<mark class="prompt-placeholder">$1</mark>');
}

function PromptCodeEditor({ value, onChange, placeholder, height = 420 }) {
  const textRef = useRef(null);
  const gutterRef = useRef(null);
  const highlightRef = useRef(null);
  const safeValue = value || '';
  const lineCount = safeValue.split('\n').length;
  const lineNumbers = Array.from({ length: lineCount }, (_, index) => index + 1).join('\n');
  const charCount = safeValue.length;

  const syncScroll = () => {
    if (!textRef.current) return;
    const { scrollTop, scrollLeft } = textRef.current;
    if (gutterRef.current) gutterRef.current.scrollTop = scrollTop;
    if (highlightRef.current) {
      highlightRef.current.scrollTop = scrollTop;
      highlightRef.current.scrollLeft = scrollLeft;
    }
  };

  return (
    <div className="prompt-editor-wrapper">
      <div className="prompt-editor" style={{ height }}>
        <div className="prompt-editor__gutter" ref={gutterRef}>
          <pre className="prompt-editor__line-numbers">{lineNumbers}</pre>
        </div>
        <div className="prompt-editor__body">
          <pre
            className="prompt-editor__highlight"
            ref={highlightRef}
            dangerouslySetInnerHTML={{ __html: `${highlightPrompt(safeValue)}\n` }}
          />
          <textarea
            ref={textRef}
            className="prompt-editor__textarea"
            value={safeValue}
            onChange={(event) => onChange(event.target.value)}
            onScroll={syncScroll}
            placeholder={placeholder}
            spellCheck={false}
          />
        </div>
      </div>
      <div className="prompt-editor-meta">
        <Text type="secondary">{lineCount} {lineCount === 1 ? 'line' : 'lines'}</Text>
        <Text type="secondary">{charCount} chars</Text>
      </div>
    </div>
  );
}

function PromptVarHints({ content, vars, locale }) {
  const usedVars = new Set();
  const varPattern = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
  let match;
  const safeContent = content || '';
  while ((match = varPattern.exec(safeContent)) !== null) {
    usedVars.add(match[1]);
  }

  const copyVar = (name) => {
    navigator.clipboard?.writeText(`{${name}}`);
  };

  return (
    <div className="prompt-var-hints">
      <span className="prompt-var-hints-label">{locale === 'zh' ? '变量' : 'Variables'}:</span>
      {vars.map((v) => {
        const used = usedVars.has(v);
        return (
          <button
            key={v}
            type="button"
            className={`prompt-var-tag ${used ? 'prompt-var-tag--used' : 'prompt-var-tag--unused'}`}
            onClick={() => copyVar(v)}
            title={locale === 'zh' ? `点击复制 {${v}}` : `Click to copy {${v}}`}
          >
            {used ? '✓' : '○'} {v}
          </button>
        );
      })}
    </div>
  );
}

const AGENT_PROMPT_VARS = [
  'current_time', 'symbol', 'leverage', 'current_price', 'atr_15m',
  'balance', 'positions_text', 'orders_text', 'formatted_market_data',
  'short_memory_text', 'history_text', 'next_run_time',
  'dca_period_text', 'dca_budget',
];

const SUMMARIZER_PROMPT_VARS = ['content'];

function PromptEditor({ value, onChange, placeholder }) {
  if (typeof window !== 'undefined') {
    return <PromptCodeEditor value={value} onChange={onChange} placeholder={placeholder} height={260} />;
  }

  const lineCount = (value || '').split('\n').length;
  const charCount = (value || '').length;
  return (
    <div className="prompt-editor-wrapper">
      <TextArea
        rows={8}
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        style={{ fontFamily: 'monospace', fontSize: 13 }}
      />
      <div className="prompt-editor-meta">
        <Text type="secondary">{lineCount} 行</Text>
        <Text type="secondary">{charCount} 字符</Text>
      </div>
    </div>
  );
}

export default function AdminPage() {
  const { t, locale } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const [activeAdminTab, setActiveAdminTab] = useState(() => {
    if (typeof window === 'undefined') return 'runtime';
    const saved = window.localStorage.getItem(ADMIN_TAB_STORAGE_KEY);
    return ADMIN_TAB_KEYS.includes(saved) ? saved : 'runtime';
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [payload, setPayload] = useState(null);
  const [saveState, setSaveState] = useState('idle');
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [promptContent, setPromptContent] = useState('');
  const [newPromptName, setNewPromptName] = useState('');

  // Task (agent) drawer state
  const [taskDrawerOpen, setTaskDrawerOpen] = useState(false);
  const [editingTask, setEditingTask] = useState(null);
  const [editingTaskId, setEditingTaskId] = useState(null);
  const [draggingTaskId, setDraggingTaskId] = useState('');
  const autosaveTimerRef = useRef(null);

  // Provider/Profile drawer state
  const [providerDrawerOpen, setProviderDrawerOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
  const [providerQuery, setProviderQuery] = useState('');
  const [providerApiBaseFilter, setProviderApiBaseFilter] = useState('__all__');
  const [providerKeyFilter, setProviderKeyFilter] = useState('__all__');
  const [providerThinkingFilter, setProviderThinkingFilter] = useState('__all__');
  const [profileDrawerOpen, setProfileDrawerOpen] = useState(false);
  const [editingProfile, setEditingProfile] = useState(null);

  // Import state
  const [importing, setImporting] = useState(false);
  const [importWriteEnv, setImportWriteEnv] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await api.get('/config');
      setPayload(response.data);
      setSaveState('idle');
      const firstPrompt = response.data.prompts?.files?.[0] || '';
      setSelectedPrompt((prev) =>
        response.data.prompts?.files?.includes(prev) ? prev : firstPrompt,
      );
    } catch (err) {
      setError(err.message || 'Failed to load config');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadAll();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadAll]);

  useEffect(() => {
    window.localStorage.setItem(
      ADMIN_TAB_STORAGE_KEY,
      ADMIN_TAB_KEYS.includes(activeAdminTab) ? activeAdminTab : 'runtime',
    );
  }, [activeAdminTab]);

  useEffect(() => {
    let mounted = true;
    async function fetchPromptContent() {
      if (!selectedPrompt) { setPromptContent(''); return; }
      try {
        const response = await api.get('/config/prompts/content', { params: { name: selectedPrompt } });
        if (mounted) setPromptContent(response.data.content || '');
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load prompt');
      }
    }
    fetchPromptContent();
    return () => { mounted = false; };
  }, [selectedPrompt]);

  const updatePayload = (updater) => {
    setSaveState('unsaved');
    setPayload((prev) => (prev ? updater(prev) : prev));
  };

  const updateGlobal = (field, value) => {
    updatePayload((prev) => ({ ...prev, globals: { ...prev.globals, [field]: value } }));
  };

  const updateGlobalSecret = (field, patch) => {
    updatePayload((prev) => ({
      ...prev,
      globals: { ...prev.globals, secrets: { ...prev.globals.secrets, [field]: { ...prev.globals.secrets[field], ...patch } } },
    }));
  };

  const persistConfig = useCallback(async (targetPayload, reload = false) => {
    if (!targetPayload) return;
    setSaving(true);
    setError('');
    setSaveState('saving');
    try {
      await api.put('/config', {
        globals: targetPayload.globals,
        agents: targetPayload.agents,
        llm_providers: (targetPayload.llm_providers || []).map((p) => {
          const { secrets, ...rest } = p;
          return { ...rest, secrets: secrets || {} };
        }),
        exchange_profiles: (targetPayload.exchange_profiles || []).map((p) => {
          const { secrets, ...rest } = p;
          return { ...rest, secrets: secrets || {} };
        }),
      });
      setSaveState('saved');
      if (reload) await loadAll();
    } catch (err) {
      setError(err.message || 'Failed to save config');
      setSaveState('failed');
    } finally {
      setSaving(false);
    }
  }, [loadAll]);

  const saveConfig = async () => {
    if (autosaveTimerRef.current) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    await persistConfig(payload, true);
  };

  useEffect(() => {
    if (!payload || loading || saveState !== 'unsaved') return undefined;
    if (autosaveTimerRef.current) window.clearTimeout(autosaveTimerRef.current);
    const snapshot = payload;
    autosaveTimerRef.current = window.setTimeout(() => {
      persistConfig(snapshot, false);
    }, 800);
    return () => {
      if (autosaveTimerRef.current) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [payload, loading, saveState, persistConfig]);

  // --- Task (Agent) CRUD ---
  const openAddTask = () => {
    const blank = buildBlankAgent(payload?.prompts?.files || []);
    setEditingTask(blank);
    setEditingTaskId(null);
    setTaskDrawerOpen(true);
  };

  const openEditTask = (agent) => {
    const initialQty = Number(agent.initial_qty || 0);
    const manualAvg = Number(agent.manual_avg_cost || 0);
    setEditingTask({
      ...agent,
      manual_avg_cost: manualAvg || (initialQty > 0 ? Number(agent.initial_cost || 0) / initialQty : 0),
    });
    setEditingTaskId(agent.config_id);
    setTaskDrawerOpen(true);
  };

  const normalizeTaskForSave = (task) => {
    const next = { ...task };
    if (String(next.mode || '').toUpperCase() === 'SPOT_DCA') {
      const qty = Number(next.initial_qty || 0);
      const avg = Number(next.manual_avg_cost || 0);
      if (qty > 0 && avg > 0) {
        next.initial_cost = Number((qty * avg).toFixed(8));
      }
    }
    return next;
  };

  const saveTask = () => {
    if (!editingTask) return;
    const taskToSave = normalizeTaskForSave(editingTask);
    const required = [
      ['config_id', 'Config ID'],
      ['symbol', t('symbol')],
      ['mode', 'Mode'],
      ['prompt_file', 'Prompt File'],
      ['llm_provider_id', t('llmProviders')],
    ];
    const missing = required
      .filter(([field]) => !String(taskToSave[field] || '').trim())
      .map(([, label]) => label);
    if (missing.length) {
      message.error(`${locale === 'zh' ? '请先填写必填项' : 'Required fields'}: ${missing.join(', ')}`);
      return;
    }
    const duplicate = (payload?.agents || []).some(
      (agent) => agent.config_id === taskToSave.config_id && agent.config_id !== editingTaskId,
    );
    if (duplicate) {
      message.error('Config ID already exists');
      return;
    }
    updatePayload((prev) => {
      const agents = [...(prev.agents || [])];
      if (editingTaskId) {
        const idx = agents.findIndex((a) => a.config_id === editingTaskId);
        if (idx >= 0) agents[idx] = taskToSave;
      } else {
        agents.push(taskToSave);
      }
      return { ...prev, agents };
    });
    setTaskDrawerOpen(false);
    setEditingTask(null);
    setEditingTaskId(null);
  };

  const duplicateTask = (agent) => {
    const now = Date.now();
    const clone = {
      ...agent,
      config_id: `${agent.config_id}-copy-${now}`,
      title: agent.title ? `${agent.title} Copy` : '',
      secrets: Object.fromEntries(Object.entries(agent.secrets || {}).map(([key]) => [key, buildBlankSecretMeta()])),
    };
    updatePayload((prev) => ({ ...prev, agents: [...prev.agents, clone] }));
  };

  const moveTask = (configId, directionOrTargetId) => {
    updatePayload((prev) => {
      const agents = [...(prev.agents || [])];
      const from = agents.findIndex((agent) => agent.config_id === configId);
      if (from < 0) return prev;
      let to = typeof directionOrTargetId === 'number'
        ? from + directionOrTargetId
        : agents.findIndex((agent) => agent.config_id === directionOrTargetId);
      if (to < 0 || to >= agents.length || to === from) return prev;
      const [item] = agents.splice(from, 1);
      agents.splice(to, 0, item);
      return { ...prev, agents };
    });
  };

  const updateEditingTask = (field, value) => {
    setEditingTask((prev) => {
      if (!prev) return prev;
      const next = { ...prev, [field]: value };
      if (String(next.mode || '').toUpperCase() === 'SPOT_DCA') {
        const qty = Number(next.initial_qty || 0);
        const avg = Number(next.manual_avg_cost || 0);
        if ((field === 'initial_qty' || field === 'manual_avg_cost') && qty > 0 && avg > 0) {
          next.initial_cost = Number((qty * avg).toFixed(8));
        }
      }
      return next;
    });
  };

  const updateEditingTaskSummarizer = (field, value) => {
    setEditingTask((prev) => ({
      ...prev,
      summarizer: { ...(prev.summarizer || {}), [field]: value },
    }));
  };

  const deleteAgentData = async (configId) => {
    await api.delete(`/config/${configId}`);
    await loadAll();
  };

  // --- Provider CRUD ---
  const openAddProvider = (apiBase = '') => {
    setEditingProvider({ ...buildBlankProvider(), api_base: normalizeApiBase(apiBase) });
    setProviderDrawerOpen(true);
  };

  const openEditProvider = (provider) => {
    setEditingProvider({ ...provider, secrets: provider.secrets || { api_key: buildBlankSecretMeta() } });
    setProviderDrawerOpen(true);
  };

  const saveProvider = () => {
    if (!editingProvider) return;
    const model = String(editingProvider.model || '').trim();
    const apiBase = normalizeApiBase(editingProvider.api_base);
    if (!model) {
      message.error(locale === 'zh' ? '请填写模型名称' : 'Model is required');
      return;
    }
    if (!apiBase) {
      message.error(locale === 'zh' ? '请填写 API Base，便于按服务端点管理模型' : 'API Base is required to organize this provider');
      return;
    }
    const providerToSave = {
      ...editingProvider,
      model,
      api_base: apiBase,
      name: String(editingProvider.name || '').trim() || `${apiBaseDisplayName(apiBase, locale)} · ${model}`,
    };
    updatePayload((prev) => {
      const providers = [...(prev.llm_providers || [])];
      const idx = providers.findIndex((p) => p.provider_id === providerToSave.provider_id);
      if (idx >= 0) providers[idx] = providerToSave;
      else providers.push(providerToSave);
      return { ...prev, llm_providers: providers };
    });
    setProviderDrawerOpen(false);
    setEditingProvider(null);
  };

  const duplicateProvider = (provider) => {
    const now = Date.now();
    setEditingProvider({
      ...provider,
      provider_id: `${provider.provider_id}-copy-${now}`,
      name: provider.name ? `${provider.name} Copy` : `Provider Copy ${now}`,
      secrets: { api_key: buildBlankSecretMeta() },
    });
    setProviderDrawerOpen(true);
  };

  const deleteProvider = (providerId) => {
    updatePayload((prev) => ({
      ...prev,
      llm_providers: (prev.llm_providers || []).filter((p) => p.provider_id !== providerId),
    }));
    setProviderDrawerOpen(false);
    setEditingProvider(null);
  };

  const updateEditingProvider = (field, value) => {
    setEditingProvider((prev) => (prev ? { ...prev, [field]: value } : prev));
  };

  const applyProviderPreset = (preset) => {
    setEditingProvider((prev) => prev ? {
      ...prev,
      name: prev.name || preset.label,
      api_base: preset.api_base,
      model: preset.model,
      compatibility_mode: preset.compatibility_mode,
      thinking_enabled: preset.thinking_enabled,
      reasoning_effort: preset.reasoning_effort,
    } : prev);
  };

  const updateEditingProviderSecret = (field, patch) => {
    setEditingProvider((prev) => ({
      ...prev,
      secrets: { ...prev.secrets, [field]: { ...(prev.secrets?.[field] || {}), ...patch } },
    }));
  };

  // --- Profile CRUD ---
  const openAddProfile = () => {
    setEditingProfile(buildBlankProfile());
    setProfileDrawerOpen(true);
  };

  const openEditProfile = (profile) => {
    setEditingProfile({ ...profile, secrets: profile.secrets || { api_key: buildBlankSecretMeta(), secret: buildBlankSecretMeta(), passphrase: buildBlankSecretMeta() } });
    setProfileDrawerOpen(true);
  };

  const saveProfile = () => {
    if (!editingProfile) return;
    updatePayload((prev) => {
      const profiles = [...(prev.exchange_profiles || [])];
      const idx = profiles.findIndex((p) => p.profile_id === editingProfile.profile_id);
      if (idx >= 0) profiles[idx] = editingProfile;
      else profiles.push(editingProfile);
      return { ...prev, exchange_profiles: profiles };
    });
    setProfileDrawerOpen(false);
    setEditingProfile(null);
  };

  const deleteProfile = (profileId) => {
    updatePayload((prev) => ({
      ...prev,
      exchange_profiles: (prev.exchange_profiles || []).filter((p) => p.profile_id !== profileId),
    }));
    setProfileDrawerOpen(false);
    setEditingProfile(null);
  };

  const updateEditingProfile = (field, value) => {
    setEditingProfile((prev) => (prev ? { ...prev, [field]: value } : prev));
  };

  const updateEditingProfileSecret = (field, patch) => {
    setEditingProfile((prev) => ({
      ...prev,
      secrets: { ...prev.secrets, [field]: { ...(prev.secrets?.[field] || {}), ...patch } },
    }));
  };

  // --- Prompt CRUD ---
  const savePrompt = async () => {
    if (!selectedPrompt) return;
    await api.put('/config/prompts', { name: selectedPrompt, content: promptContent });
    await loadAll();
  };

  const deletePrompt = async () => {
    if (!selectedPrompt) return;
    await api.delete('/config/prompts', { data: { name: selectedPrompt } });
    setSelectedPrompt('');
    setPromptContent('');
    await loadAll();
  };

  const createPrompt = async () => {
    const name = newPromptName.trim();
    if (!name) return;
    const filename = name.endsWith('.txt') ? name : `${name}.txt`;
    await api.put('/config/prompts', { name: filename, content: DEFAULT_PROMPT_FILE_CONTENT });
    setNewPromptName('');
    await loadAll();
    setSelectedPrompt(filename);
  };

  // --- Import/Export ---
  const handleExport = async () => {
    try {
      const response = await api.get('/config/full-export', { responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const a = document.createElement('a');
      a.href = url;
      a.download = `crypto_full_export_${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message || 'Export failed');
    }
  };

  const handleImport = async (file) => {
    setImporting(true);
    setError('');
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      await api.post('/config/full-import', { data, write_env: importWriteEnv });
      await loadAll();
    } catch (err) {
      setError(err.message || 'Import failed');
    } finally {
      setImporting(false);
    }
    return false;
  };

  const getProviderInfo = (providerId) => {
    if (!providerId || !payload?.llm_providers) return null;
    return payload.llm_providers.find((p) => p.provider_id === providerId) || null;
  };

  const getProfileInfo = (profileId) => {
    if (!profileId || !payload?.exchange_profiles) return null;
    return payload.exchange_profiles.find((p) => p.profile_id === profileId) || null;
  };

  const providerCatalog = useMemo(() => {
    const providers = payload?.llm_providers || [];
    const taskUsage = new Map();
    (payload?.agents || []).forEach((agent) => {
      [agent.llm_provider_id, agent.summarizer_provider_id]
        .filter(Boolean)
        .forEach((providerId) => {
          const tasks = taskUsage.get(providerId) || new Set();
          tasks.add(agent.config_id);
          taskUsage.set(providerId, tasks);
        });
    });

    const groupMap = new Map();
    providers.forEach((provider) => {
      const apiBase = normalizeApiBase(provider.api_base);
      const key = apiBaseGroupKey(apiBase);
      if (!groupMap.has(key)) {
        groupMap.set(key, {
          key,
          apiBase,
          label: apiBaseDisplayName(apiBase, locale),
          providers: [],
          taskIds: new Set(),
        });
      }
      const group = groupMap.get(key);
      group.providers.push(provider);
      (taskUsage.get(provider.provider_id) || new Set()).forEach((taskId) => group.taskIds.add(taskId));
    });

    const groups = [...groupMap.values()]
      .map((group) => ({
        ...group,
        providers: [...group.providers].sort((a, b) => String(a.model || a.name).localeCompare(String(b.model || b.name))),
        configuredCount: group.providers.filter(providerHasApiKey).length,
        taskCount: group.taskIds.size,
      }))
      .sort((a, b) => {
        if (!a.apiBase) return 1;
        if (!b.apiBase) return -1;
        return a.label.localeCompare(b.label);
      });

    const normalizedQuery = providerQuery.trim().toLowerCase();
    const visibleGroups = groups
      .map((group) => {
        const visibleProviders = group.providers.filter((provider) => {
          const matchesApiBase = providerApiBaseFilter === '__all__' || group.key === providerApiBaseFilter;
          const hasKey = providerHasApiKey(provider);
          const matchesKey = providerKeyFilter === '__all__'
            || (providerKeyFilter === 'configured' && hasKey)
            || (providerKeyFilter === 'missing' && !hasKey);
          const matchesThinking = providerThinkingFilter === '__all__'
            || (providerThinkingFilter === 'thinking' && provider.thinking_enabled === true)
            || (providerThinkingFilter === 'standard' && provider.thinking_enabled !== true);
          const searchable = [provider.name, provider.provider_id, provider.model, provider.api_base, provider.role]
            .join(' ')
            .toLowerCase();
          return matchesApiBase && matchesKey && matchesThinking && (!normalizedQuery || searchable.includes(normalizedQuery));
        });
        return {
          ...group,
          providers: visibleProviders,
          configuredCount: visibleProviders.filter(providerHasApiKey).length,
          taskCount: new Set(
            visibleProviders.flatMap((provider) => [...(taskUsage.get(provider.provider_id) || new Set())]),
          ).size,
        };
      })
      .filter((group) => group.providers.length);

    return {
      groups,
      visibleGroups,
      endpointOptions: groups.map((group) => ({
        label: `${group.label} (${group.providers.length})`,
        value: group.key,
      })),
      endpointDraftOptions: groups
        .filter((group) => group.apiBase)
        .map((group) => ({ label: `${group.label} · ${group.apiBase}`, value: group.apiBase })),
      stats: {
        total: providers.length,
        endpoints: groups.filter((group) => group.apiBase).length,
        configured: providers.filter(providerHasApiKey).length,
        assignedTasks: new Set([...taskUsage.values()].flatMap((tasks) => [...tasks])).size,
      },
    };
  }, [locale, payload?.agents, payload?.llm_providers, providerApiBaseFilter, providerKeyFilter, providerQuery, providerThinkingFilter]);

  const taskMode = editingTask?.mode || 'STRATEGY';
  const memoryDashboard = payload ? {
    current_symbol: payload.agents?.[0]?.symbol || '',
    symbols: Array.from(new Set((payload.agents || []).map((agent) => agent.symbol).filter(Boolean))),
    agent_summaries: (payload.agents || []).map((agent) => ({
      config_id: agent.config_id,
      symbol: agent.symbol,
      mode: agent.mode,
      model: agent.model,
      enabled: agent.enabled,
    })),
  } : null;

  return (
    <Space className="admin-page" direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="hero-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Title level={2} style={{ margin: 0 }}>{t('config')}</Title>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {locale === 'zh' ? '数据库驱动的运行配置，支持加密密钥和表单编辑。' : 'Database-backed runtime config with encrypted secrets and form-first editing.'}
            </Paragraph>
          </div>
          <Space>
            <Tag color={saveState === 'failed' ? 'red' : saveState === 'saving' ? 'blue' : saveState === 'unsaved' ? 'gold' : saveState === 'saved' ? 'green' : 'default'}>
              {saveState === 'failed' ? 'Save failed' : saveState === 'saving' ? 'Saving...' : saveState === 'unsaved' ? 'Unsaved' : saveState === 'saved' ? 'Saved' : 'Auto save'}
            </Tag>
            <Button onClick={saveConfig} loading={saving} disabled={!payload}>{t('saveConfig')}</Button>
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon closable onClose={() => setError('')} /> : null}

      {loading ? (
        <Card className="panel-card loading-card"><Spin /></Card>
      ) : payload ? (
        <Tabs activeKey={activeAdminTab} onChange={setActiveAdminTab} items={[
          // ===== 运行配置 =====
          {
            key: 'runtime',
            label: t('runtimeConfig'),
            children: (
              <Card className="panel-card" title={t('globals')}>
                <div className="field-grid">
                  <div className="form-field">
                    <label>Leverage</label>
                    <InputNumber min={1} value={payload.globals.leverage} onChange={(v) => updateGlobal('leverage', v ?? 1)} />
                  </div>
                  <div className="form-field">
                    <label>{t('scheduler')}</label>
                    <Switch checked={payload.globals.enable_scheduler} onChange={(c) => updateGlobal('enable_scheduler', c)} />
                  </div>
                  <div className="form-field">
                    <label>LangChain Tracing</label>
                    <Switch checked={payload.globals.langchain_tracing} onChange={(c) => updateGlobal('langchain_tracing', c)} />
                  </div>
                  <div className="form-field">
                    <label>LangChain Project</label>
                    <Input value={payload.globals.langchain_project} onChange={(e) => updateGlobal('langchain_project', e.target.value)} />
                  </div>
                  <div className="form-field">
                    <label>LLM Timeout</label>
                    <InputNumber min={10} value={payload.globals.llm_timeout_seconds} onChange={(v) => updateGlobal('llm_timeout_seconds', v ?? 120)} />
                  </div>
                  <div className="form-field">
                    <label>LLM Retries</label>
                    <InputNumber min={0} value={payload.globals.llm_max_retries} onChange={(v) => updateGlobal('llm_max_retries', v ?? 0)} />
                  </div>
                  <div className="form-field field-span-2">
                    <label>{locale === 'zh' ? '默认行情分析周期（未单独配置任务时生效）' : 'Default market analysis timeframes (fallback)'}</label>
                    <Select
                      mode="multiple"
                      value={payload.globals.market_timeframes || MARKET_TIMEFRAME_OPTIONS}
                      options={MARKET_TIMEFRAME_OPTIONS.map((value) => ({ label: value, value }))}
                      onChange={(values) => updateGlobal('market_timeframes', values.length ? values : MARKET_TIMEFRAME_OPTIONS)}
                      style={{ width: '100%' }}
                    />
                  </div>
                </div>
                <div className="field-grid">
                  <SecretField label="Global Binance API Key" meta={payload.globals.secrets.global_binance_api_key} onChange={(v) => updateGlobalSecret('global_binance_api_key', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_binance_api_key', { value: '', clear: true })} />
                  <SecretField label="Global Binance Secret" meta={payload.globals.secrets.global_binance_secret} onChange={(v) => updateGlobalSecret('global_binance_secret', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_binance_secret', { value: '', clear: true })} />
                  <SecretField label="OKX API Key" meta={payload.globals.secrets.global_okx_api_key} onChange={(v) => updateGlobalSecret('global_okx_api_key', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_okx_api_key', { value: '', clear: true })} />
                  <SecretField label="OKX Secret" meta={payload.globals.secrets.global_okx_secret} onChange={(v) => updateGlobalSecret('global_okx_secret', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_okx_secret', { value: '', clear: true })} />
                  <SecretField label="OKX Passphrase" meta={payload.globals.secrets.global_okx_passphrase} onChange={(v) => updateGlobalSecret('global_okx_passphrase', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_okx_passphrase', { value: '', clear: true })} />
                  <SecretField label="LangSmith API Key" meta={payload.globals.secrets.langchain_api_key} onChange={(v) => updateGlobalSecret('langchain_api_key', { value: v, clear: false })} onClear={() => updateGlobalSecret('langchain_api_key', { value: '', clear: true })} />
                </div>
              </Card>
            ),
          },
          // ===== 任务配置 =====
          {
            key: 'tasks',
            label: locale === 'zh' ? '任务配置' : 'Task Config',
            children: (
              <Card className="panel-card" title={locale === 'zh' ? '任务配置' : 'Task Config'} extra={
                <Button type="primary" onClick={openAddTask}>{t('addAgent')}</Button>
              }>
                {isMobile ? (
                  <div className="admin-mobile-list">
                    {(payload.agents || []).map((record, index) => (
                      <Card
                        key={record.config_id}
                        size="small"
                        className="admin-mobile-card"
                        title={record.title || record.config_id}
                        extra={<Tag color={record.enabled ? 'green' : 'default'}>{record.enabled ? 'ON' : 'OFF'}</Tag>}
                      >
                        <div className="admin-mobile-meta">
                          <Text type="secondary">Config</Text><Text>{record.config_id}</Text>
                          <Text type="secondary">{t('symbol')}</Text><Text>{record.symbol || '-'}</Text>
                          <Text type="secondary">Mode</Text><Tag>{record.mode}</Tag>
                          <Text type="secondary">Prompt</Text><Text className="text-break">{record.prompt_file || '-'}</Text>
                        </div>
                        <Space className="admin-mobile-actions" wrap>
                          <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveTask(record.config_id, -1)} />
                          <Button size="small" icon={<ArrowDownOutlined />} disabled={index === (payload.agents || []).length - 1} onClick={() => moveTask(record.config_id, 1)} />
                          <Button size="small" onClick={() => openEditTask(record)}>Edit</Button>
                          <Button size="small" onClick={() => duplicateTask(record)}>Copy</Button>
                          <Popconfirm title={t('confirmDelete')} description="Delete this config and linked data?" onConfirm={() => deleteAgentData(record.config_id)}>
                            <Button size="small" danger>Delete</Button>
                          </Popconfirm>
                        </Space>
                      </Card>
                    ))}
                    {!(payload.agents || []).length ? <Empty description={t('emptyConfig')} /> : null}
                  </div>
                ) : (
                  <Table
                    rowKey="config_id"
                    dataSource={payload.agents || []}
                    pagination={false}
                    scroll={{ x: 1120 }}
                    onRow={(record) => ({
                      draggable: true,
                      onDragStart: () => setDraggingTaskId(record.config_id),
                      onDragOver: (event) => event.preventDefault(),
                      onDrop: () => {
                        if (draggingTaskId) moveTask(draggingTaskId, record.config_id);
                        setDraggingTaskId('');
                      },
                      onDragEnd: () => setDraggingTaskId(''),
                    })}
                    columns={[
                      {
                        title: '',
                        width: 96,
                        fixed: 'left',
                        render: (_, record, index) => (
                          <Space size={4} className="agent-order-controls">
                            <HolderOutlined className="agent-drag-handle" />
                            <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveTask(record.config_id, -1)} />
                            <Button size="small" icon={<ArrowDownOutlined />} disabled={index === (payload.agents || []).length - 1} onClick={() => moveTask(record.config_id, 1)} />
                          </Space>
                        ),
                      },
                      { title: 'Config ID', dataIndex: 'config_id', width: 180, ellipsis: true },
                      { title: 'Title', dataIndex: 'title', width: 180, ellipsis: true },
                      { title: t('symbol'), dataIndex: 'symbol', width: 120 },
                      { title: 'Mode', dataIndex: 'mode', width: 120, render: (v) => <Tag>{v}</Tag> },
                      { title: 'Enabled', dataIndex: 'enabled', width: 110, render: (v) => <Tag color={v ? 'green' : 'default'}>{v ? 'ON' : 'OFF'}</Tag> },
                      { title: 'Prompt', dataIndex: 'prompt_file', width: 180, ellipsis: true },
                      {
                        title: '', width: 210, fixed: 'right', render: (_, record) => (
                          <Space className="table-actions">
                            <Button size="small" onClick={() => openEditTask(record)}>Edit</Button>
                            <Button size="small" onClick={() => duplicateTask(record)}>Copy</Button>
                            <Popconfirm
                              title={t('confirmDelete')}
                              description="Delete this config and linked data?"
                              onConfirm={() => deleteAgentData(record.config_id)}
                            >
                              <Button size="small" danger>Delete</Button>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                )}
              </Card>
            ),
          },
          // ===== 模型服务商 =====
          {
            key: 'providers',
            label: t('llmProviders'),
            children: (
              <Card
                className="panel-card provider-catalog-card"
                title={t('llmProviders')}
                extra={<Button type="primary" onClick={() => openAddProvider()}>{locale === 'zh' ? '新建服务端点 / 模型' : 'New endpoint / model'}</Button>}
              >
                <div className="provider-overview" aria-label={locale === 'zh' ? '模型服务商概览' : 'Provider overview'}>
                  <div className="provider-stat"><Text type="secondary">{locale === 'zh' ? '模型配置' : 'Models'}</Text><strong>{providerCatalog.stats.total}</strong></div>
                  <div className="provider-stat"><Text type="secondary">API Base</Text><strong>{providerCatalog.stats.endpoints}</strong></div>
                  <div className="provider-stat"><Text type="secondary">{locale === 'zh' ? '已配置密钥' : 'Keys configured'}</Text><strong>{providerCatalog.stats.configured}</strong></div>
                  <div className="provider-stat"><Text type="secondary">{locale === 'zh' ? '任务正在使用' : 'Used by tasks'}</Text><strong>{providerCatalog.stats.assignedTasks}</strong></div>
                </div>

                <div className="provider-filter-bar">
                  <Input
                    allowClear
                    value={providerQuery}
                    onChange={(event) => setProviderQuery(event.target.value)}
                    placeholder={locale === 'zh' ? '搜索名称、模型、端点或配置 ID' : 'Search name, model, endpoint, or ID'}
                  />
                  <Select
                    value={providerApiBaseFilter}
                    options={[{ label: locale === 'zh' ? '全部 API Base' : 'All API Bases', value: '__all__' }, ...providerCatalog.endpointOptions]}
                    onChange={setProviderApiBaseFilter}
                    showSearch
                    optionFilterProp="label"
                  />
                  <Select
                    value={providerKeyFilter}
                    options={[
                      { label: locale === 'zh' ? '全部密钥状态' : 'All key states', value: '__all__' },
                      { label: locale === 'zh' ? '已配置密钥' : 'Key configured', value: 'configured' },
                      { label: locale === 'zh' ? '未配置密钥' : 'Key missing', value: 'missing' },
                    ]}
                    onChange={setProviderKeyFilter}
                  />
                  <Select
                    value={providerThinkingFilter}
                    options={[
                      { label: locale === 'zh' ? '全部推理模式' : 'All reasoning modes', value: '__all__' },
                      { label: locale === 'zh' ? '支持思考' : 'Thinking enabled', value: 'thinking' },
                      { label: locale === 'zh' ? '标准模式' : 'Standard mode', value: 'standard' },
                    ]}
                    onChange={setProviderThinkingFilter}
                  />
                  {(providerQuery || providerApiBaseFilter !== '__all__' || providerKeyFilter !== '__all__' || providerThinkingFilter !== '__all__') && (
                    <Button onClick={() => {
                      setProviderQuery('');
                      setProviderApiBaseFilter('__all__');
                      setProviderKeyFilter('__all__');
                      setProviderThinkingFilter('__all__');
                    }}>
                      {locale === 'zh' ? '重置' : 'Reset'}
                    </Button>
                  )}
                </div>

                {providerCatalog.visibleGroups.length ? (
                  <Collapse
                    className="provider-groups"
                    defaultActiveKey={providerCatalog.visibleGroups.map((group) => group.key)}
                    items={providerCatalog.visibleGroups.map((group) => ({
                      key: group.key,
                      label: (
                        <div className="provider-group-heading">
                          <div className="provider-group-endpoint">
                            <Text strong>{group.label}</Text>
                            <Text type="secondary" className="text-break">{group.apiBase || (locale === 'zh' ? '需要补充端点地址' : 'Endpoint needs to be configured')}</Text>
                          </div>
                          <Space size={6} wrap>
                            <Tag>{group.providers.length} {locale === 'zh' ? '个模型' : 'models'}</Tag>
                            <Tag color={group.configuredCount === group.providers.length ? 'green' : 'gold'}>{group.configuredCount}/{group.providers.length} {locale === 'zh' ? '密钥' : 'keys'}</Tag>
                            {group.taskCount > 0 && <Tag color="blue">{group.taskCount} {locale === 'zh' ? '个任务' : 'tasks'}</Tag>}
                          </Space>
                        </div>
                      ),
                      extra: (
                        <Button
                          size="small"
                          onClick={(event) => {
                            event.stopPropagation();
                            openAddProvider(group.apiBase);
                          }}
                        >
                          {locale === 'zh' ? '添加模型' : 'Add model'}
                        </Button>
                      ),
                      children: isMobile ? (
                        <div className="admin-mobile-list">
                          {group.providers.map((record) => (
                            <Card
                              key={record.provider_id}
                              size="small"
                              className="admin-mobile-card"
                              title={record.name || record.provider_id}
                              extra={<Tag color={providerHasApiKey(record) ? 'green' : 'gold'}>{providerHasApiKey(record) ? (locale === 'zh' ? '密钥已配' : 'Key ready') : (locale === 'zh' ? '缺少密钥' : 'Key missing')}</Tag>}
                            >
                              <div className="admin-mobile-meta">
                                <Text type="secondary">Model</Text><Text className="text-break">{record.model || '-'}</Text>
                                <Text type="secondary">{locale === 'zh' ? '任务引用' : 'Task usage'}</Text><Text>{[...(new Set((payload.agents || []).filter((agent) => agent.llm_provider_id === record.provider_id || agent.summarizer_provider_id === record.provider_id).map((agent) => agent.config_id)))].length}</Text>
                                <Text type="secondary">{t('thinkingMode')}</Text><Text>{record.thinking_enabled === true ? (locale === 'zh' ? '已启用' : 'Enabled') : (locale === 'zh' ? '标准' : 'Standard')}</Text>
                                <Text type="secondary">{t('reasoningEffort')}</Text><Text>{record.reasoning_effort || '-'}</Text>
                              </div>
                              <Space className="admin-mobile-actions" wrap>
                                <Button size="small" onClick={() => openEditProvider(record)}>{locale === 'zh' ? '编辑' : 'Edit'}</Button>
                                <Button size="small" onClick={() => duplicateProvider(record)}>{locale === 'zh' ? '复制' : 'Copy'}</Button>
                                <Popconfirm title={t('confirmDelete')} onConfirm={() => deleteProvider(record.provider_id)}>
                                  <Button size="small" danger>{locale === 'zh' ? '删除' : 'Delete'}</Button>
                                </Popconfirm>
                              </Space>
                            </Card>
                          ))}
                        </div>
                      ) : (
                        <Table
                          className="provider-group-table"
                          rowKey="provider_id"
                          dataSource={group.providers}
                          pagination={false}
                          scroll={{ x: 980 }}
                          columns={[
                            {
                              title: t('providerName'), width: 220,
                              render: (_, record) => (
                                <Space direction="vertical" size={0}>
                                  <Text strong>{record.name || record.provider_id}</Text>
                                  <Text type="secondary" className="provider-id">{record.provider_id}</Text>
                                </Space>
                              ),
                            },
                            { title: 'Model', dataIndex: 'model', width: 180, ellipsis: true },
                            {
                              title: locale === 'zh' ? '密钥状态' : 'API key', width: 120,
                              render: (_, record) => <Tag color={providerHasApiKey(record) ? 'green' : 'gold'}>{providerHasApiKey(record) ? (locale === 'zh' ? '已配置' : 'Configured') : (locale === 'zh' ? '未配置' : 'Missing')}</Tag>,
                            },
                            {
                              title: locale === 'zh' ? '任务引用' : 'Task usage', width: 105,
                              render: (_, record) => {
                                const usages = (payload.agents || []).filter((agent) => agent.llm_provider_id === record.provider_id || agent.summarizer_provider_id === record.provider_id);
                                return usages.length ? <Tooltip title={usages.map((agent) => agent.title || agent.config_id).join(', ')}><Tag color="blue">{usages.length}</Tag></Tooltip> : '-';
                              },
                            },
                            { title: t('thinkingMode'), width: 110, render: (_, record) => record.thinking_enabled === true ? <Tag color="purple">Thinking</Tag> : <Tag>Standard</Tag> },
                            { title: t('reasoningEffort'), dataIndex: 'reasoning_effort', width: 115, render: (value) => value || '-' },
                            { title: 'Input $/M', dataIndex: 'input_price_per_m', width: 105, render: (value) => value ?? 0 },
                            { title: 'Output $/M', dataIndex: 'output_price_per_m', width: 112, render: (value) => value ?? 0 },
                            {
                              title: '', width: 185, fixed: 'right', render: (_, record) => (
                                <Space className="table-actions">
                                  <Button size="small" onClick={() => openEditProvider(record)}>{locale === 'zh' ? '编辑' : 'Edit'}</Button>
                                  <Button size="small" onClick={() => duplicateProvider(record)}>{locale === 'zh' ? '复制' : 'Copy'}</Button>
                                  <Popconfirm title={t('confirmDelete')} onConfirm={() => deleteProvider(record.provider_id)}>
                                    <Button size="small" danger>{locale === 'zh' ? '删除' : 'Delete'}</Button>
                                  </Popconfirm>
                                </Space>
                              ),
                            },
                          ]}
                        />
                      ),
                    }))}
                  />
                ) : (
                  <Empty description={providerCatalog.stats.total ? (locale === 'zh' ? '没有符合当前筛选条件的模型' : 'No models match the current filters') : t('noProvider')} />
                )}
              </Card>
            ),
          },
          // ===== 交易所配置 =====
          {
            key: 'exchanges',
            label: t('exchangeProfiles'),
            children: (
              <Card className="panel-card" title={t('exchangeProfiles')} extra={
                <Button type="primary" onClick={openAddProfile}>{t('addProfile')}</Button>
              }>
                <Table
                  rowKey="profile_id"
                  dataSource={payload.exchange_profiles || []}
                  pagination={false}
                  columns={[
                    { title: t('profileName'), dataIndex: 'name' },
                    { title: 'Exchange', dataIndex: 'exchange' },
                    { title: 'Market Type', dataIndex: 'market_type' },
                    {
                      title: '', width: 120, render: (_, record) => (
                        <Space>
                          <Button size="small" onClick={() => openEditProfile(record)}>Edit</Button>
                          <Popconfirm title={t('confirmDelete')} onConfirm={() => deleteProfile(record.profile_id)}>
                            <Button size="small" danger>Delete</Button>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'memory',
            label: t('memoryCenter'),
            children: (
              <Card className="panel-card" title={t('memoryCenter')}>
                <Tabs
                  items={[
                    {
                      key: 'daily',
                      label: t('dailySummaries'),
                      children: <DailySummaryPanel dashboard={memoryDashboard} authenticated embedded />,
                    },
                    {
                      key: 'short',
                      label: t('shortMemories'),
                      children: <ShortMemoryPanel dashboard={memoryDashboard} authenticated embedded />,
                    },
                  ]}
                />
              </Card>
            ),
          },
          // ===== Prompt =====
          {
            key: 'prompts',
            label: t('prompts'),
            children: (
              <div className="config-editor prompt-config-editor">
                <Card className="panel-card config-sidebar prompt-sidebar-card" title={t('promptEditor')} extra={
                  <Space.Compact className="prompt-create">
                    <Input
                      size="small"
                      placeholder={locale === 'zh' ? '新文件名' : 'New filename'}
                      value={newPromptName}
                      onChange={(e) => setNewPromptName(e.target.value)}
                      onPressEnter={createPrompt}
                    />
                    <Tooltip title={t('create')}>
                      <Button size="small" type="primary" icon={<PlusOutlined />} onClick={createPrompt} disabled={!newPromptName.trim()} />
                    </Tooltip>
                  </Space.Compact>
                }>
                  <List
                    className="prompt-file-list"
                    dataSource={payload.prompts?.files || []}
                    renderItem={(item) => (
                      <List.Item>
                        <button
                          type="button"
                          className={`prompt-file-row ${item === selectedPrompt ? 'active' : ''}`}
                          onClick={() => setSelectedPrompt(item)}
                        >
                          <FileTextOutlined />
                          <span>{item}</span>
                        </button>
                      </List.Item>
                    )}
                  />
                </Card>
                <Card className="panel-card" title={selectedPrompt || t('promptEditor')}>
                  <Space direction="vertical" style={{ width: '100%' }} size="middle">
                    <div className="prompt-editor-container">
                      <PromptCodeEditor
                        value={promptContent}
                        onChange={setPromptContent}
                        placeholder="Use {current_time}, {symbol}, {formatted_market_data}, {positions_text}, {orders_text}, {history_text}, {short_memory_text}"
                        height={520}
                      />
                      <div className="prompt-editor-stats">
                        <Text type="secondary">{(promptContent || '').split('\n').length} {locale === 'zh' ? '行' : 'lines'}</Text>
                        <Text type="secondary">{(promptContent || '').length} {locale === 'zh' ? '字符' : 'chars'}</Text>
                      </div>
                    </div>
                    <PromptVarHints content={promptContent} vars={AGENT_PROMPT_VARS} locale={locale} />
                    <Space>
                      <Button type="primary" onClick={savePrompt} disabled={!selectedPrompt}>{t('savePrompt')}</Button>
                      <Popconfirm title={t('confirmDelete')} onConfirm={deletePrompt} disabled={!selectedPrompt}>
                        <Button danger disabled={!selectedPrompt}>{t('deletePrompt')}</Button>
                      </Popconfirm>
                    </Space>
                  </Space>
                </Card>
              </div>
            ),
          },
          // ===== 导入导出 =====
          {
            key: 'importexport',
            label: locale === 'zh' ? '导入导出' : 'Import/Export',
            children: (
              <Card className="panel-card" title={locale === 'zh' ? '快速导入导出' : 'Quick Import/Export'}>
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <Alert type="info" showIcon message={
                    locale === 'zh'
                      ? '导出包含所有配置、明文密钥、Prompt 文件和模型计价。不同 CONFIG_MASTER_KEY 的机器之间可以自由迁移，导入时密钥会用当前 KEY 重新加密。'
                      : 'Export includes all configs, plaintext secrets, prompts, and pricing. Migration between machines with different CONFIG_MASTER_KEY is supported — secrets are re-encrypted on import.'
                  } />
                  <Space>
                    <Button type="primary" onClick={handleExport}>
                      {locale === 'zh' ? '导出全部配置' : 'Export All'}
                    </Button>
                    <Upload accept=".json" showUploadList={false} beforeUpload={handleImport}>
                      <Button loading={importing}>
                        <UploadOutlined /> {locale === 'zh' ? '导入配置' : 'Import Config'}
                      </Button>
                    </Upload>
                  </Space>
                  <div className="form-field">
                    <Space>
                      <Switch checked={importWriteEnv} onChange={setImportWriteEnv} />
                      <Text type="secondary">
                        {locale === 'zh'
                          ? '同时写入 .env（ADMIN_PASSWORD、JWT_SECRET、CONFIG_MASTER_KEY、PORT 等）'
                          : 'Also write .env (ADMIN_PASSWORD, JWT_SECRET, CONFIG_MASTER_KEY, PORT, etc.)'}
                      </Text>
                    </Space>
                  </div>
                </Space>
              </Card>
            ),
          },
          // ===== 计价 =====
        ]} />
      ) : null}

      {/* Task (Agent) Drawer */}
      <Drawer
        title={editingTaskId ? (editingTask?.title || editingTaskId) : (locale === 'zh' ? '新增任务' : 'Add Task')}
        width={isMobile ? '100vw' : 720}
        open={taskDrawerOpen}
        onClose={() => { setTaskDrawerOpen(false); setEditingTask(null); setEditingTaskId(null); }}
        extra={
          <Space>
            {editingTaskId && (
              <Popconfirm
                title={t('confirmDelete')}
                description="Delete this config and linked data?"
                onConfirm={() => deleteAgentData(editingTaskId)}
              >
                <Button danger>{t('delete')}</Button>
              </Popconfirm>
            )}
            <Button type="primary" onClick={saveTask}>{t('save')}</Button>
          </Space>
        }
      >
        {editingTask && (
          <Collapse defaultActiveKey={['basic', 'schedule', 'model']} items={[
            {
              key: 'basic',
              label: t('basicSettings'),
              children: (
                <div className="field-grid">
                  <div className="form-field">
                    <label>Config ID *</label>
                    <Input value={editingTask.config_id} onChange={(e) => updateEditingTask('config_id', e.target.value)} />
                  </div>
                  <div className="form-field">
                    <label>Title</label>
                    <Input value={editingTask.title || ''} onChange={(e) => updateEditingTask('title', e.target.value)} />
                  </div>
                  <div className="form-field">
                    <label>{t('symbol')} *</label>
                    <Input value={editingTask.symbol} onChange={(e) => updateEditingTask('symbol', e.target.value)} />
                  </div>
                  <div className="form-field">
                    <label>Enabled</label>
                    <Switch checked={editingTask.enabled} onChange={(c) => updateEditingTask('enabled', c)} />
                  </div>
                  <div className="form-field">
                    <label>Mode *</label>
                    <Select value={editingTask.mode} options={(payload.options?.modes || []).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingTask('mode', v)} style={{ width: '100%' }} />
                  </div>
                  <div className="form-field">
                    <label>Prompt File *</label>
                    <Select value={editingTask.prompt_file || undefined} options={(payload.options?.prompt_files || []).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingTask('prompt_file', v)} allowClear style={{ width: '100%' }} />
                  </div>
                  <div className="form-field field-span-2">
                    <label>{locale === 'zh' ? '市场分析周期' : 'Market analysis timeframes'}</label>
                    <Select
                      mode="multiple"
                      value={editingTask.market_timeframes || MARKET_TIMEFRAME_OPTIONS}
                      options={(payload.options?.market_timeframes || MARKET_TIMEFRAME_OPTIONS).map((value) => ({ label: value, value }))}
                      onChange={(values) => updateEditingTask('market_timeframes', values.length ? values : [...MARKET_TIMEFRAME_OPTIONS])}
                      style={{ width: '100%' }}
                    />
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
                    <label>Run Interval</label>
                    <InputNumber min={15} value={editingTask.run_interval ?? 60} onChange={(v) => updateEditingTask('run_interval', v ?? 60)} style={{ width: '100%' }} />
                  </div>
                  {(taskMode === 'REAL' || taskMode === 'STRATEGY') && (
                    <div className="form-field">
                      <label>Leverage</label>
                      <InputNumber min={1} value={editingTask.leverage ?? 1} onChange={(v) => updateEditingTask('leverage', v ?? 1)} style={{ width: '100%' }} />
                    </div>
                  )}
                  {taskMode === 'SPOT_DCA' && (
                    <>
                      <div className="form-field">
                        <label>DCA Amount</label>
                        <InputNumber min={0} value={editingTask.dca_amount ?? 0} onChange={(v) => updateEditingTask('dca_amount', v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>DCA Freq</label>
                        <Select value={editingTask.dca_freq || undefined} options={(payload.options?.dca_freqs || []).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingTask('dca_freq', v)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>DCA Time</label>
                        <Input value={editingTask.dca_time || ''} onChange={(e) => updateEditingTask('dca_time', e.target.value)} />
                      </div>
                      <div className="form-field">
                        <label>DCA Weekday</label>
                        <InputNumber min={0} max={6} value={editingTask.dca_weekday ?? 0} onChange={(v) => updateEditingTask('dca_weekday', v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>Initial Qty</label>
                        <InputNumber min={0} value={editingTask.initial_qty ?? 0} onChange={(v) => updateEditingTask('initial_qty', v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>Manual Avg Cost</label>
                        <InputNumber min={0} value={editingTask.manual_avg_cost ?? 0} onChange={(v) => updateEditingTask('manual_avg_cost', v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>Initial Cost</label>
                        <InputNumber min={0} value={editingTask.initial_cost ?? 0} onChange={(v) => updateEditingTask('initial_cost', v ?? 0)} style={{ width: '100%' }} disabled={Number(editingTask.initial_qty || 0) > 0 && Number(editingTask.manual_avg_cost || 0) > 0} />
                      </div>
                    </>
                  )}
                </div>
              ),
            },
            {
              key: 'model',
              label: t('decisionModel'),
              children: (
                <div className="field-grid">
                  <div className="form-field field-span-2">
                    <label>{t('selectProvider')} *</label>
                    <ProviderSelect providers={payload.llm_providers} value={editingTask.llm_provider_id} onChange={(v) => updateEditingTask('llm_provider_id', v)} allowEmpty />
                  </div>
                  {(() => {
                    const info = getProviderInfo(editingTask.llm_provider_id);
                    if (!info) return null;
                    return (
                      <>
                        <div className="form-field"><label>Model</label><Input value={info.model} disabled /></div>
                        <div className="form-field"><label>Temperature</label><InputNumber value={info.temperature} disabled style={{ width: '100%' }} /></div>
                        <div className="form-field"><label>{locale === 'zh' ? '提示词角色' : 'Prompt role'}</label><Input value={info.system_prompt_role === 'user' ? (locale === 'zh' ? '用户消息' : 'User message') : 'System message'} disabled /></div>
                        <div className="form-field"><label>{locale === 'zh' ? '任务提示词角色' : 'Task prompt role'}</label><Select value={editingTask.system_prompt_role || info.system_prompt_role || 'system'} options={[{ value: 'system', label: 'System message' }, { value: 'user', label: 'User message (compatibility)' }]} onChange={(value) => updateEditingTask('system_prompt_role', value)} /></div>
                        <div className="form-field field-span-2"><label>API Base</label><Input value={info.api_base} disabled /></div>
                      </>
                    );
                  })()}
                </div>
              ),
            },
            {
              key: 'summarizer',
              label: t('summarizerModel'),
              children: (
                <div className="field-grid">
                  <div className="form-field field-span-2">
                    <label>{t('selectProvider')}</label>
                    <ProviderSelect providers={payload.llm_providers} value={editingTask.summarizer_provider_id} onChange={(v) => updateEditingTask('summarizer_provider_id', v)} allowEmpty />
                  </div>
                  {(() => {
                    const info = getProviderInfo(editingTask.summarizer_provider_id);
                    if (!info) return null;
                    return (
                      <>
                        <div className="form-field"><label>Model</label><Input value={info.model} disabled /></div>
                        <div className="form-field"><label>Temperature</label><InputNumber value={info.temperature} disabled style={{ width: '100%' }} /></div>
                        <div className="form-field field-span-2"><label>API Base</label><Input value={info.api_base} disabled /></div>
                      </>
                    );
                  })()}
                </div>
              ),
            },
            {
              key: 'exchange',
              label: t('exchangeSettings'),
              children: (
                <div className="field-grid">
                  <div className="form-field field-span-2">
                    <label>{t('selectProfile')}</label>
                    <ProfileSelect profiles={payload.exchange_profiles} value={editingTask.exchange_profile_id} onChange={(v) => updateEditingTask('exchange_profile_id', v)} allowEmpty />
                  </div>
                  {(() => {
                    const info = getProfileInfo(editingTask.exchange_profile_id);
                    if (!info) return null;
                    return (
                      <>
                        <div className="form-field"><label>Exchange</label><Input value={info.exchange} disabled /></div>
                        <div className="form-field"><label>Market Type</label><Input value={info.market_type} disabled /></div>
                      </>
                    );
                  })()}
                </div>
              ),
            },
            {
              key: 'summaryPrompts',
              label: locale === 'zh' ? '总结 Prompt' : 'Summary Prompts',
              children: (
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  <div className="form-field">
                    <label>{t('strategyPrompt')}</label>
                    <PromptEditor
                      value={editingTask.strategy_prompt || editingTask.summarizer?.strategy_prompt || ''}
                      onChange={(v) => { updateEditingTask('strategy_prompt', v); updateEditingTaskSummarizer('strategy_prompt', v); }}
                      placeholder={DEFAULT_STRATEGY_PROMPT}
                    />
                    <PromptVarHints content={editingTask.strategy_prompt || editingTask.summarizer?.strategy_prompt || ''} vars={SUMMARIZER_PROMPT_VARS} locale={locale} />
                  </div>
                  <div className="form-field">
                    <label>{t('dailyPrompt')}</label>
                    <PromptEditor
                      value={editingTask.daily_prompt || editingTask.summarizer?.daily_prompt || ''}
                      onChange={(v) => { updateEditingTask('daily_prompt', v); updateEditingTaskSummarizer('daily_prompt', v); }}
                      placeholder={DEFAULT_DAILY_PROMPT}
                    />
                    <PromptVarHints content={editingTask.daily_prompt || editingTask.summarizer?.daily_prompt || ''} vars={SUMMARIZER_PROMPT_VARS} locale={locale} />
                  </div>
                  <div className="form-field">
                    <label>{t('shortMemoryPrompt')}</label>
                    <PromptEditor
                      value={editingTask.short_memory_prompt || editingTask.summarizer?.short_memory_prompt || ''}
                      onChange={(v) => { updateEditingTask('short_memory_prompt', v); updateEditingTaskSummarizer('short_memory_prompt', v); }}
                      placeholder={DEFAULT_SHORT_MEMORY_PROMPT}
                    />
                    <PromptVarHints content={editingTask.short_memory_prompt || editingTask.summarizer?.short_memory_prompt || ''} vars={SUMMARIZER_PROMPT_VARS} locale={locale} />
                  </div>
                </Space>
              ),
            },
          ]} />
        )}
      </Drawer>

      {/* Provider Drawer */}
      <Drawer
        title={editingProvider && payload?.llm_providers?.some((p) => p.provider_id === editingProvider.provider_id) ? 'Edit Provider' : t('addProvider')}
        width={isMobile ? '100vw' : 560}
        open={providerDrawerOpen}
        onClose={() => { setProviderDrawerOpen(false); setEditingProvider(null); }}
        extra={
          <Space>
            {editingProvider && payload?.llm_providers?.some((p) => p.provider_id === editingProvider.provider_id) && (
              <Button danger onClick={() => deleteProvider(editingProvider.provider_id)}>{t('delete')}</Button>
            )}
            <Button type="primary" onClick={saveProvider}>{t('save')}</Button>
          </Space>
        }
      >
        {editingProvider && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message={locale === 'zh' ? '按 API Base 管理模型' : 'Manage models by API Base'}
              description={locale === 'zh'
                ? '从已有端点开始可快速新增模型。复制模型不会复制 API Key，避免密钥被意外复用。'
                : 'Choose an existing endpoint to add a model quickly. Copying a model never copies its API key.'}
            />
            <div className="form-field">
              <label>{locale === 'zh' ? '复用已有 API Base' : 'Reuse an API Base'}</label>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                value={providerCatalog.endpointDraftOptions.some((option) => option.value === normalizeApiBase(editingProvider.api_base)) ? normalizeApiBase(editingProvider.api_base) : undefined}
                options={providerCatalog.endpointDraftOptions}
                placeholder={locale === 'zh' ? '选择端点后自动填入下方地址' : 'Choose an endpoint to prefill the address'}
                onChange={(value) => {
                  if (value) updateEditingProvider('api_base', value);
                }}
              />
            </div>
            <div className="form-field">
              <label>{locale === 'zh' ? '快速预设' : 'Quick presets'}</label>
              <Space wrap>
                {LLM_PROVIDER_PRESETS.map((preset) => (
                  <Button key={preset.key} size="small" onClick={() => applyProviderPreset(preset)}>{preset.label}</Button>
                ))}
              </Space>
              <Text type="secondary">{locale === 'zh' ? '只填充协议、地址、模型与推荐力度，不会改动 API Key。' : 'Fills protocol, endpoint, model, and effort without changing the API key.'}</Text>
            </div>
            <div className="field-grid">
              <div className="form-field">
                <label>{t('providerName')}</label>
                <Input value={editingProvider.name} onChange={(e) => updateEditingProvider('name', e.target.value)} placeholder={locale === 'zh' ? '留空时根据端点和模型自动生成' : 'Generated from endpoint and model when empty'} />
              </div>
              <div className="form-field">
                <label>Model *</label>
                <Input value={editingProvider.model} onChange={(e) => updateEditingProvider('model', e.target.value)} placeholder="e.g. deepseek-chat, gpt-4o" />
              </div>
              <div className="form-field field-span-2">
                <label>API Base *</label>
                <Input value={editingProvider.api_base} onChange={(e) => updateEditingProvider('api_base', e.target.value)} placeholder="e.g. https://api.deepseek.com/v1" />
                <Text type="secondary">{locale === 'zh' ? '同一地址下的模型会在列表中自动归为一组。' : 'Models using the same address are grouped together automatically.'}</Text>
              </div>
            </div>
            <Text type="secondary" className="provider-id">Provider ID: {editingProvider.provider_id}</Text>
            <div className="form-field">
              <label>Temperature</label>
              <InputNumber min={0} max={2} step={0.1} value={editingProvider.temperature} onChange={(v) => updateEditingProvider('temperature', v)} style={{ width: '100%' }} />
            </div>
            <div className="form-field">
              <label>{locale === 'zh' ? '接口兼容模式' : 'API compatibility'}</label>
              <Select
                value={editingProvider.compatibility_mode || 'auto'}
                options={[
                  { value: 'auto', label: locale === 'zh' ? '自动识别（推荐）' : 'Auto detect (recommended)' },
                  { value: 'openai', label: 'OpenAI Chat Completions-compatible' },
                  { value: 'anthropic', label: 'Anthropic Messages API' },
                  { value: 'deepseek', label: 'DeepSeek Chat Completions' },
                ]}
                onChange={(value) => updateEditingProvider('compatibility_mode', value)}
                style={{ width: '100%' }}
              />
              <Text type="secondary">{locale === 'zh' ? '默认使用 OpenAI Chat Completions。DeepSeek 请显式选择其兼容模式以回传 reasoning_content；只有提供 /v1/messages 的服务才选择 Anthropic。' : 'OpenAI Chat Completions is the default. Select DeepSeek to replay reasoning_content, or Anthropic only for endpoints that expose /v1/messages.'}</Text>
            </div>
            <div className="form-field">
              <label>{locale === 'zh' ? '提示词角色' : 'Prompt role'}</label>
              <Select
                value={editingProvider.system_prompt_role || 'system'}
                options={[
                  { value: 'system', label: locale === 'zh' ? 'System 消息（默认）' : 'System message (default)' },
                  { value: 'user', label: locale === 'zh' ? 'User 消息（兼容模式）' : 'User message (compatibility)' },
                ]}
                onChange={(value) => updateEditingProvider('system_prompt_role', value)}
              />
              <Text type="secondary">{locale === 'zh' ? '若服务商报错或不支持 system role，请选择 User 消息。任务和临时聊天会自动继承。' : 'Choose User message when an endpoint rejects system roles. Task and temporary chats inherit this setting.'}</Text>
            </div>
            <div className="field-grid">
              <div className="form-field">
                <label>Input price ($ / 1M tokens)</label>
                <InputNumber min={0} step={0.01} value={editingProvider.input_price_per_m ?? 0} onChange={(v) => updateEditingProvider('input_price_per_m', v ?? 0)} style={{ width: '100%' }} />
              </div>
              <div className="form-field">
                <label>Output price ($ / 1M tokens)</label>
                <InputNumber min={0} step={0.01} value={editingProvider.output_price_per_m ?? 0} onChange={(v) => updateEditingProvider('output_price_per_m', v ?? 0)} style={{ width: '100%' }} />
              </div>
            </div>
            <SecretField label="API Key" meta={editingProvider.secrets?.api_key} onChange={(v) => updateEditingProviderSecret('api_key', { value: v, clear: false })} onClear={() => updateEditingProviderSecret('api_key', { value: '', clear: true })} />
            <div className="form-field">
              <label>{t('thinkingMode')}</label>
              <Select
                value={editingProvider.thinking_enabled == null ? 'auto' : editingProvider.thinking_enabled ? 'enabled' : 'disabled'}
                options={[
                  { value: 'auto', label: locale === 'zh' ? '跟随模型默认（推荐）' : 'Use model default (recommended)' },
                  { value: 'enabled', label: locale === 'zh' ? '强制开启' : 'Force enabled' },
                  { value: 'disabled', label: locale === 'zh' ? '强制关闭' : 'Force disabled' },
                ]}
                onChange={(value) => updateEditingProvider('thinking_enabled', value === 'auto' ? null : value === 'enabled')}
                style={{ width: '100%' }}
              />
            </div>
            {editingProvider.thinking_enabled !== false && (
              <div className="form-field">
                <label>{t('reasoningEffort')}</label>
                <Select value={editingProvider.reasoning_effort || undefined} options={(payload.options?.reasoning_efforts || ['none', 'low', 'medium', 'high', 'xhigh', 'max']).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingProvider('reasoning_effort', v)} allowClear placeholder={locale === 'zh' ? '跟随模型默认' : 'Use model default'} style={{ width: '100%' }} />
                <Text type="secondary">{editingProvider.compatibility_mode === 'deepseek'
                  ? (locale === 'zh' ? 'DeepSeek 原生支持 low / high / max；medium 与 xhigh 会映射到 high。' : 'DeepSeek supports low / high / max; medium and xhigh map to high.')
                  : editingProvider.compatibility_mode === 'anthropic'
                    ? (locale === 'zh' ? 'Claude 4.6/5 使用 adaptive thinking；较早模型按强度映射 budget_tokens。' : 'Claude 4.6/5 use adaptive thinking; older models map effort to budget_tokens.')
                    : (locale === 'zh' ? '支持值取决于模型；OpenAI 新推理模型可使用 none 到 max。' : 'Support depends on the model; recent OpenAI reasoning models accept none through max.')}</Text>
              </div>
            )}
            <div className="form-field">
              <label>extra_body (JSON)</label>
              <TextArea rows={4} value={JSON.stringify(editingProvider.extra_body || {}, null, 2)} onChange={(e) => {
                try {
                  updateEditingProvider('extra_body', JSON.parse(e.target.value));
                } catch {
                  // Keep the last valid JSON while the user is typing.
                }
              }} style={{ fontFamily: 'monospace', fontSize: 13 }} />
            </div>
          </Space>
        )}
      </Drawer>

      {/* Profile Drawer */}
      <Drawer
        title={editingProfile && payload?.exchange_profiles?.some((p) => p.profile_id === editingProfile.profile_id) ? 'Edit Profile' : t('addProfile')}
        width={isMobile ? '100vw' : 560}
        open={profileDrawerOpen}
        onClose={() => { setProfileDrawerOpen(false); setEditingProfile(null); }}
        extra={
          <Space>
            {editingProfile && payload?.exchange_profiles?.some((p) => p.profile_id === editingProfile.profile_id) && (
              <Button danger onClick={() => deleteProfile(editingProfile.profile_id)}>{t('delete')}</Button>
            )}
            <Button type="primary" onClick={saveProfile}>{t('save')}</Button>
          </Space>
        }
      >
        {editingProfile && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <div className="form-field">
              <label>{t('profileName')}</label>
              <Input value={editingProfile.name} onChange={(e) => updateEditingProfile('name', e.target.value)} />
            </div>
            <div className="form-field">
              <label>Exchange</label>
              <Select value={editingProfile.exchange} options={(payload.options?.exchanges || ['binance', 'okx']).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingProfile('exchange', v)} style={{ width: '100%' }} />
            </div>
            <div className="form-field">
              <label>Market Type</label>
              <Select value={editingProfile.market_type} options={(payload.options?.market_types || ['swap', 'spot']).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingProfile('market_type', v)} style={{ width: '100%' }} />
            </div>
            <SecretField label="API Key" meta={editingProfile.secrets?.api_key} onChange={(v) => updateEditingProfileSecret('api_key', { value: v, clear: false })} onClear={() => updateEditingProfileSecret('api_key', { value: '', clear: true })} />
            <SecretField label="Secret" meta={editingProfile.secrets?.secret} onChange={(v) => updateEditingProfileSecret('secret', { value: v, clear: false })} onClear={() => updateEditingProfileSecret('secret', { value: '', clear: true })} />
            {editingProfile.exchange === 'okx' && (
              <SecretField label="Passphrase" meta={editingProfile.secrets?.passphrase} onChange={(v) => updateEditingProfileSecret('passphrase', { value: v, clear: false })} onClear={() => updateEditingProfileSecret('passphrase', { value: '', clear: true })} />
            )}
          </Space>
        )}
      </Drawer>
    </Space>
  );
}
