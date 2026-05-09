import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

const STORAGE_KEYS = {
  locale: 'crypto-agent-locale',
  theme: 'crypto-agent-theme',
  selectedSymbol: 'crypto-agent-selected-symbol',
};

const messages = {
  en: {
    brand: 'Crypto Agent',
    publicDashboard: 'Dashboard',
    usage: 'Usage',
    console: 'Console',
    chat: 'Chat',
    config: 'Config',
    history: 'History',
    login: 'Sign In',
    logout: 'Logout',
    openConsole: 'Open Console',
    backToDashboard: 'Public Dashboard',
    switchLanguage: 'Language',
    switchTheme: 'Theme',
    light: 'Light',
    dark: 'Dark',
    loading: 'Loading',
    noData: 'No data',
    save: 'Save',
    delete: 'Delete',
    create: 'Create',
    configured: 'Configured',
    notConfigured: 'Not configured',
    clear: 'Clear',
    cancel: 'Cancel',
    confirm: 'Confirm',
    publicHeadline: 'Public trading performance and runtime visibility',
    publicSubhead: 'Follow strategy analysis, positions, orders, K-line activity, and usage metrics from one responsive dashboard.',
    usageHeadline: 'Usage analytics',
    usageSubhead: 'Inspect token, model, and agent-level consumption without entering the console.',
    consoleHeadline: 'Control console',
    timeframe: 'Timeframe',
    compare: 'Compare',
    symbol: 'Symbol',
    summary: 'Summary',
    analysis: 'Analysis',
    strategyLogic: 'Strategy logic',
    positions: 'Positions',
    pendingOrders: 'Open orders',
    recentOrders: 'Recent orders',
    recentTrades: 'Recent trades',
    dailySummaries: 'Daily summaries',
    equityCompare: 'Equity compare',
    scheduler: 'Scheduler',
    symbolMode: 'Symbol mode',
    symbolFreq: 'Symbol frequency',
    agents: 'Agents',
    totalTokens: '14d tokens',
    totalCost: 'Total cost',
    trackedModels: 'Models',
    trackedAgents: 'Tracked agents',
    totalTrades: 'Total trades',
    totalPnl: 'Total PnL',
    winRate: 'Win rate',
    latestEquity: 'Latest equity',
    latestDay: 'Latest day',
    modelPricing: 'Pricing',
    prompts: 'Prompts',
    runtimeConfig: 'Runtime config',
    globals: 'Global settings',
    agentConfigs: 'Agent configs',
    addAgent: 'Add agent',
    duplicateAgent: 'Duplicate',
    removeAgent: 'Remove',
    deleteAgentData: 'Delete config and linked data',
    basicSettings: 'Basic settings',
    scheduleSettings: 'Schedule and mode',
    modelSettings: 'Model settings',
    exchangeSettings: 'Exchange credentials',
    summarizerSettings: 'Summarizer',
    advancedSettings: 'Advanced overrides',
    promptEditor: 'Prompt editor',
    pricingEditor: 'Pricing editor',
    saveConfig: 'Save configuration',
    savePrompt: 'Save prompt',
    deletePrompt: 'Delete prompt',
    createSession: 'New session',
    generateTitle: 'Generate title',
    clearMessages: 'Clear messages',
    toolApproval: 'Tool approval required',
    approve: 'Approve',
    reject: 'Reject',
    send: 'Send',
    password: 'Password',
    loginDescription: 'Sign in to manage chat, config, and history.',
    workspace: 'Workspace',
    liveWorkspace: 'Live workspace',
    usageOverview: 'Usage overview',
    modelUsage: 'Model usage',
    agentUsage: 'Agent usage',
    dailyTokens: 'Daily tokens',
    dailyCost: 'Daily cost',
    emptySessions: 'No sessions yet',
    emptyConfig: 'No agent configs yet',
    emptyWorkspace: 'No workspace data available',
    emptyHistory: 'No history data available',
    noActivePositions: 'No active positions',
    noOpenOrders: 'No open orders',
    noSummaries: 'No summaries yet',
    themeLightHint: 'Bright surfaces and soft contrast',
    themeDarkHint: 'Dim surfaces for low-light viewing',
    localeEnHint: 'English labels and content chrome',
    localeZhHint: 'Chinese labels and content chrome',
    setupHeadline: 'Initial setup',
    setupDesc: 'Set bootstrap secrets before using the console in production.',
    dockerDetected: 'Docker runtime detected',
    dockerDesc: 'Environment changes are applied after the container restarts.',
    setupCompleted: 'Setup saved',
    restartBackend: 'Restart the backend or container for security settings to take effect.',
    adminPassword: 'Admin password',
    jwtSecret: 'JWT secret',
    configMasterKey: 'Config master key',
    jwtExpireHours: 'JWT expire hours',
    port: 'Port',
    timezone: 'Timezone',
    runSchedulerInWeb: 'Run scheduler in web process',
    saveSetup: 'Save setup',
  },
  zh: {
    brand: 'Crypto Agent',
    publicDashboard: '公开看板',
    usage: '用量统计',
    console: '控制台',
    chat: '聊天',
    config: '配置',
    history: '历史',
    login: '登录',
    logout: '退出',
    openConsole: '进入控制台',
    backToDashboard: '返回公开看板',
    switchLanguage: '语言',
    switchTheme: '主题',
    light: '亮色',
    dark: '暗色',
    loading: '加载中',
    noData: '暂无数据',
    save: '保存',
    delete: '删除',
    create: '创建',
    configured: '已配置',
    notConfigured: '未配置',
    clear: '清空',
    cancel: '取消',
    confirm: '确认',
    publicHeadline: '公开展示交易表现与运行状态',
    publicSubhead: '在一个响应式看板里查看策略分析、持仓、订单、K 线和用量指标。',
    usageHeadline: '用量分析',
    usageSubhead: '无需进入控制台，也可以查看 token、模型与 agent 的消耗情况。',
    consoleHeadline: '控制台',
    timeframe: '周期',
    compare: '对比',
    symbol: '交易对',
    summary: '摘要',
    analysis: '分析内容',
    strategyLogic: '策略逻辑',
    positions: '持仓',
    pendingOrders: '当前挂单',
    recentOrders: '最近订单',
    recentTrades: '最近成交',
    dailySummaries: '每日总结',
    equityCompare: '权益对比',
    scheduler: '调度器',
    symbolMode: '运行模式',
    symbolFreq: '运行频率',
    agents: 'Agent 数',
    totalTokens: '14 天 Tokens',
    totalCost: '总成本',
    trackedModels: '模型数',
    trackedAgents: 'Agent 数',
    totalTrades: '总交易数',
    totalPnl: '总盈亏',
    winRate: '胜率',
    latestEquity: '最新权益',
    latestDay: '最近一天',
    modelPricing: '计价',
    prompts: 'Prompt',
    runtimeConfig: '运行配置',
    globals: '全局设置',
    agentConfigs: 'Agent 配置',
    addAgent: '新增 Agent',
    duplicateAgent: '复制',
    removeAgent: '移除',
    deleteAgentData: '删除配置并清理关联数据',
    basicSettings: '基础信息',
    scheduleSettings: '调度与模式',
    modelSettings: '模型配置',
    exchangeSettings: '交易所凭证',
    summarizerSettings: '总结模型',
    advancedSettings: '高级覆盖项',
    promptEditor: 'Prompt 编辑',
    pricingEditor: '计价编辑',
    saveConfig: '保存配置',
    savePrompt: '保存 Prompt',
    deletePrompt: '删除 Prompt',
    createSession: '新建会话',
    generateTitle: '生成标题',
    clearMessages: '清空消息',
    toolApproval: '工具调用待审批',
    approve: '批准',
    reject: '拒绝',
    send: '发送',
    password: '密码',
    loginDescription: '登录后可管理聊天、配置和历史。',
    workspace: '工作区',
    liveWorkspace: '实时工作区',
    usageOverview: '用量概览',
    modelUsage: '模型用量',
    agentUsage: 'Agent 用量',
    dailyTokens: '每日 Token',
    dailyCost: '每日成本',
    emptySessions: '暂无会话',
    emptyConfig: '暂无 Agent 配置',
    emptyWorkspace: '暂无工作区数据',
    emptyHistory: '暂无历史数据',
    noActivePositions: '暂无持仓',
    noOpenOrders: '暂无挂单',
    noSummaries: '暂无总结',
    themeLightHint: '明亮背景与柔和对比',
    themeDarkHint: '低光环境下更舒适',
    localeEnHint: '界面标签使用英文',
    localeZhHint: '界面标签使用中文',
  },
};

const PreferencesContext = createContext(null);

function detectLocale() {
  if (typeof window === 'undefined') {
    return 'en';
  }
  const saved = window.localStorage.getItem(STORAGE_KEYS.locale);
  if (saved === 'en' || saved === 'zh') {
    return saved;
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

function detectTheme() {
  if (typeof window === 'undefined') {
    return 'light';
  }
  const saved = window.localStorage.getItem(STORAGE_KEYS.theme);
  if (saved === 'light' || saved === 'dark') {
    return saved;
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function detectSelectedSymbol() {
  if (typeof window === 'undefined') {
    return '';
  }
  return window.localStorage.getItem(STORAGE_KEYS.selectedSymbol) || '';
}

export function PreferencesProvider({ children }) {
  const [locale, setLocale] = useState(detectLocale);
  const [theme, setTheme] = useState(detectTheme);
  const [selectedSymbol, setSelectedSymbol] = useState(detectSelectedSymbol);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.locale, locale);
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
  }, [locale]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.theme, theme);
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    if (selectedSymbol) {
      window.localStorage.setItem(STORAGE_KEYS.selectedSymbol, selectedSymbol);
    } else {
      window.localStorage.removeItem(STORAGE_KEYS.selectedSymbol);
    }
  }, [selectedSymbol]);

  const value = useMemo(() => {
    const dictionary = messages[locale] || messages.en;
    return {
      locale,
      setLocale,
      theme,
      setTheme,
      selectedSymbol,
      setSelectedSymbol,
      isDark: theme === 'dark',
      t(key) {
        return dictionary[key] || messages.en[key] || key;
      },
    };
  }, [locale, theme, selectedSymbol]);

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences() {
  const context = useContext(PreferencesContext);
  if (!context) {
    throw new Error('usePreferences must be used within PreferencesProvider');
  }
  return context;
}
