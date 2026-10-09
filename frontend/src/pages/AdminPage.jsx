import McpSettingsPanel from '../components/McpSettingsPanel';
import { PriceSyncPanel, ProviderPricingFields } from '../components/ProviderPricingFields';
import RunScheduleEditor from '../components/RunScheduleEditor';
import SpotScheduleEditor from '../components/SpotScheduleEditor';
import NewsSettingsPanel from '../components/NewsSettingsPanel';
import ScorerProviderFields from '../components/ScorerProviderFields';
import { JEV_PROVIDER_PRESETS } from '../lib/scorerProvider';
import DatabaseMaintenance from '../components/DatabaseMaintenance';
import ResponsiveTabs from '../components/ResponsiveTabs';
import TradingRulesPanel from '../components/TradingRulesPanel';
import AgentRunsPanel from '../components/AgentRunsPanel';
import SpotSymbolPicker from '../components/SpotSymbolPicker';
import MarketSymbolPicker from '../components/MarketSymbolPicker';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Divider,
  Drawer,
  Empty,
  Grid,
  Input,
  InputNumber,
  List,
  Modal,
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
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, DownloadOutlined, HolderOutlined, FileTextOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { EXIT_MODES, exitModeLabel, resolveExitMode } from '../lib/exitManagement';
import { usePreferences } from '../app/usePreferences';
import { ShortMemoryPanel } from '../components/AgentWorkspace';

const { TextArea } = Input;
const { Title, Paragraph, Text } = Typography;
const { useBreakpoint } = Grid;
const ADMIN_TAB_STORAGE_KEY = 'crypto-agent-admin-active-tab';
const ADMIN_TAB_KEYS = ['tasks', 'intelligence', 'providers', 'exchanges', 'mcp', 'memory', 'runtime', 'agent-runs', 'prompts', 'database', 'importexport'];

const DEFAULT_STRATEGY_PROMPT = '请把以下单轮交易分析压缩为精炼的中文策略摘要。合并重复分析，保留趋势判断、关键价位、风险点、持仓/挂单意图、实际执行结果和下一步条件。不要为字数目标截断条件或结果。只输出摘要文本。\n\n内容：\n{content}';
const DEFAULT_SHORT_MEMORY_PROMPT = '请整理旧记忆并复盘本窗口，完整保留尚有效的条件、实际结果和未解决问题，不限制字数。以下是提供的证据：\n{content}';

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

Recent Decisions:
{recent_summaries_text}

Trading Rules (read-only for the trading agent):
{trading_rules_text}

简述决策、关键依据和必要交易参数。需要操作时调用可用工具，按回执说明结果；等待时写明下次行动条件。`;

const MARKET_TIMEFRAME_OPTIONS = ['15m', '30m', '1h', '4h', '1d', '1w', '1M'];
const SPOT_MARKET_TIMEFRAMES = ['4h', '1d', '1w'];

function taskSymbols(task) {
  const symbols = Array.isArray(task?.symbols) ? task.symbols : [task?.symbol];
  return [...new Set(symbols.map((symbol) => String(symbol || '').trim().toUpperCase()).filter(Boolean))];
}

function taskCatalogContext(task, profiles, persistedProfiles) {
  const profile = profiles.find((item) => item.profile_id === task?.exchange_profile_id);
  const persisted = persistedProfiles.find((item) => item.profile_id === profile?.profile_id
    && item.exchange === profile?.exchange && item.market_type === profile?.market_type);
  return {
    exchange: profile?.exchange || (!task?.exchange_profile_id ? task?.exchange : '') || '',
    exchange_profile_id: persisted?.profile_id || undefined,
    market_type: task?.mode === 'SPOT_DCA' ? 'spot' : (task?.market_type || 'swap'),
  };
}

const LLM_PROVIDER_PRESETS = [
  ...JEV_PROVIDER_PRESETS,
  { key: 'openai', label: 'OpenAI / Codex', api_base: 'https://api.openai.com/v1', model: 'gpt-5.6', compatibility_mode: 'openai', thinking_enabled: true, reasoning_effort: 'medium' },
  { key: 'deepseek', label: 'DeepSeek', api_base: 'https://api.deepseek.com', model: 'deepseek-v4-pro', compatibility_mode: 'deepseek', thinking_enabled: true, reasoning_effort: 'high' },
  { key: 'bai-claude', label: 'BAI · Claude', api_base: 'https://api.bankofai.io/v1', model: 'claude-sonnet-5', compatibility_mode: 'openai', thinking_enabled: true, reasoning_effort: 'high' },
  { key: 'bai-gemini', label: 'BAI · Gemini', api_base: 'https://api.b.ai/v1', model: 'gemini-3.8-flash', compatibility_mode: 'openai', thinking_enabled: true, reasoning_effort: 'high' },
];

function buildBlankSecretMeta() {
  return { configured: false, masked_value: '', value: '', clear: false };
}

function buildBlankAgent() {
  const now = Date.now();
  return {
    config_id: `agent-${now}`,
    title: '',
    symbol: '',
    enabled: true,
    mode: 'STRATEGY',
    exit_mode: 'attached_required',
    model: '',
    api_base: '',
    temperature: 0.3,
    prompt_file: '',
    market_timeframes: ['1h', '4h', '1d'],
    market_profile: 'hourly',
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
    fallback_llm_provider_ids: [],
    summarizer_provider_id: '',
    exchange_profile_id: '',
    strategy_prompt: DEFAULT_STRATEGY_PROMPT,
    short_memory_prompt: DEFAULT_SHORT_MEMORY_PROMPT,
    system_prompt_role: 'system',
    summarizer: {
      model: '',
      api_base: '',
      temperature: 0.3,
      strategy_prompt: DEFAULT_STRATEGY_PROMPT,
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
    input_price_per_m: null,
    output_price_per_m: null,
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
        <Input
          value={meta?.value || ''}
          onChange={(event) => onChange(event.target.value)}
          placeholder={t('notConfigured')} autoComplete="off" spellCheck={false}
        />
        <Button disabled={!meta?.value} onClick={() => navigator.clipboard.writeText(meta.value)}>{t('copy')}</Button>
        <Button onClick={onClear}>{t('clear')}</Button>
      </Space.Compact>
    </div>
  );
}

function ProviderSelect({ providers, value, onChange, allowEmpty, emptyLabel }) {
  const { t } = usePreferences();
  const options = (providers || []).filter((p) => p.api_protocol !== 'decisions').map((p) => ({
    label: `${p.name || p.provider_id} (${p.model || '-'})`,
    value: p.provider_id,
  }));
  if (allowEmpty) {
    options.unshift({ label: emptyLabel || t('noProvider'), value: '' });
  }
  return <Select value={value || ''} options={options} onChange={onChange} style={{ width: '100%' }} />;
}

function ProfileSelect({ profiles, value, onChange, allowEmpty, emptyLabel, ariaLabel }) {
  const { t } = usePreferences();
  const options = (profiles || []).map((p) => ({
    label: `${p.name || p.profile_id} (${p.exchange}/${p.market_type})`,
    value: p.profile_id,
  }));
  if (allowEmpty) {
    options.unshift({ label: emptyLabel || t('noProfile'), value: '' });
  }
  return <Select aria-label={ariaLabel} value={value || ''} options={options} onChange={onChange} style={{ width: '100%' }} />;
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

function PromptCodeEditor({ value, onChange, placeholder, height = 420, onSave, disabled = false }) {
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
            aria-label="Prompt editor"
            disabled={disabled}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
                event.preventDefault(); onSave?.();
              }
            }}
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
  'recent_summaries_text', 'trading_rules_text',
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
    if (typeof window === 'undefined') return 'tasks';
    const saved = window.localStorage.getItem(ADMIN_TAB_STORAGE_KEY);
    return ADMIN_TAB_KEYS.includes(saved) ? saved : 'tasks';
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [payload, setPayload] = useState(null);
  const payloadRef = useRef(null);
  const revisionRef = useRef(0);
  const saveQueueRef = useRef(Promise.resolve());
  const pendingSavesRef = useRef(0);
  const lastSavedRevisionRef = useRef(-1);
  const [persistedProfiles, setPersistedProfiles] = useState([]);
  const [persistedProviders, setPersistedProviders] = useState([]);
  const [persistedAgents, setPersistedAgents] = useState([]);
  const [saveState, setSaveState] = useState('idle');
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [promptContent, setPromptContent] = useState('');
  const [savedPrompt, setSavedPrompt] = useState('');
  const [promptLoading, setPromptLoading] = useState(false);
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [promptQuery, setPromptQuery] = useState('');
  const promptDirty = promptContent !== savedPrompt;
  const updatePromptContent = (content) => {
    setPromptContent(content);
    try { sessionStorage.setItem(`crypto-prompt-draft:${selectedPrompt}`, content); } catch { /* Editor remains usable when storage is full. */ }
  };
  useEffect(() => {
    if (!promptDirty) return undefined;
    const warn = (event) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [promptDirty]);
  const selectPrompt = (name) => {
    if (name === selectedPrompt) return;
    if (promptDirty) Modal.confirm({ title: locale === 'zh' ? '放弃未保存的 Prompt 修改？' : 'Discard unsaved prompt changes?', onOk: () => { try { sessionStorage.removeItem(`crypto-prompt-draft:${selectedPrompt}`); } catch { /* Storage unavailable. */ } setSelectedPrompt(name); } });
    else setSelectedPrompt(name);
  };
  const [newPromptName, setNewPromptName] = useState('');

  // Task (agent) drawer state
  const [taskDrawerOpen, setTaskDrawerOpen] = useState(false);
  const [editingTask, setEditingTask] = useState(null);
  const [editingTaskId, setEditingTaskId] = useState(null);
  const [taskSaving, setTaskSaving] = useState(false);
  const [deletingTaskId, setDeletingTaskId] = useState(null);
  const deletingTaskRef = useRef(false);
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
  const [exportingDb, setExportingDb] = useState(false);

  const loadAll = useCallback(async () => {
    const revision = revisionRef.current;
    setLoading(true);
    setError('');
    try {
      const response = await api.get('/config');
      if (revision !== revisionRef.current) return;
      payloadRef.current = response.data;
      setPayload(response.data);
      setPersistedProfiles(response.data.exchange_profiles || []);
      setPersistedProviders(response.data.llm_providers || []);
      setPersistedAgents(response.data.agents || []);
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
      if (!selectedPrompt) { setPromptContent(''); setSavedPrompt(''); return; }
      setPromptLoading(true);
      try {
        const response = await api.get('/config/prompts/content', { params: { name: selectedPrompt } });
        if (mounted) {
          let draft = null;
          try { draft = sessionStorage.getItem(`crypto-prompt-draft:${selectedPrompt}`); } catch { /* Storage unavailable. */ }
          setPromptContent(draft ?? response.data.content ?? '');
          setSavedPrompt(response.data.content || '');
        }
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load prompt');
      } finally { if (mounted) setPromptLoading(false); }
    }
    fetchPromptContent();
    return () => { mounted = false; };
  }, [selectedPrompt]);

  const updatePayload = (updater) => {
    if (!payloadRef.current || deletingTaskRef.current) return;
    const next = updater(payloadRef.current);
    payloadRef.current = next;
    revisionRef.current += 1;
    setSaveState('unsaved');
    setPayload(next);
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

  const persistConfig = useCallback((targetPayload, reload = false, revision = revisionRef.current) => {
    if (!targetPayload || deletingTaskRef.current) return Promise.resolve(false);
    pendingSavesRef.current += 1;
    setSaving(true);
    setError('');
    setSaveState('saving');
    // Serialize writes: a slow earlier autosave must never overwrite a newer
    // task/profile edit, or mark a newer unsaved revision as saved.
    const persist = async () => {
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
        lastSavedRevisionRef.current = revision;
        setPersistedProfiles(targetPayload.exchange_profiles || []);
        setPersistedProviders(targetPayload.llm_providers || []);
        setPersistedAgents(targetPayload.agents || []);
        if (revision === revisionRef.current) setError('');
        if (reload && revision === revisionRef.current) await loadAll();
        return true;
      } catch (err) {
        if (revision === revisionRef.current) setError(err.message || 'Failed to save config');
        return false;
      } finally {
        pendingSavesRef.current -= 1;
        setSaving(pendingSavesRef.current > 0);
        setSaveState(pendingSavesRef.current > 0 ? 'saving'
          : lastSavedRevisionRef.current === revisionRef.current ? 'saved'
            : revision === revisionRef.current ? 'failed' : 'unsaved');
      }
    };
    const operation = saveQueueRef.current.then(persist, persist);
    saveQueueRef.current = operation;
    return operation;
  }, [loadAll]);

  const saveConfig = async () => {
    if (autosaveTimerRef.current) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    await persistConfig(payload, true);
  };

  useEffect(() => {
    if (!payload || loading || taskSaving || deletingTaskId || saveState !== 'unsaved') return undefined;
    if (autosaveTimerRef.current) window.clearTimeout(autosaveTimerRef.current);
    const snapshot = payload;
    const revision = revisionRef.current;
    autosaveTimerRef.current = window.setTimeout(() => {
      persistConfig(snapshot, false, revision);
    }, 800);
    return () => {
      if (autosaveTimerRef.current) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [payload, loading, taskSaving, deletingTaskId, saveState, persistConfig]);

  // --- Task (Agent) CRUD ---
  const openAddTask = () => {
    const blank = buildBlankAgent();
    setEditingTask(blank);
    setEditingTaskId(null);
    setTaskDrawerOpen(true);
  };

  const openEditTask = (agent) => {
    const initialQty = Number(agent.initial_qty || 0);
    const manualAvg = Number(agent.manual_avg_cost || 0);
    setEditingTask({
      ...agent,
      symbols: taskSymbols(agent),
      market_timeframes: agent.market_timeframes?.length ? agent.market_timeframes : (agent.mode === 'SPOT_DCA' ? [...SPOT_MARKET_TIMEFRAMES] : [...MARKET_TIMEFRAME_OPTIONS]),
      exit_mode: resolveExitMode(agent),
      fallback_llm_provider_ids: Array.isArray(agent.fallback_llm_provider_ids) ? agent.fallback_llm_provider_ids : [],
      manual_avg_cost: manualAvg || (initialQty > 0 ? Number(agent.initial_cost || 0) / initialQty : 0),
    });
    setEditingTaskId(agent.config_id);
    setTaskDrawerOpen(true);
  };

  const normalizeTaskForSave = (task) => {
    const next = { ...task };
    next.prompt_file = next.prompt_file || '';
    next.fallback_llm_provider_ids = (next.fallback_llm_provider_ids || []).filter(Boolean);
    if (String(next.mode || '').toUpperCase() === 'SPOT_DCA') {
      next.symbols = taskSymbols(next);
      next.symbol = next.symbols[0] || '';
      next.market_type = 'spot';
      const qty = Number(next.initial_qty || 0);
      const avg = Number(next.manual_avg_cost || 0);
      if (qty > 0 && avg > 0) {
        next.initial_cost = Number((qty * avg).toFixed(8));
      }
    } else {
      delete next.symbols;
    }
    return next;
  };

  const saveTask = async () => {
    if (!editingTask || taskSaving || deletingTaskRef.current) return;
    const taskToSave = normalizeTaskForSave(editingTask);
    if (taskToSave.mode === 'SPOT_DCA') {
      const symbols = taskToSave.symbols;
      if (!symbols.length || symbols.length > 10 || symbols.some((symbol) => !/^[A-Z0-9][A-Z0-9._-]*\/[A-Z0-9][A-Z0-9._-]*$/.test(symbol) || symbol.split('/')[0] === symbol.split('/')[1]) || new Set(symbols.map((symbol) => symbol.split('/')[1])).size > 1) {
        message.error(locale === 'zh' ? '请选择 1–10 个相同计价币的现货标的。' : 'Choose 1–10 spot markets sharing one quote currency.');
        return;
      }
      const profile = (payload.exchange_profiles || []).find((item) => item.profile_id === taskToSave.exchange_profile_id);
      if ((taskToSave.exchange_profile_id && !profile) || (profile && profile.market_type !== 'spot')) {
        message.error(locale === 'zh' ? '现货任务请选择 spot 类型的交易所配置。' : 'Select a spot exchange profile for this task.');
        return;
      }
      if (!profile && !taskToSave.exchange) {
        message.error(locale === 'zh' ? '请先选择现货交易所配置。' : 'Choose a spot exchange profile first.');
        return;
      }
    }
    const invalidRule = (taskToSave.run_schedule || []).findIndex(rule => (
      !rule.days?.length || !/^([01]\d|2[0-3]):[0-5]\d$/.test(rule.start)
      || !/^(([01]\d|2[0-3]):[0-5]\d|24:00)$/.test(rule.end) || rule.start === rule.end
      || !Number.isInteger(rule.interval) || rule.interval < 15 || rule.interval > 1440
    ));
    if (invalidRule >= 0) {
      message.error(locale === 'zh'
        ? `请检查第 ${invalidRule + 1} 条时段的星期、时间及间隔；全天请填 00:00—24:00。`
        : `Check weekdays, times and interval in rule ${invalidRule + 1}; use 00:00–24:00 for a full day.`);
      return;
    }

    const required = [
      ['config_id', 'Config ID'],
      ['symbol', t('symbol')],
      ['mode', 'Mode'],
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
    setTaskSaving(true);
    try {
      const original = persistedAgents.find((agent) => agent.config_id === taskToSave.config_id);
      const originalCatalog = taskCatalogContext(original, persistedProfiles, persistedProfiles);
      const currentCatalog = taskCatalogContext(taskToSave, payload.exchange_profiles || [], persistedProfiles);
      const targetsChanged = !original || original.mode !== taskToSave.mode
        || taskSymbols(original).join(',') !== taskSymbols(taskToSave).join(',')
        || (original.exchange_profile_id || '') !== (taskToSave.exchange_profile_id || '')
        || originalCatalog.exchange !== currentCatalog.exchange
        || originalCatalog.market_type !== currentCatalog.market_type
        || (original.enabled === false && taskToSave.enabled !== false);
      // Existing tasks remain editable during an exchange outage. Creation,
      // target/account changes, and re-enabling still require verification.
      if (targetsChanged) {
        const requestedSymbols = taskSymbols(taskToSave);
        const { data } = await api.get('/config/market-symbols', {
          params: { ...currentCatalog, symbols: requestedSymbols.join(','), linear_only: currentCatalog.market_type === 'swap' || undefined, limit: 1 },
          silent: true,
        });
        const invalid = data.invalid_symbols || [];
        const verified = new Set((data.selected_symbols || []).map((item) => item.symbol));
        if (invalid.length || requestedSymbols.some((symbol) => !verified.has(symbol))) {
          message.error(locale === 'zh' ? `存在不可交易的标的，请重新选择：${(invalid.length ? invalid : requestedSymbols.filter((symbol) => !verified.has(symbol))).join('、')}` : 'Some selected markets are unavailable. Choose valid exchange markets.');
          return;
        }
      }
      if (autosaveTimerRef.current) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
      const current = payloadRef.current;
      const agents = [...(current.agents || [])];
      if (editingTaskId) {
        const idx = agents.findIndex((a) => a.config_id === editingTaskId);
        if (idx >= 0) agents[idx] = taskToSave;
      } else {
        agents.push(taskToSave);
      }
      const next = { ...current, agents };
      const revision = ++revisionRef.current;
      if (!await persistConfig(next, false, revision)) {
        message.error(locale === 'zh' ? '任务保存失败，修改已保留，请检查错误后重试。' : 'Task save failed. Your draft is preserved; resolve the error and retry.');
        return;
      }
      payloadRef.current = next;
      setPayload(next);
      setTaskDrawerOpen(false);
      setEditingTask(null);
      setEditingTaskId(null);
    } catch (err) {
      message.error(err.message || (locale === 'zh' ? '标的校验失败，请重试。' : 'Market validation failed. Please retry.'));
    } finally {
      setTaskSaving(false);
    }
  };

  const duplicateTask = (agent) => {
    const copyId = Array.from(crypto.getRandomValues(new Uint32Array(2))).join('-');
    const clone = {
      ...agent,
      config_id: `${agent.config_id}-copy-${copyId}`,
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
    const savedTask = persistedAgents.find((agent) => agent.config_id === editingTaskId);
    if (field === 'symbols' && editingTask?.mode === 'SPOT_DCA'
      && taskSymbols({ symbols: value }).length > 1
      && [editingTask, savedTask].some((task) => task && ['initial_qty', 'initial_cost', 'manual_avg_cost'].some((key) => Number(task[key] || 0) !== 0))) {
      message.error(locale === 'zh'
        ? '当前任务有手工初始持仓或成本，请先核对、清理并保存初始持仓设置，再添加多个标的。'
        : 'This task has manual initial holdings or costs. Review, clear, and save those settings before adding multiple markets.');
      return;
    }
    setEditingTask((prev) => {
      if (!prev) return prev;
      const next = { ...prev, [field]: value };
      if (field === 'mode' && value !== prev.mode) {
        next.market_type = value === 'SPOT_DCA' ? 'spot' : 'swap';
        next.market_timeframes = value === 'SPOT_DCA' ? [...SPOT_MARKET_TIMEFRAMES] : [...MARKET_TIMEFRAME_OPTIONS];
        const profile = (payload.exchange_profiles || []).find((item) => item.profile_id === next.exchange_profile_id);
        if (profile && profile.market_type !== next.market_type) { next.exchange_profile_id = ''; next.exchange = ''; }
        if (value === 'SPOT_DCA') { next.symbols = []; next.symbol = ''; }
        else { delete next.symbols; next.symbol = ''; }
      }
      if (field === 'exchange_profile_id' && value !== prev.exchange_profile_id && next.mode !== 'SPOT_DCA') {
        const profile = (payload.exchange_profiles || []).find((item) => item.profile_id === value);
        next.exchange = profile?.exchange || '';
        next.symbol = '';
      }
      if (String(next.mode || '').toUpperCase() === 'SPOT_DCA') {
        if (field === 'exchange_profile_id' && value !== prev.exchange_profile_id) {
          const profile = (payload.exchange_profiles || []).find((item) => item.profile_id === value);
          next.exchange = profile?.exchange || '';
          next.symbols = [];
          next.symbol = '';
          next.initial_qty = 0;
          next.initial_cost = 0;
          next.manual_avg_cost = 0;
        }
        if (field === 'symbols') {
          next.symbols = [...new Set(value.map((symbol) => String(symbol).trim().toUpperCase()).filter(Boolean))];
          next.symbol = next.symbols[0] || '';
          if (next.symbols.length > 1) {
            next.initial_qty = 0;
            next.initial_cost = 0;
            next.manual_avg_cost = 0;
          }
        }
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
    if (deletingTaskRef.current || taskSaving) return;
    deletingTaskRef.current = true;
    setDeletingTaskId(configId);
    setError('');
    if (autosaveTimerRef.current) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    try {
      // Drain older writes before deleting so an autosave cannot recreate the task.
      await saveQueueRef.current;
      await api.delete(`/config/${encodeURIComponent(configId)}`);
      const hasUnsavedChanges = lastSavedRevisionRef.current !== revisionRef.current;
      const next = {
        ...payloadRef.current,
        agents: (payloadRef.current.agents || []).filter((agent) => agent.config_id !== configId),
      };
      payloadRef.current = next;
      const revision = ++revisionRef.current;
      setPayload(next);
      setPersistedAgents((agents) => agents.filter((agent) => agent.config_id !== configId));
      if (!hasUnsavedChanges) lastSavedRevisionRef.current = revision;
      setSaveState(hasUnsavedChanges ? 'unsaved' : 'saved');
      if (editingTaskId === configId) {
        setTaskDrawerOpen(false);
        setEditingTask(null);
        setEditingTaskId(null);
      }
      message.success(locale === 'zh' ? '任务及关联数据已删除。' : 'Task and linked data deleted.');
    } catch (err) {
      const detail = err.message || (locale === 'zh' ? '请稍后重试。' : 'Please try again.');
      const text = `${locale === 'zh' ? '删除任务失败：' : 'Failed to delete task: '}${detail}`;
      setError(text);
      message.error(text, 8);
    } finally {
      deletingTaskRef.current = false;
      setDeletingTaskId(null);
    }
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
      api_protocol: preset.api_protocol || 'chat',
      decisions_api: preset.decisions_api || 'bai',
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
    if (!selectedPrompt || promptSaving || promptLoading) return;
    setPromptSaving(true);
    const content = promptContent;
    try {
      await api.put('/config/prompts', { name: selectedPrompt, content });
      setSavedPrompt(content);
      try { sessionStorage.removeItem(`crypto-prompt-draft:${selectedPrompt}`); } catch { /* Storage unavailable. */ }
      message.success(locale === 'zh' ? 'Prompt 已保存' : 'Prompt saved');
    } catch (err) { setError(err.message); }
    finally { setPromptSaving(false); }
  };

  const deletePrompt = async () => {
    if (!selectedPrompt) return;
    try {
      await api.delete('/config/prompts', { data: { name: selectedPrompt } });
      try { sessionStorage.removeItem(`crypto-prompt-draft:${selectedPrompt}`); } catch { /* Storage unavailable. */ }
      setSelectedPrompt('');
      setPromptContent('');
      setSavedPrompt('');
      await loadAll();
    } catch (err) { setError(err.message); }
  };

  const createPrompt = async () => {
    const name = newPromptName.trim();
    if (!name) return;
    const filename = name.endsWith('.txt') ? name : `${name}.txt`;
    if (payload.prompts?.files?.includes(filename)) { message.error(locale === 'zh' ? '文件已存在，请选择后编辑' : 'File already exists'); return; }
    if (promptDirty) { message.warning(locale === 'zh' ? '请先保存当前 Prompt' : 'Save the current prompt first'); return; }
    try {
      await api.put('/config/prompts', { name: filename, content: DEFAULT_PROMPT_FILE_CONTENT });
      setNewPromptName('');
      await loadAll();
      setSelectedPrompt(filename);
    } catch (err) { setError(err.message); }
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

  const handleExportDatabase = async () => {
    setExportingDb(true);
    try {
      await api.post('/config/database/download-ticket');
      const a = document.createElement('a');
      a.href = '/api/config/database/download';
      a.download = 'trading_data.db';
      document.body.appendChild(a);
      a.click();
      a.remove();
      message.success(locale === 'zh' ? '已交给浏览器下载，快照准备后可在下载列表查看进度' : 'Download requested; track progress in your browser after snapshot preparation');
    } catch (err) {
      setError(err.message || 'Database export failed');
    } finally {
      setExportingDb(false);
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
      [agent.llm_provider_id, agent.summarizer_provider_id, ...(agent.fallback_llm_provider_ids || [])]
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
  const taskCatalog = taskCatalogContext(editingTask, payload?.exchange_profiles || [], persistedProfiles);
  const multiSpotTask = taskMode === 'SPOT_DCA' && taskSymbols(editingTask).length > 1;
  const taskDefaultTimeframes = taskMode === 'SPOT_DCA' ? SPOT_MARKET_TIMEFRAMES : MARKET_TIMEFRAME_OPTIONS;
  const memoryDashboard = payload ? {
    current_symbol: '',
    symbols: Array.from(new Set((payload.agents || []).flatMap(taskSymbols))),
    agent_summaries: (payload.agents || []).map((agent) => ({
      config_id: agent.config_id,
      symbol: agent.symbol,
      symbols: taskSymbols(agent),
      mode: agent.mode,
      model: agent.model,
      enabled: agent.enabled,
    })),
  } : null;

  return (
    <Space className="admin-page" direction="vertical" size="large" style={{ width: '100%' }} inert={Boolean(deletingTaskId) || undefined}>
      <Card className="hero-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Title level={2} style={{ margin: 0 }}>{t('config')}</Title>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {locale === 'zh' ? '管理任务、模型和交易所。运行配置会自动保存，提示词需单独保存。' : 'Manage tasks, models and exchanges. Runtime changes save automatically; save prompts separately.'}
            </Paragraph>
          </div>
          <Space>
            <Tag color={saveState === 'failed' ? 'red' : saveState === 'saving' ? 'blue' : saveState === 'unsaved' ? 'gold' : saveState === 'saved' ? 'green' : 'default'}>
              {locale === 'zh'
                ? ({ failed: '保存失败', saving: '保存中…', unsaved: '待保存', saved: '已保存' }[saveState] || '自动保存')
                : ({ failed: 'Save failed', saving: 'Saving…', unsaved: 'Unsaved', saved: 'Saved' }[saveState] || 'Auto save')}
            </Tag>
            <Button onClick={saveConfig} loading={saving} disabled={!payload || Boolean(deletingTaskId)}>{t('saveConfig')}</Button>
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon closable onClose={() => setError('')} /> : null}

      {loading ? (
        <Card className="panel-card loading-card"><Spin /></Card>
      ) : payload ? (
        <ResponsiveTabs tabPosition={isMobile ? 'top' : 'left'} label={locale === 'zh' ? '配置分区' : 'Configuration section'} activeKey={activeAdminTab} onChange={setActiveAdminTab} items={[
          {
            key: 'intelligence',
            label: locale === 'zh' ? '消息聚合' : 'News aggregation',
            children: <NewsSettingsPanel value={payload.globals.news} onChange={(value) => updateGlobal('news', value)} providers={payload.llm_providers} profiles={persistedProfiles} agents={payload.agents} polymarket={payload.globals.polymarket} onPolymarketChange={(value) => updateGlobal('polymarket', value)} blockbeatsKey={payload.globals.secrets?.global_blockbeats_api_key} onBlockbeatsKeyChange={(patch) => updateGlobalSecret('global_blockbeats_api_key', patch)} />,
          },
          // ===== 运行配置 =====
          {
            key: 'runtime',
            label: locale === 'zh' ? '系统设置' : 'System settings',
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
                    <label>{locale === 'zh' ? '摘要/记忆等辅助调用远程追踪' : 'Trace auxiliary summary/memory calls'}</label>
                    <Switch checked={payload.globals.langchain_background_tracing === true} onChange={(c) => updateGlobal('langchain_background_tracing', c)} />
                    <Text type="secondary">{locale === 'zh' ? '默认关闭，仍保留本地调用与费用记录；Agent 决策和聊天保留完整追踪。消息处理在消息聚合中单独设置。' : 'Off by default; local call/cost records remain. Agent decisions and chats keep full tracing. News tracing is configured separately.'}</Text>
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
                  <div className="form-field">
                    <label>{locale === 'zh' ? '全局复盘 / 归档模型' : 'Global review / archive model'}</label>
                    <Input value={payload.globals.global_summarizer_model || ''} onChange={(e) => updateGlobal('global_summarizer_model', e.target.value)} placeholder="gpt-4.1-mini" />
                  </div>
                  <div className="form-field">
                    <label>{locale === 'zh' ? '全局摘要 API Base' : 'Global summarizer API Base'}</label>
                    <Input value={payload.globals.global_summarizer_api_base || ''} onChange={(e) => updateGlobal('global_summarizer_api_base', e.target.value)} placeholder="https://api.openai.com/v1" />
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
                  <SecretField label={locale === 'zh' ? '全局摘要 API Key' : 'Global summarizer API Key'} meta={payload.globals.secrets.global_summarizer_api_key} onChange={(v) => updateGlobalSecret('global_summarizer_api_key', { value: v, clear: false })} onClear={() => updateGlobalSecret('global_summarizer_api_key', { value: '', clear: true })} />
                </div>
              </Card>
            ),
          },
          // ===== 任务配置 =====
          {
            key: 'tasks',
            label: locale === 'zh' ? '任务与调度' : 'Tasks and scheduling',
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
                          <Text type="secondary">{t('symbol')}</Text><Text>{taskSymbols(record).join(', ') || '-'}</Text>
                          <Text type="secondary">Mode</Text><Tag>{record.mode}</Tag>
                          <Text type="secondary">{locale === 'zh' ? '决策模型' : 'Model'}</Text>
                          <div>
                            <Tag color="blue">{getProviderInfo(record.llm_provider_id)?.name || record.model || '-'}</Tag>
                            {(record.fallback_llm_provider_ids || []).length > 0 && (
                              <Tag color="cyan">+{record.fallback_llm_provider_ids.length} {locale === 'zh' ? '兜底' : 'fallbacks'}</Tag>
                            )}
                          </div>
                          <Text type="secondary">Prompt</Text><Text className="text-break">{record.prompt_file || '-'}</Text>
                        </div>
                        <Space className="admin-mobile-actions" wrap>
                          <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveTask(record.config_id, -1)} />
                          <Button size="small" icon={<ArrowDownOutlined />} disabled={index === (payload.agents || []).length - 1} onClick={() => moveTask(record.config_id, 1)} />
                          <Button size="small" onClick={() => openEditTask(record)}>Edit</Button>
                          <Button size="small" onClick={() => duplicateTask(record)}>Copy</Button>
                          <Popconfirm title={t('confirmDelete')} description={locale === 'zh' ? '删除此任务及本系统关联的运行、历史数据？删除不会卖出持仓或撤销交易所委托，交易所资产和委托需自行管理。' : 'Delete this task and its local runtime/history data? This will not sell holdings or cancel exchange orders; manage them directly on the exchange.'} onConfirm={() => deleteAgentData(record.config_id)}>
                            <Button size="small" danger loading={deletingTaskId === record.config_id} disabled={taskSaving || Boolean(deletingTaskId && deletingTaskId !== record.config_id)}>{t('delete')}</Button>
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
                      { title: t('symbol'), dataIndex: 'symbol', width: 180, render: (_, record) => taskSymbols(record).join(', ') || '-' },
                      { title: 'Mode', dataIndex: 'mode', width: 120, render: (v) => <Tag>{v}</Tag> },
                      {
                        title: locale === 'zh' ? '决策模型 / 兜底' : 'Model / Fallback',
                        key: 'decision_model',
                        width: 200,
                        render: (_, record) => {
                          const primaryInfo = getProviderInfo(record.llm_provider_id);
                          const primaryLabel = primaryInfo ? (primaryInfo.name || primaryInfo.model) : (record.model || '-');
                          const fallbackCount = (record.fallback_llm_provider_ids || []).length;
                          const fallbackLabels = (record.fallback_llm_provider_ids || []).map((id, idx) => {
                            const fbInfo = getProviderInfo(id);
                            return `${idx + 1}. ${fbInfo ? (fbInfo.name || fbInfo.model) : id || (locale === 'zh' ? '未配置' : 'Not configured')}`;
                          });
                          return (
                            <Space direction="vertical" size={2}>
                              <Tag color="blue">{primaryLabel}</Tag>
                              {fallbackCount > 0 && (
                                <Tooltip title={
                                  <div>
                                    <div style={{ fontWeight: 'bold', marginBottom: 4 }}>
                                      {locale === 'zh' ? '兜底链路：' : 'Fallback Chain:'}
                                    </div>
                                    {fallbackLabels.map((item, i) => (
                                      <div key={i}>{item}</div>
                                    ))}
                                  </div>
                                }>
                                  <Tag color="cyan" style={{ cursor: 'pointer' }}>
                                    +{fallbackCount} {locale === 'zh' ? '级兜底' : 'fallbacks'}
                                  </Tag>
                                </Tooltip>
                              )}
                            </Space>
                          );
                        },
                      },
                      { title: 'Enabled', dataIndex: 'enabled', width: 110, render: (v) => <Tag color={v ? 'green' : 'default'}>{v ? 'ON' : 'OFF'}</Tag> },
                      { title: 'Prompt', dataIndex: 'prompt_file', width: 180, ellipsis: true },
                      {
                        title: '', width: 210, fixed: 'right', render: (_, record) => (
                          <Space className="table-actions">
                            <Button size="small" onClick={() => openEditTask(record)}>Edit</Button>
                            <Button size="small" onClick={() => duplicateTask(record)}>Copy</Button>
                            <Popconfirm
                              title={t('confirmDelete')}
                              description={locale === 'zh' ? '删除此任务及本系统关联的运行、历史数据？删除不会卖出持仓或撤销交易所委托，交易所资产和委托需自行管理。' : 'Delete this task and its local runtime/history data? This will not sell holdings or cancel exchange orders; manage them directly on the exchange.'}
                              onConfirm={() => deleteAgentData(record.config_id)}
                            >
                              <Button size="small" danger loading={deletingTaskId === record.config_id} disabled={taskSaving || Boolean(deletingTaskId && deletingTaskId !== record.config_id)}>{t('delete')}</Button>
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
            label: locale === 'zh' ? '模型与价格' : 'Models and prices',
            children: (
              <div className="settings-stack"><PriceSyncPanel value={payload.globals.pricing_sync} onChange={(value) => updateGlobal('pricing_sync', value)} />
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
                                <Text type="secondary">{locale === 'zh' ? '任务引用' : 'Task usage'}</Text><Text>{[...(new Set((payload.agents || []).filter((agent) => agent.llm_provider_id === record.provider_id || agent.summarizer_provider_id === record.provider_id || (agent.fallback_llm_provider_ids || []).includes(record.provider_id)).map((agent) => agent.config_id)))].length}</Text>
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
                                const usages = (payload.agents || []).filter((agent) => agent.llm_provider_id === record.provider_id || agent.summarizer_provider_id === record.provider_id || (agent.fallback_llm_provider_ids || []).includes(record.provider_id));
                                return usages.length ? <Tooltip title={usages.map((agent) => agent.title || agent.config_id).join(', ')}><Tag color="blue">{usages.length}</Tag></Tooltip> : '-';
                              },
                            },
                            { title: t('thinkingMode'), width: 110, render: (_, record) => record.thinking_enabled === true ? <Tag color="purple">Thinking</Tag> : <Tag>Standard</Tag> },
                            { title: t('reasoningEffort'), dataIndex: 'reasoning_effort', width: 115, render: (value) => value || '-' },
                            { title: locale === 'zh' ? '输入 /M' : 'Input /M', dataIndex: 'input_price_per_m', width: 130, render: (value,row) => { const prices = row.pricing_mode === 'models_dev' ? row.effective_prices : row; const rate = prices?.input_price_per_m; return rate == null ? (locale === 'zh' ? '未定价' : 'Unpriced') : `${rate} ${prices.currency || prices.pricing_currency || 'USD'}`; } },
                            { title: locale === 'zh' ? '输出 /M' : 'Output /M', dataIndex: 'output_price_per_m', width: 130, render: (value,row) => { const prices = row.pricing_mode === 'models_dev' ? row.effective_prices : row; const rate = prices?.output_price_per_m; return rate == null ? (locale === 'zh' ? '未定价' : 'Unpriced') : `${rate} ${prices.currency || prices.pricing_currency || 'USD'}`; } },
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
              </Card></div>
            ),
          },
          // ===== 交易所配置 =====
          {
            key: 'exchanges',
            label: locale === 'zh' ? '交易账户' : 'Trading accounts',
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
          { key: 'mcp', label: 'MCP', children: <McpSettingsPanel profiles={persistedProfiles} /> },
          {
            key: 'memory',
            label: t('memoryCenter'),
            children: (
              <Card className="panel-card" title={t('memoryCenter')}>
                <Tabs
                  items={[
                    {
                      key: 'rules',
                      label: locale === 'zh' ? '交易规则' : 'Trading rules',
                      children: <TradingRulesPanel agents={payload.agents || []} />,
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
            key: 'agent-runs',
            label: locale === 'zh' ? 'Agent 运行' : 'Agent runs',
            children: <AgentRunsPanel agents={payload.agents || []} />,
          },
          {
            key: 'prompts',
            label: t('prompts'),
            children: (
              <div className={`config-editor prompt-config-editor ${promptExpanded ? "is-expanded" : ""}`}>
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
                  <Input.Search aria-label="Search prompts" placeholder={locale === 'zh' ? '搜索 Prompt' : 'Search prompts'} value={promptQuery} onChange={(event) => setPromptQuery(event.target.value)} allowClear />
                  <List
                    className="prompt-file-list"
                    dataSource={(payload.prompts?.files || []).filter((name) => name.toLowerCase().includes(promptQuery.toLowerCase()))}
                    renderItem={(item) => (
                      <List.Item>
                        <button
                          type="button"
                          className={`prompt-file-row ${item === selectedPrompt ? 'active' : ''}`}
                          disabled={promptSaving} onClick={() => selectPrompt(item)}
                        >
                          <FileTextOutlined />
                          <span>{item}</span>
                        </button>
                      </List.Item>
                    )}
                  />
                </Card>
                <Card className="panel-card" title={<Space wrap>{selectedPrompt || t('promptEditor')}<Tag title={locale === 'zh' ? '未保存的编辑会在当前浏览器标签页中保留为草稿' : 'Unsaved edits are retained as drafts in this browser tab'} color={promptDirty ? 'orange' : 'green'}>{promptLoading ? 'Loading…' : promptDirty ? (locale === 'zh' ? '未保存' : 'Unsaved') : (locale === 'zh' ? '已保存' : 'Saved')}</Tag></Space>} extra={<Space wrap><Button type="primary" loading={promptSaving} disabled={!selectedPrompt || promptLoading || !promptDirty} onClick={savePrompt}>{t('savePrompt')}</Button><Button onClick={() => setPromptExpanded(!promptExpanded)}>{promptExpanded ? (locale === 'zh' ? '退出专注' : 'Exit focus') : (locale === 'zh' ? '专注编辑' : 'Focus editor')}</Button></Space>}>
                  <Space direction="vertical" style={{ width: '100%' }} size="middle">
                    <div className="prompt-editor-container">
                      <PromptCodeEditor
                        value={promptContent}
                        onChange={updatePromptContent}
                        placeholder="Use {current_time}, {symbol}, {formatted_market_data}, {positions_text}, {orders_text}, {history_text}, {short_memory_text}"
                        height={promptExpanded ? "calc(100dvh - 290px)" : 520}
                        onSave={savePrompt}
                        disabled={promptLoading || promptSaving}
                      />
                      <div className="prompt-editor-stats">
                        <Text type="secondary">{(promptContent || '').split('\n').length} {locale === 'zh' ? '行' : 'lines'}</Text>
                        <Text type="secondary">{(promptContent || '').length} {locale === 'zh' ? '字符' : 'chars'}</Text>
                      </div>
                    </div>
                    <PromptVarHints content={promptContent} vars={AGENT_PROMPT_VARS} locale={locale} />
                    <Space>
                      <Button type="primary" loading={promptSaving} onClick={savePrompt} disabled={!selectedPrompt || promptLoading || !promptDirty}>{t('savePrompt')} · Ctrl/⌘ S</Button>
                      <Popconfirm title={t('confirmDelete')} onConfirm={deletePrompt} disabled={!selectedPrompt}>
                        <Button danger disabled={!selectedPrompt || promptSaving || promptLoading}>{t('deletePrompt')}</Button>
                      </Popconfirm>
                    </Space>
                  </Space>
                </Card>
              </div>
            ),
          },
          // ===== 导入导出 =====
          { key: 'database', label: locale === 'zh' ? '数据库管理' : 'Database', children: <DatabaseMaintenance /> },
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
                  <Divider style={{ margin: '16px 0' }} />
                  <div>
                    <Typography.Title level={5} style={{ marginTop: 0 }}>
                      {locale === 'zh' ? '数据库备份 (.db)' : 'Database Backup (.db)'}
                    </Typography.Title>
                    <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
                      {locale === 'zh'
                        ? '一键导出完整的 SQLite 数据库（包含全部历史平仓交易、委托、记忆、日志等原始表）。导出采用 SQLite 在线热备份，保证事务完整不损坏。'
                        : 'One-click export of the complete SQLite database file (including all closed position history, orders, memory, logs). Uses online backup to ensure transaction consistency.'}
                    </Typography.Paragraph>
                    <Button
                      icon={<DownloadOutlined />}
                      loading={exportingDb}
                      onClick={handleExportDatabase}
                    >
                      {locale === 'zh' ? '一键导出数据库 (.db)' : 'Export Database (.db)'}
                    </Button>
                  </div>
                </Space>
              </Card>
            ),
          },
          // ===== 计价 =====
        ].sort((a,b)=>ADMIN_TAB_KEYS.indexOf(a.key)-ADMIN_TAB_KEYS.indexOf(b.key))} />
      ) : null}

      {/* Task (Agent) Drawer */}
      <Drawer
        title={editingTaskId ? (editingTask?.title || editingTaskId) : (locale === 'zh' ? '新增任务' : 'Add Task')}
        width={isMobile ? '100vw' : 720}
        open={taskDrawerOpen}
        closable={!taskSaving && !deletingTaskId}
        onClose={() => { if (!taskSaving && !deletingTaskId) { setTaskDrawerOpen(false); setEditingTask(null); setEditingTaskId(null); } }}
        extra={
          <Space>
            {editingTaskId && (
              <Popconfirm
                title={t('confirmDelete')}
                description={locale === 'zh' ? '删除此任务及本系统关联的运行、历史数据？删除不会卖出持仓或撤销交易所委托，交易所资产和委托需自行管理。' : 'Delete this task and its local runtime/history data? This will not sell holdings or cancel exchange orders; manage them directly on the exchange.'}
                onConfirm={() => deleteAgentData(editingTaskId)}
              >
                <Button danger loading={deletingTaskId === editingTaskId} disabled={taskSaving || Boolean(deletingTaskId && deletingTaskId !== editingTaskId)}>{t('delete')}</Button>
              </Popconfirm>
            )}
            <Button type="primary" loading={taskSaving} disabled={Boolean(deletingTaskId)} onClick={saveTask}>{t('save')}</Button>
          </Space>
        }
      >
        {editingTask && (
          <div inert={taskSaving || Boolean(deletingTaskId) || undefined}>
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
                    <label>Mode *</label>
                    <Select value={editingTask.mode} options={(payload.options?.modes || []).map((v) => ({ label: v, value: v }))} onChange={(v) => updateEditingTask('mode', v)} style={{ width: '100%' }} />
                  </div>
                  <div className="form-field field-span-2">
                    <label>{locale === 'zh' ? '交易账户' : 'Exchange account'} * · {taskCatalog.market_type === 'spot' ? 'Spot' : 'Swap'}</label>
                    <ProfileSelect ariaLabel={locale === 'zh' ? '交易账户' : 'Exchange account'} profiles={(payload.exchange_profiles || []).filter((profile) => profile.market_type === taskCatalog.market_type)} value={editingTask.exchange_profile_id} onChange={(v) => updateEditingTask('exchange_profile_id', v)} allowEmpty />
                    {!editingTask.exchange_profile_id && editingTask.exchange && <Text type="secondary">{locale === 'zh' ? `使用原任务的 ${editingTask.exchange} 账户配置。选择新的账户后需重新选择标的。` : `Using the task's legacy ${editingTask.exchange} account. Changing the account clears selected markets.`}</Text>}
                    {editingTask.exchange_profile_id && !taskCatalog.exchange_profile_id && taskCatalog.exchange && <Text type="secondary">{locale === 'zh' ? '按当前交易所配置加载标的；保存任务时一并保存账户配置。' : 'Loading markets for the current exchange; the profile will be saved with the task.'}</Text>}
                  </div>
                  <div className={`form-field${taskMode === 'SPOT_DCA' ? ' field-span-2' : ''}`}>
                    <label>{taskMode === 'SPOT_DCA' ? (locale === 'zh' ? '现货标的（最多 10 个）' : 'Spot symbols (up to 10)') : t('symbol')} *</label>
                    {taskMode === 'SPOT_DCA' ? (
                      <SpotSymbolPicker
                        key={`${editingTaskId || 'new'}:${editingTask.exchange_profile_id || 'legacy'}:${taskCatalog.exchange}`}
                        exchange={taskCatalog.exchange}
                        profileId={taskCatalog.exchange_profile_id}
                        value={taskSymbols(editingTask)}
                        onChange={(values) => updateEditingTask('symbols', values)}
                        locale={locale}
                      />
                    ) : <MarketSymbolPicker key={`${editingTaskId || 'new'}:${editingTask.exchange_profile_id || 'legacy'}:${taskCatalog.exchange}:${taskCatalog.market_type}`} profileId={taskCatalog.exchange_profile_id} exchange={taskCatalog.exchange} marketType={taskCatalog.market_type} value={editingTask.symbol} onChange={(value) => updateEditingTask('symbol', value)} locale={locale} />}
                  </div>
                  <div className="form-field">
                    <label>Enabled</label>
                    <Switch checked={editingTask.enabled} onChange={(c) => updateEditingTask('enabled', c)} />
                  </div>
                  {(taskMode === 'REAL' || taskMode === 'STRATEGY') && (
                    <div className="form-field field-span-2">
                      <label>{locale === 'zh' ? '退出管理方式' : 'Exit management'}</label>
                      <Select
                        aria-label={locale === 'zh' ? '退出管理方式' : 'Exit management'}
                        value={resolveExitMode(editingTask)}
                        options={EXIT_MODES.map((value) => ({ value, label: exitModeLabel(value, locale) }))}
                        onChange={(value) => updateEditingTask('exit_mode', value)}
                        style={{ width: '100%' }}
                      />
                      <Text type="secondary">
                        {locale === 'zh'
                          ? ({ attached_required: '每次开仓需提供止盈和止损。', attached_optional: '开仓时可单独提供或省略止盈止损。', independent_exits: '通过独立退出单按数量设置止盈或止损，支持分批减仓。' }[resolveExitMode(editingTask)])
                          : ({ attached_required: 'Each entry requires take profit and stop loss.', attached_optional: 'Entries may supply either, both, or no TP/SL.', independent_exits: 'Separate exits specify price and quantity, supporting partial reductions.' }[resolveExitMode(editingTask)])}
                        {' '}{locale === 'zh' ? '已有持仓、挂单或待核验操作时，需先处理完毕才能切换。' : 'Close positions and resolve outstanding orders before switching modes.'}
                      </Text>
                    </div>
                  )}
                  <div className="form-field">
                    <label>Prompt File</label>
                    <Select
                      aria-label="Prompt File"
                      value={editingTask.prompt_file || ''}
                      options={[
                        { label: locale === 'zh' ? '内置默认（随版本更新）' : 'Built-in default (updated with releases)', value: '' },
                        ...(payload.options?.prompt_files || []).filter(Boolean).map((v) => ({ label: v, value: v })),
                      ]}
                      onChange={(v) => updateEditingTask('prompt_file', v || '')}
                      allowClear
                      style={{ width: '100%' }}
                    />
                    <Text type="secondary">{locale === 'zh' ? '内置默认随版本升级更新；自定义文件保留原有内容。' : 'The built-in default updates with releases; custom files retain their content.'}</Text>
                  </div>
                  {taskMode !== 'SPOT_DCA' && <div className="form-field field-span-2"><label>{locale === 'zh' ? '行情输入预设' : 'Market input preset'}</label><Select value={editingTask.market_profile || 'legacy'} options={[{value:'legacy',label:locale === 'zh'?'保留原有行情配置':'Existing market configuration'},{value:'hourly',label:locale === 'zh'?'1h 分析 · 1h / 4h / 1d':'Hourly analysis · 1h / 4h / 1d'}]} onChange={(v)=>{updateEditingTask('market_profile',v);if(v==='hourly')updateEditingTask('market_timeframes',['1h','4h','1d']);}}/><Text type="secondary">{locale === 'zh'?'1h 预设各周期计算 600 根历史，仅展示最近 24 / 12 / 7 根已收盘 K 线。':'Hourly preset computes indicators from 600 bars per timeframe and displays 24 / 12 / 7 closed bars.'}</Text></div>}
                  <div className="form-field field-span-2">
                    <label>{locale === 'zh' ? '市场分析周期' : 'Market analysis timeframes'}</label>
                    <Select
                      mode="multiple"
                      value={editingTask.market_timeframes || taskDefaultTimeframes}
                      options={(payload.options?.market_timeframes || MARKET_TIMEFRAME_OPTIONS).map((value) => ({ label: value, value }))}
                      onChange={(values) => updateEditingTask('market_timeframes', values.length ? values : [...taskDefaultTimeframes])}
                      style={{ width: '100%' }}
                    />
                    {taskMode === 'SPOT_DCA' && <Text type="secondary">{locale === 'zh' ? '现货默认使用 4h、日线和周线，可按任务覆盖；此处只设置分析周期，定投运行时间在下方配置。' : 'Spot defaults to 4h, daily and weekly analysis. Override per task; execution timing is configured below.'}</Text>}
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
                    <label>{locale === 'zh' ? '默认运行间隔（分钟）' : 'Default interval (minutes)'}</label>
                    <InputNumber min={15} max={1440} precision={0} value={editingTask.run_interval ?? 60} onChange={(v) => updateEditingTask('run_interval', v ?? 60)} style={{ width: '100%' }} />
                  </div>
                  {(taskMode === 'REAL' || taskMode === 'STRATEGY') && (
                    <div className="form-field">
                      <label>Leverage</label>
                      <InputNumber min={1} value={editingTask.leverage ?? 1} onChange={(v) => updateEditingTask('leverage', v ?? 1)} style={{ width: '100%' }} />
                    </div>
                  )}
                  {(taskMode === 'REAL' || taskMode === 'STRATEGY') && (
                    <RunScheduleEditor value={editingTask.run_schedule || []}
                      onChange={rules => updateEditingTask('run_schedule', rules)}
                      onPreset={rules => setEditingTask(previous => ({ ...previous, run_schedule: rules }))} />
                  )}
                  {taskMode === 'SPOT_DCA' && (
                    <>
                      <div className="form-field field-span-2">
                        <Alert type="info" showIcon message={locale === 'zh' ? '组合共用一份定投预算' : 'One shared portfolio budget'} description={locale === 'zh' ? 'DCA Amount 是每次计划运行整个任务可用的额度，多个标的共享，由 Agent 分配；不会按标的数量倍增。多标的任务不支持统一填写历史持仓数量或成本，余额与成交按各标的读取。' : 'DCA Amount is the total allowance per run, shared across all symbols and allocated by the agent. It is not multiplied by symbol count. Multi-symbol tasks read balances and fills per symbol; legacy initial quantity/cost fields are unavailable.'} />
                      </div>
                      <div className="form-field">
                        <label>{locale === 'zh' ? '每次运行组合额度（DCA Amount）' : 'Portfolio allowance per run (DCA Amount)'}</label>
                        <InputNumber min={0} value={editingTask.dca_amount ?? 0} onChange={(v) => updateEditingTask('dca_amount', v ?? 0)} style={{ width: '100%' }} />
                      </div>
                      <div className="form-field">
                        <label>{locale === 'zh' ? '任务累计预算（可选）' : 'Lifetime task budget (optional)'}</label>
                        <InputNumber min={0} value={editingTask.dca_budget ?? null} onChange={(v) => updateEditingTask('dca_budget', v)} style={{ width: '100%' }} placeholder={locale === 'zh' ? '留空不设累计上限' : 'Leave empty for no lifetime cap'} />
                        <Text type="secondary">{locale === 'zh' ? '所有标的累计买入共用此额度，计价币与现货标的一致。' : 'A shared cap on cumulative buys across all symbols, in their quote currency.'}</Text>
                      </div>
                      <div className="form-field field-span-2"><SpotScheduleEditor config={editingTask} onChange={(value) => updateEditingTask('dca_schedule', value)} /></div>
                      <div className="form-field">
                        <label>Initial Qty</label>
                        <InputNumber min={0} value={editingTask.initial_qty ?? 0} onChange={(v) => updateEditingTask('initial_qty', v ?? 0)} style={{ width: '100%' }} disabled={multiSpotTask} />
                      </div>
                      <div className="form-field">
                        <label>Manual Avg Cost</label>
                        <InputNumber min={0} value={editingTask.manual_avg_cost ?? 0} onChange={(v) => updateEditingTask('manual_avg_cost', v ?? 0)} style={{ width: '100%' }} disabled={multiSpotTask} />
                      </div>
                      <div className="form-field">
                        <label>Initial Cost</label>
                        <InputNumber min={0} value={editingTask.initial_cost ?? 0} onChange={(v) => updateEditingTask('initial_cost', v ?? 0)} style={{ width: '100%' }} disabled={multiSpotTask || (Number(editingTask.initial_qty || 0) > 0 && Number(editingTask.manual_avg_cost || 0) > 0)} />
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
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  <Card size="small" title={t('primaryDecisionModel')} type="inner">
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
                  </Card>

                  <Card
                    size="small"
                    title={
                      <Space>
                        <span>{t('fallbackModels')}</span>
                        <Tag color="cyan">{(editingTask.fallback_llm_provider_ids || []).length} {locale === 'zh' ? '级兜底' : 'fallbacks'}</Tag>
                      </Space>
                    }
                    extra={
                      <Button
                        type="dashed"
                        size="small"
                        icon={<PlusOutlined />}
                        onClick={() => {
                          const current = editingTask.fallback_llm_provider_ids || [];
                          updateEditingTask('fallback_llm_provider_ids', [...current, '']);
                        }}
                      >
                        {t('addFallbackModel')}
                      </Button>
                    }
                    type="inner"
                  >
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 16 }}
                      message={t('fallbackChainTip')}
                    />

                    {!(editingTask.fallback_llm_provider_ids || []).length ? (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description={t('noFallbackModels')}
                      >
                        <Button
                          type="primary"
                          size="small"
                          icon={<PlusOutlined />}
                          onClick={() => {
                            updateEditingTask('fallback_llm_provider_ids', ['']);
                          }}
                        >
                          {t('addFallbackModel')}
                        </Button>
                      </Empty>
                    ) : (
                      <div className="fallback-chain-list">
                        {(editingTask.fallback_llm_provider_ids || []).map((fbId, idx) => {
                          const info = getProviderInfo(fbId);
                          return (
                            <div key={`fallback-${idx}`} style={{ marginBottom: 12 }}>
                              <div style={{ textAlign: 'center', margin: '4px 0 8px', color: '#1890ff', fontSize: 12 }}>
                                ↓ {t('fallbackOnError')}（{t('fallbackStep')}{idx + 1}）
                              </div>
                              <Card
                                size="small"
                                style={{ background: 'var(--ant-color-bg-container, #fafafa)', border: '1px solid var(--ant-color-border-secondary, #e8e8e8)' }}
                                title={
                                  <Space>
                                    <Tag color="blue">{t('fallbackStep')}{idx + 1}</Tag>
                                    <span>{info ? (info.name || info.model) : (locale === 'zh' ? '请选择兜底服务商' : 'Select fallback provider')}</span>
                                  </Space>
                                }
                                extra={
                                  <Space size="small">
                                    <Button
                                      size="small"
                                      icon={<ArrowUpOutlined />}
                                      disabled={idx === 0}
                                      onClick={() => {
                                        const list = [...(editingTask.fallback_llm_provider_ids || [])];
                                        const temp = list[idx - 1];
                                        list[idx - 1] = list[idx];
                                        list[idx] = temp;
                                        updateEditingTask('fallback_llm_provider_ids', list);
                                      }}
                                    />
                                    <Button
                                      size="small"
                                      icon={<ArrowDownOutlined />}
                                      disabled={idx === (editingTask.fallback_llm_provider_ids || []).length - 1}
                                      onClick={() => {
                                        const list = [...(editingTask.fallback_llm_provider_ids || [])];
                                        const temp = list[idx + 1];
                                        list[idx + 1] = list[idx];
                                        list[idx] = temp;
                                        updateEditingTask('fallback_llm_provider_ids', list);
                                      }}
                                    />
                                    <Button
                                      size="small"
                                      danger
                                      icon={<DeleteOutlined />}
                                      onClick={() => {
                                        const list = [...(editingTask.fallback_llm_provider_ids || [])];
                                        list.splice(idx, 1);
                                        updateEditingTask('fallback_llm_provider_ids', list);
                                      }}
                                    />
                                  </Space>
                                }
                              >
                                <div className="field-grid">
                                  <div className="form-field field-span-2">
                                    <label>{t('selectProvider')}</label>
                                    <ProviderSelect
                                      providers={payload.llm_providers}
                                      value={fbId}
                                      onChange={(val) => {
                                        const list = [...(editingTask.fallback_llm_provider_ids || [])];
                                        list[idx] = val;
                                        updateEditingTask('fallback_llm_provider_ids', list);
                                      }}
                                      allowEmpty
                                    />
                                  </div>
                                  {info && (
                                    <>
                                      <div className="form-field"><label>Model</label><Input value={info.model} disabled /></div>
                                      <div className="form-field"><label>Temperature</label><InputNumber value={info.temperature} disabled style={{ width: '100%' }} /></div>
                                      <div className="form-field field-span-2"><label>API Base</label><Input value={info.api_base} disabled /></div>
                                    </>
                                  )}
                                </div>
                              </Card>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Card>
                </Space>
              ),
            },
            {
              key: 'summarizer',
              label: locale === 'zh' ? '复盘与归档模型' : 'Review and archive model',
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
                    <Text type="secondary">{locale === 'zh' ? '交易账户和标的在上方基本设置中选择。' : 'Choose the account and markets in Basic settings above.'}</Text>
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
              label: locale === 'zh' ? '策略压缩、复盘与归档 Prompt' : 'Strategy summary, review and archive prompts',
              children: (
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  <Alert type="info" showIcon title={locale === 'zh' ? '每轮交易结束由配置的摘要模型压缩策略，模型返回内容完整保存。' : 'Each trading round uses the configured summarizer to compress the strategy and saves its complete response.'} description={locale === 'zh' ? '单轮策略压缩使用下方 Prompt 和任务摘要模型（如 DeepSeek）；原始分析另行保留。每次运行后，短期记忆结合前 4 小时摘要、当前摘要和现有记忆更新，并维护未锁定规则。' : 'Per-round strategy compression uses the prompt below and the task summarizer, such as DeepSeek; the original analysis is retained separately. After each run, short memory incorporates the prior four hours, the current summary and existing memory, and reviews unlocked rules.'} />
                  <div className="form-field">
                    <label>{t('strategyPrompt')}</label>
                    <Text type="secondary">{locale === 'zh' ? '每轮生成策略摘要时使用。模型归纳压缩后，系统不再按字符数裁切输出；调用失败会明确标记并保留原始记录。' : 'Used for each strategy summary. The model compresses the analysis; its output is saved without character cuts. A failed call is marked explicitly and preserves the original record.'}</Text>
                    <PromptEditor
                      value={editingTask.strategy_prompt || editingTask.summarizer?.strategy_prompt || ''}
                      onChange={(v) => { updateEditingTask('strategy_prompt', v); updateEditingTaskSummarizer('strategy_prompt', v); }}
                      placeholder={DEFAULT_STRATEGY_PROMPT}
                    />
                    <PromptVarHints content={editingTask.strategy_prompt || editingTask.summarizer?.strategy_prompt || ''} vars={SUMMARIZER_PROMPT_VARS} locale={locale} />
                  </div>
                  <div className="form-field">
                    <label>{t('shortMemoryPrompt')}</label>
                    <Text type="secondary">{locale === 'zh' ? '用于记忆整理和规则复盘。复盘权限、人工锁定保护和输出格式由系统提供；这里补充你的整理偏好。' : 'Used for memory consolidation and rule review. The system supplies permissions, human-lock protection and output format; add your review preferences here.'}</Text>
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
          </div>
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
            <div className="form-field"><label>{locale === 'zh' ? '服务接口' : 'API protocol'}</label><Select value={editingProvider.api_protocol || 'chat'} options={[{value:'chat',label:locale === 'zh'?'对话与摘要':'Chat and summaries'},{value:'decisions',label:locale === 'zh'?'Jev 结构化决策':'Jev structured decisions'}]} onChange={(v)=>updateEditingProvider('api_protocol',v)}/></div>
            {editingProvider.api_protocol === 'decisions' && <ScorerProviderFields key={`${editingProvider.provider_id}:${editingProvider.api_base}:${editingProvider.decisions_api}`} value={editingProvider} saved={persistedProviders.find((provider) => provider.provider_id === editingProvider.provider_id)} onChange={(patch) => setEditingProvider((prev) => ({ ...prev, ...patch }))} locale={locale} />}
            {editingProvider.api_protocol !== 'decisions' && <div className="form-field"><label>{locale === 'zh' ? '固定报告输出能力' : 'Report output capability'}</label><Select value={editingProvider.report_output_mode || 'json'} options={[{value:'json',label:locale === 'zh'?'JSON 校验（默认）':'JSON validation (default)'},{value:'json_schema',label:'Native JSON Schema'},{value:'tool',label:locale === 'zh'?'指定报告工具':'Forced report tool'}]} onChange={(v)=>updateEditingProvider('report_output_mode',v)}/><Text type="secondary">{locale === 'zh'?'按渠道实际支持的能力选择；仅在收尾阶段生成报告，格式纠正不重放交易。':'Select the capability supported by this channel. Report repair never repeats trading.'}</Text></div>}
            {editingProvider.api_protocol !== 'decisions' && <><div className="form-field">
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
              <Text type="secondary">{locale === 'zh' ? '若服务商报错或不支持 system role，请选择 User 消息。任务和任务聊天会自动继承。' : 'Choose User message when an endpoint rejects system roles. Task and task chats inherit this setting.'}</Text>
            </div>
            </>}
            <ProviderPricingFields value={editingProvider} onChange={(patch) => setEditingProvider((prev) => ({ ...prev, ...patch }))}/>
            <SecretField label="API Key" meta={editingProvider.secrets?.api_key} onChange={(v) => updateEditingProviderSecret('api_key', { value: v, clear: false })} onClear={() => updateEditingProviderSecret('api_key', { value: '', clear: true })} />
            {editingProvider.api_protocol !== 'decisions' && <><div className="form-field">
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
                <label htmlFor="provider-reasoning-effort">{t('reasoningEffort')}</label>
                <Select
                  id="provider-reasoning-effort"
                  value={editingProvider.reasoning_effort || ''}
                  options={[
                    { value: '', label: locale === 'zh' ? '跟随模型默认' : 'Use model default' },
                    ...(payload.options?.reasoning_efforts || ['none', 'low', 'medium', 'high', 'xhigh', 'max']).filter((v) => !String(editingProvider.model || '').toLowerCase().startsWith('gemini-3.8') || ['low', 'medium', 'high'].includes(v)).map((v) => ({ label: v, value: v })),
                  ]}
                  onChange={(v) => updateEditingProvider('reasoning_effort', v || undefined)}
                  placeholder={locale === 'zh' ? '跟随模型默认' : 'Use model default'}
                  style={{ width: '100%' }}
                />
                <Text type="secondary">{String(editingProvider.model || '').toLowerCase().startsWith('gemini-3.8')
                  ? (locale === 'zh' ? 'Gemini 3.8 支持 low / medium / high。BAI Chat Completions 会返回推理 token 数，但不提供可展示的 reasoning 文本。' : 'Gemini 3.8 supports low / medium / high. BAI Chat Completions reports reasoning-token usage but does not expose displayable reasoning text.')
                  : editingProvider.compatibility_mode === 'deepseek'
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
            </div></>}
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
