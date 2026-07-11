import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Grid,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import {
  ClearOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  MenuOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Bubble, Conversations, Sender, XProvider } from '@ant-design/x';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock from '../components/ReasoningBlock';
import { api, streamSse } from '../lib/api';
import { createChatStreamLifecycle, reduceChatStreamLifecycle } from '../lib/chatStreamLifecycle';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;
const { useBreakpoint } = Grid;
const RUNTIME_STORAGE_KEY = 'crypto-agent-chat-runtime-v1';

function normalizeAssistantDraft(draft) {
  return {
    role: 'assistant',
    content: draft.content || '',
    reasoning_content: draft.reasoning_content || '',
    pending: Boolean(draft.pending),
    draft: true,
  };
}

function hasRenderableMessage(message) {
  if (message.role !== 'assistant') return true;
  return Boolean(message.pending || String(message.content || '').trim() || String(message.reasoning_content || '').trim());
}

function unwrapApproval(approval) {
  if (!approval) return null;
  return approval.value && typeof approval.value === 'object' ? approval.value : approval;
}

function ToolMessage({ content, locale }) {
  return (
    <Collapse
      className="chat-tool-message"
      items={[{
        key: 'tool-result',
        label: locale === 'zh' ? '工具执行结果' : 'Tool result',
        children: <MarkdownBlock content={String(content || '')} />,
      }]}
    />
  );
}

function MessageContent({ content, reasoning, role, streaming, status }) {
  const { t, locale } = usePreferences();
  if (role === 'tool') return <ToolMessage content={content} locale={locale} />;
  const normalized = splitThinkingContent(content || '', reasoning || '');
  if (!normalized.content?.trim() && !normalized.reasoning?.trim()) {
    return streaming ? (
      <div className="chat-waiting-inline">
        <span className="chat-stream-status-dot" />
        <span>{status || (locale === 'zh' ? '正在等待模型响应' : 'Waiting for the model')}</span>
      </div>
    ) : null;
  }
  return (
    <div className={`chat-message-content ${streaming ? 'is-streaming' : ''}`}>
      <MarkdownBlock content={normalized.content || ''} />
      <ReasoningBlock title={t('reasoning')} content={normalized.reasoning} streaming={streaming} />
    </div>
  );
}

function PersistenceWarning({ warning, locale, onDismiss }) {
  if (!warning) return null;
  return (
    <Alert
      className="chat-persistence-warning"
      type="warning"
      showIcon
      closable
      onClose={onDismiss}
      message={locale === 'zh' ? '回答已生成，但暂未保存' : 'Response generated but not saved'}
      description={warning}
    />
  );
}

function ToolApproval({ approval, loading, onApprove, onReject, locale }) {
  const approvalValue = unwrapApproval(approval);
  if (!approvalValue) return null;
  return (
    <Alert
      className="tool-approval-card"
      type="warning"
      showIcon
      icon={<SafetyCertificateOutlined />}
      message={locale === 'zh' ? '工具调用待审批' : 'Tool approval required'}
      description={
        <Collapse
          className="tool-approval-details"
          items={[{
            key: 'tool-details',
            label: approvalValue.tool_name || 'Unknown tool',
            children: <pre className="approval-json">{JSON.stringify(approvalValue.tool_args || {}, null, 2)}</pre>,
          }]}
        />
      }
      action={
        <Space>
          <Button size="small" type="primary" onClick={onApprove} loading={loading}>{locale === 'zh' ? '批准' : 'Approve'}</Button>
          <Button size="small" danger onClick={onReject} loading={loading}>{locale === 'zh' ? '拒绝' : 'Reject'}</Button>
        </Space>
      }
    />
  );
}

function ConversationMemoryCard({ memory, loading, onCompact, locale }) {
  const isZh = locale === 'zh';
  const summary = String(memory?.summary || '').trim();
  const total = Number(memory?.total_message_count || 0);
  const compacted = Number(memory?.summarized_message_count || 0);
  const copySummary = async () => {
    if (!summary) return;
    await navigator.clipboard?.writeText(summary);
  };
  return (
    <Collapse
      className="chat-memory-card"
      items={[{
        key: 'conversation-memory',
        label: (
          <Space size={6} wrap>
            <DatabaseOutlined />
            <Text strong>{isZh ? '当前会话记忆' : 'Conversation memory'}</Text>
            <Tag>{isZh ? `${compacted}/${total} 条已整理` : `${compacted}/${total} summarized`}</Tag>
          </Space>
        ),
        extra: <Button size="small" type="text" icon={<ReloadOutlined />} loading={loading} onClick={(event) => { event.stopPropagation(); onCompact?.(); }}>{isZh ? '整理' : 'Refresh'}</Button>,
        children: (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {summary ? <MarkdownBlock content={summary} /> : <Text type="secondary">{isZh ? '会话较短，尚未生成滚动摘要。' : 'This chat is still short; no rolling summary yet.'}</Text>}
            <Space wrap>
              <Text type="secondary" className="chat-memory-meta">{isZh ? `保留最近 ${memory?.recent_message_count || 0} 条原文` : `${memory?.recent_message_count || 0} recent raw messages retained`}</Text>
              {memory?.updated_at ? <Text type="secondary" className="chat-memory-meta">{new Date(memory.updated_at).toLocaleString()}</Text> : null}
              {summary ? <Button size="small" icon={<CopyOutlined />} onClick={copySummary}>{isZh ? '复制' : 'Copy'}</Button> : null}
            </Space>
          </Space>
        ),
      }]}
    />
  );
}

function StreamFailureCard({ failure, onRetry, onEdit, locale }) {
  if (!failure) return null;
  const isZh = locale === 'zh';
  return (
    <Alert
      className="chat-stream-failure"
      type="error"
      showIcon
      message={isZh ? '本次回复未完成' : 'Response did not complete'}
      description={failure.message || (isZh ? '模型请求失败，请重试或修改问题。' : 'The model request failed. Retry or edit your question.')}
      action={failure.messageText ? <Space><Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>{isZh ? '重试' : 'Retry'}</Button><Button size="small" icon={<EditOutlined />} onClick={onEdit}>{isZh ? '编辑问题' : 'Edit'}</Button></Space> : null}
    />
  );
}

function readLastRuntime() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RUNTIME_STORAGE_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function buildInitialRuntime(data) {
  const last = readLastRuntime();
  const profiles = (data?.exchange_profiles || []).filter((item) => item.configured);
  const providers = (data?.llm_providers || []).filter((item) => item.api_key_configured);
  const profile = profiles.find((item) => item.profile_id === last.exchange_profile_id) || profiles[0] || {};
  const provider = providers.find((item) => item.provider_id === last.llm_provider_id) || providers[0] || {};
  return {
    exchange_profile_id: profile.profile_id || '',
    market_type: profile.supported_market_types?.includes(last.market_type) ? last.market_type : 'spot',
    symbol: '',
    llm_provider_id: provider.provider_id || '',
    global_requirement: last.global_requirement || '',
    system_prompt_role: last.system_prompt_role || provider.system_prompt_role || 'system',
  };
}

function runtimeSubtitle(runtime, locale) {
  if (!runtime?.symbol) return '';
  const market = runtime.market_type === 'swap'
    ? (locale === 'zh' ? '永续合约' : 'Perpetual')
    : (locale === 'zh' ? '现货' : 'Spot');
  return [String(runtime.exchange || '').toUpperCase(), market, runtime.symbol, runtime.model].filter(Boolean).join(' · ');
}

export default function ChatPage({ token }) {
  const { t, locale } = usePreferences();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const [bootstrap, setBootstrap] = useState(null);
  const [currentSessionId, setCurrentSessionId] = useState('');
  const [currentConfigId, setCurrentConfigId] = useState('');
  const [messages, setMessages] = useState([]);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const [streamFailure, setStreamFailure] = useState(null);
  const [persistenceWarning, setPersistenceWarning] = useState('');
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [editingFailedMessage, setEditingFailedMessage] = useState('');
  const [conversationMemory, setConversationMemory] = useState(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [error, setError] = useState('');
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createMode, setCreateMode] = useState('task');
  const [creatingConfigId, setCreatingConfigId] = useState('');
  const [temporaryRuntime, setTemporaryRuntime] = useState({
    exchange_profile_id: '', market_type: 'spot', symbol: '', llm_provider_id: '', global_requirement: '', system_prompt_role: 'system',
  });
  const [symbolOptions, setSymbolOptions] = useState([]);
  const [symbolLoading, setSymbolLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const draftRef = useRef({ content: '', reasoning_content: '', pending: false });
  const incomingDraftRef = useRef({ content: '', reasoning_content: '' });
  const animationFrameRef = useRef(null);
  const lastDraftPaintRef = useRef(0);
  const abortRef = useRef(null);
  const symbolRequestRef = useRef(0);
  const chatWindowRef = useRef(null);
  const followOutputRef = useRef(true);

  const isZh = locale === 'zh';

  const paintDraftMessage = () => {
    const draft = normalizeAssistantDraft(draftRef.current);
    setMessages((prev) => {
      const next = [...prev];
      const lastIndex = next.length - 1;
      if (lastIndex >= 0 && next[lastIndex].role === 'assistant' && next[lastIndex].draft) next[lastIndex] = draft;
      else next.push(draft);
      return next;
    });
  };

  const drainDraftBuffer = (immediate = false) => {
    if (immediate && animationFrameRef.current) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    const pending = incomingDraftRef.current;
    const backlog = pending.content.length + pending.reasoning_content.length;
    if (!backlog) return;
    const batchSize = immediate ? backlog : backlog > 500 ? 120 : backlog > 160 ? 48 : backlog > 40 ? 18 : 8;
    let remaining = batchSize;
    const reasoningSlice = pending.reasoning_content.slice(0, remaining);
    pending.reasoning_content = pending.reasoning_content.slice(reasoningSlice.length);
    remaining -= reasoningSlice.length;
    const contentSlice = pending.content.slice(0, remaining);
    pending.content = pending.content.slice(contentSlice.length);
    draftRef.current.reasoning_content += reasoningSlice;
    draftRef.current.content += contentSlice;
    draftRef.current.pending = false;
    paintDraftMessage();
  };

  const scheduleDraftAnimation = () => {
    if (animationFrameRef.current) return;
    const animate = (timestamp) => {
      animationFrameRef.current = null;
      if (timestamp - lastDraftPaintRef.current >= 30) {
        drainDraftBuffer(false);
        lastDraftPaintRef.current = timestamp;
      }
      if (incomingDraftRef.current.content || incomingDraftRef.current.reasoning_content) {
        animationFrameRef.current = window.requestAnimationFrame(animate);
      }
    };
    animationFrameRef.current = window.requestAnimationFrame(animate);
  };

  const appendDraftMessage = () => {
    draftRef.current = { content: '', reasoning_content: '', pending: true };
    incomingDraftRef.current = { content: '', reasoning_content: '' };
    followOutputRef.current = true;
    setShowScrollToBottom(false);
    setMessages((prev) => [...prev, normalizeAssistantDraft(draftRef.current)]);
  };

  const addOrUpdateSession = (session) => {
    if (!session?.session_id) return;
    setBootstrap((prev) => ({
      ...prev,
      sessions: [session, ...(prev?.sessions || []).filter((item) => item.session_id !== session.session_id)],
    }));
  };

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const response = await api.get('/chat/bootstrap');
        if (!mounted) return;
        const data = response.data;
        setBootstrap(data);
        const firstConfig = data.configs?.[0]?.config_id || '';
        setCurrentConfigId(firstConfig);
        setCreatingConfigId(firstConfig);
        setTemporaryRuntime(buildInitialRuntime(data));
        if ((data.exchange_profiles || []).some((item) => item.configured) && (data.llm_providers || []).some((item) => item.api_key_configured)) {
          setCreateMode('temporary');
        }
        if (data.sessions?.[0]?.session_id) setCurrentSessionId(data.sessions[0].session_id);
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load chat bootstrap');
      } finally {
        if (mounted) setLoading(false);
      }
    }
    load();
    return () => {
      mounted = false;
      if (animationFrameRef.current) window.cancelAnimationFrame(animationFrameRef.current);
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    let mounted = true;
    async function loadMessages() {
      if (!currentSessionId) {
        setMessages([]);
        setPendingApproval(null);
        setConversationMemory(null);
        return;
      }
      try {
        const response = await api.get(`/chat/sessions/${currentSessionId}`);
        if (!mounted) return;
        setMessages((response.data.messages || []).filter(hasRenderableMessage));
        setPendingApproval(response.data.pending_approval || null);
        setConversationMemory(response.data.conversation_memory || null);
        setStreamFailure(null);
        setPersistenceWarning('');
        setEditingFailedMessage('');
        if (response.data.session) addOrUpdateSession(response.data.session);
        if (response.data.session?.config_id) {
          setCurrentConfigId(response.data.session.config_id);
          setCreatingConfigId(response.data.session.config_id);
        }
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load session');
      }
    }
    loadMessages();
    return () => { mounted = false; };
  }, [currentSessionId]);

  useEffect(() => {
    const scroller = chatWindowRef.current?.querySelector('.ant-bubble-list');
    if (!scroller) return undefined;
    const handleScroll = () => {
      const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      const nearBottom = distance < 96;
      followOutputRef.current = nearBottom;
      setShowScrollToBottom(!nearBottom);
    };
    scroller.addEventListener('scroll', handleScroll, { passive: true });
    handleScroll();
    return () => scroller.removeEventListener('scroll', handleScroll);
  }, [currentSessionId, loading, messages.length]);

  useEffect(() => {
    if (!followOutputRef.current) return undefined;
    const frame = window.requestAnimationFrame(() => {
      const scroller = chatWindowRef.current?.querySelector('.ant-bubble-list');
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, streamStatus]);

  const scrollToBottom = () => {
    const scroller = chatWindowRef.current?.querySelector('.ant-bubble-list');
    if (!scroller) return;
    followOutputRef.current = true;
    setShowScrollToBottom(false);
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });
  };

  const sessionItems = useMemo(() => bootstrap?.sessions || [], [bootstrap]);
  const configOptions = useMemo(() => bootstrap?.configs || [], [bootstrap]);
  const exchangeProfiles = useMemo(() => bootstrap?.exchange_profiles || [], [bootstrap]);
  const providerOptions = useMemo(() => bootstrap?.llm_providers || [], [bootstrap]);
  const currentSession = useMemo(
    () => sessionItems.find((item) => item.session_id === currentSessionId) || null,
    [currentSessionId, sessionItems],
  );
  const activeRuntime = currentSession?.session_type === 'temporary' ? currentSession.runtime : null;
  const activeConfig = useMemo(
    () => configOptions.find((item) => item.config_id === (currentSession?.config_id || currentConfigId)) || null,
    [configOptions, currentConfigId, currentSession],
  );
  const activeSubtitle = activeRuntime
    ? runtimeSubtitle(activeRuntime, locale)
    : (activeConfig ? `${activeConfig.symbol} / ${activeConfig.mode} · ${activeConfig.model || ''}` : t('chatPageDesc'));
  const selectedProfile = exchangeProfiles.find((item) => item.profile_id === temporaryRuntime.exchange_profile_id);
  const selectedProvider = providerOptions.find((item) => item.provider_id === temporaryRuntime.llm_provider_id);
  const temporaryTimeframes = bootstrap?.temporary_chat?.timeframes || ['15m', '1h', '4h', '1d', '1w'];

  const conversationItems = useMemo(
    () => sessionItems.map((item) => ({
      key: item.session_id,
      label: item.title || item.session_id,
      timestamp: item.updated_at,
      group: item.session_type === 'temporary'
        ? `${item.runtime?.symbol || item.symbol || 'Chat'} · ${isZh ? '临时' : 'Temporary'}`
        : (item.symbol || item.config_id),
    })),
    [isZh, sessionItems],
  );

  const bubbleItems = useMemo(
    () => messages.filter(hasRenderableMessage).map((message, index) => {
      const role = message.role === 'assistant' ? 'ai' : message.role === 'user' ? 'user' : 'system';
      return {
        key: `${message.role}-${index}`,
        role,
        content: message.content || '',
        extraInfo: {
          reasoning: message.reasoning_content || '',
          originalRole: message.role,
          streaming: streaming && index === messages.length - 1 && message.role === 'assistant',
          status: streamStatus,
        },
        streaming: streaming && index === messages.length - 1 && message.role === 'assistant',
      };
    }),
    [messages, streaming, streamStatus],
  );

  const searchSymbols = async (keyword = '') => {
    if (!temporaryRuntime.exchange_profile_id) return;
    const requestId = symbolRequestRef.current + 1;
    symbolRequestRef.current = requestId;
    setSymbolLoading(true);
    try {
      const response = await api.get('/chat/market-symbols', {
        params: {
          exchange_profile_id: temporaryRuntime.exchange_profile_id,
          market_type: temporaryRuntime.market_type,
          keyword,
        },
      });
      if (requestId !== symbolRequestRef.current) return;
      setSymbolOptions(response.data.symbols || []);
    } catch (err) {
      if (requestId === symbolRequestRef.current) {
        setSymbolOptions([]);
        setError(err.message || 'Failed to load symbols');
      }
    } finally {
      if (requestId === symbolRequestRef.current) setSymbolLoading(false);
    }
  };

  const ensureSession = async () => {
    if (currentSessionId) return currentSessionId;
    const configId = currentConfigId || configOptions[0]?.config_id;
    if (!configId) throw new Error(isZh ? '请先新建一个聊天会话。' : 'Create a chat session first.');
    const response = await api.post('/chat/sessions', { mode: 'task', config_id: configId });
    const session = response.data.session || { session_id: response.data.session_id, title: 'New chat', config_id: configId, session_type: 'task' };
    addOrUpdateSession(session);
    setCurrentSessionId(response.data.session_id);
    return response.data.session_id;
  };

  const runStream = async (url, messageText = '') => {
    setStreaming(true);
    setStreamStatus(isZh ? '正在整理上下文' : 'Preparing context');
    setStreamFailure(null);
    setPersistenceWarning('');
    appendDraftMessage();
    const controller = new AbortController();
    abortRef.current = controller;
    let aborted = false;
    let lifecycle = createChatStreamLifecycle();
    try {
      await streamSse(url, token, (event) => {
        lifecycle = reduceChatStreamLifecycle(lifecycle, event);
        if (event.type === 'token') {
          incomingDraftRef.current.content += event.token;
          setStreamStatus('');
          scheduleDraftAnimation();
        } else if (event.type === 'reasoning_token') {
          incomingDraftRef.current.reasoning_content += event.token;
          setStreamStatus('');
          scheduleDraftAnimation();
        } else if (event.type === 'status') {
          setStreamStatus(event.message || '');
        } else if (event.type === 'tool_calls') {
          setStreamStatus(t('toolApproval'));
        } else if (event.type === 'approval_required') {
          setPendingApproval(event.approval || null);
          setStreamStatus(t('toolApproval'));
        } else if (event.type === 'tool_result') {
          setMessages((prev) => [...prev, { role: 'tool', content: event.content }]);
        } else if (event.type === 'done') {
          drainDraftBuffer(true);
          if (Array.isArray(event.messages)) setMessages(event.messages.filter(hasRenderableMessage));
          setPendingApproval(event.pending_approval || null);
          if (event.conversation_memory) setConversationMemory(event.conversation_memory);
          setPersistenceWarning(event.persisted === false ? (event.persistence_error || (isZh ? '回答已生成，但暂时无法保存到历史会话。' : 'The response was generated but could not be saved.')) : '');
          setStreamStatus('');
        } else if (event.type === 'error') {
          if (!event.phase || event.phase === 'generation') {
            setStreamFailure({ message: event.message || 'Stream failed', messageText, phase: event.phase || 'generation' });
          } else {
            setPersistenceWarning(event.message || (isZh ? '回答处理失败，请重试。' : 'The response could not be finalized.'));
          }
          setStreamStatus('');
        }
      }, controller.signal);
    } catch (err) {
      aborted = err.name === 'AbortError';
      if (!aborted) {
        lifecycle = { ...lifecycle, handledTerminalEvent: true };
        setStreamFailure({ message: err.message || (isZh ? '连接意外中断' : 'Connection interrupted'), messageText, phase: 'network' });
      }
    } finally {
      drainDraftBuffer(true);
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.role === 'assistant' && last.draft && !last.content && !last.reasoning_content) return prev.slice(0, -1);
        return prev;
      });
      abortRef.current = null;
      setStreaming(false);
      setStreamStatus('');
    }
    return { ...lifecycle, aborted };
  };

  const handleSend = async (value = input, { appendUserMessage = true, retryBackend = false, replaceLastUserMessage = false } = {}) => {
    if (!value.trim() || streaming) return;
    const message = value.trim();
    const replacingFailedMessage = replaceLastUserMessage || Boolean(editingFailedMessage);
    setInput('');
    setPendingApproval(null);
    setError('');
    try {
      const sessionId = await ensureSession();
      if (appendUserMessage && !replacingFailedMessage) setMessages((prev) => [...prev, { role: 'user', content: message }]);
      const retryQuery = retryBackend || replacingFailedMessage ? `&retry=true${replacingFailedMessage ? '&replace_last=true' : ''}` : '';
      const result = await runStream(`/api/chat/sessions/${sessionId}/stream?message=${encodeURIComponent(message)}${retryQuery}`, message);
      setEditingFailedMessage('');
      if (!result.completed && !result.aborted && !result.handledTerminalEvent) {
        setStreamFailure({ message: isZh ? '连接已结束，但没有收到完成事件。' : 'The connection closed without a completion event.', messageText: message, phase: 'network' });
      }
    } catch (err) {
      setError(err.message || 'Failed to start chat');
      setInput(message);
    }
  };

  const handleApproval = async (approved) => {
    if (!currentSessionId || streaming) return;
    setPendingApproval(null);
    await runStream(`/api/chat/sessions/${currentSessionId}/stream?approval=${approved}`);
  };

  const stopStreaming = () => {
    abortRef.current?.abort();
    setStreamStatus('');
  };

  const retryFailedMessage = () => {
    if (!streamFailure?.messageText || streaming) return;
    handleSend(streamFailure.messageText, { appendUserMessage: false, retryBackend: true });
  };

  const editFailedMessage = () => {
    if (!streamFailure?.messageText) return;
    setInput(streamFailure.messageText);
    setEditingFailedMessage(streamFailure.messageText);
    setStreamFailure(null);
  };

  const compactConversationMemory = async () => {
    if (!currentSessionId || memoryLoading) return;
    setMemoryLoading(true);
    try {
      const response = await api.post(`/chat/sessions/${currentSessionId}/memory/compact`);
      setConversationMemory(response.data.conversation_memory || null);
    } catch (err) {
      setError(err.message || 'Failed to compact conversation memory');
    } finally {
      setMemoryLoading(false);
    }
  };

  const createSession = async () => {
    try {
      setCreating(true);
      let payload;
      if (createMode === 'temporary') {
        if (!temporaryRuntime.exchange_profile_id || !temporaryRuntime.symbol || !temporaryRuntime.llm_provider_id) {
          throw new Error(isZh ? '请选择交易所账户、市场标的和模型服务商。' : 'Choose an exchange profile, symbol, and LLM provider.');
        }
        if (!temporaryRuntime.global_requirement.trim()) {
          throw new Error(isZh ? '请填写全局需求。' : 'Enter a global requirement.');
        }
        payload = { mode: 'temporary', runtime: { ...temporaryRuntime, read_only: true } };
      } else {
        if (!creatingConfigId) throw new Error(isZh ? '请选择任务配置。' : 'Choose a task configuration.');
        payload = { mode: 'task', config_id: creatingConfigId };
      }
      const response = await api.post('/chat/sessions', payload);
      const session = response.data.session || {
        session_id: response.data.session_id,
        title: createMode === 'temporary' ? `${temporaryRuntime.symbol} | Temporary chat` : 'New chat',
        config_id: createMode === 'task' ? creatingConfigId : '',
        symbol: createMode === 'temporary' ? temporaryRuntime.symbol : '',
        session_type: createMode,
        runtime: createMode === 'temporary' ? temporaryRuntime : {},
      };
      if (createMode === 'temporary') {
        window.localStorage.setItem(RUNTIME_STORAGE_KEY, JSON.stringify({
          exchange_profile_id: temporaryRuntime.exchange_profile_id,
          market_type: temporaryRuntime.market_type,
          llm_provider_id: temporaryRuntime.llm_provider_id,
          global_requirement: temporaryRuntime.global_requirement,
          system_prompt_role: temporaryRuntime.system_prompt_role,
        }));
      }
      addOrUpdateSession(session);
      setCurrentSessionId(response.data.session_id);
      if (createMode === 'task') setCurrentConfigId(creatingConfigId);
      setCreateModalOpen(false);
      setSidebarOpen(false);
      setMessages([]);
      setPendingApproval(null);
      setConversationMemory(null);
      setStreamFailure(null);
      setPersistenceWarning('');
    } catch (err) {
      setError(err.message || 'Failed to create chat');
    } finally {
      setCreating(false);
    }
  };

  const clearSession = async () => {
    if (!currentSessionId) return;
    await api.post(`/chat/sessions/${currentSessionId}/clear`);
    setMessages([]);
    setPendingApproval(null);
    setConversationMemory(null);
    setStreamFailure(null);
    setPersistenceWarning('');
    draftRef.current = { content: '', reasoning_content: '', pending: false };
    incomingDraftRef.current = { content: '', reasoning_content: '' };
  };

  const deleteSession = async () => {
    if (!currentSessionId) return;
    const deletingId = currentSessionId;
    await api.delete(`/chat/sessions/${deletingId}`);
    const remaining = sessionItems.filter((item) => item.session_id !== deletingId);
    const nextSession = remaining[0] || null;
    setBootstrap((prev) => ({ ...prev, sessions: remaining }));
    setCurrentSessionId(nextSession?.session_id || '');
    setCurrentConfigId(nextSession?.config_id || currentConfigId || configOptions[0]?.config_id || '');
    if (!nextSession) {
      setMessages([]);
      setPendingApproval(null);
      setConversationMemory(null);
      setStreamFailure(null);
      setPersistenceWarning('');
      draftRef.current = { content: '', reasoning_content: '', pending: false };
      incomingDraftRef.current = { content: '', reasoning_content: '' };
    }
    if (isMobile) setSidebarOpen(false);
  };

  const openCreateSessionModal = () => {
    setCreatingConfigId(currentConfigId || configOptions[0]?.config_id || '');
    setTemporaryRuntime((prev) => ({ ...buildInitialRuntime(bootstrap), ...prev, symbol: '' }));
    setSymbolOptions([]);
    setCreateModalOpen(true);
    setSidebarOpen(false);
  };

  const updateRuntime = (patch) => setTemporaryRuntime((prev) => ({ ...prev, ...patch }));

  const memoryCard = <ConversationMemoryCard memory={conversationMemory} loading={memoryLoading} onCompact={compactConversationMemory} locale={locale} />;

  const sidebarContent = (
    <div className="x-chat-sidebar-inner">
      <div className="x-chat-sidebar-top">
        <div className="x-chat-sidebar-title">
          <Text strong>{t('chat')}</Text>
          <Text type="secondary">{isZh ? '实时行情、消息面与对话历史' : 'Live market, news, and chat history'}</Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} block onClick={openCreateSessionModal}>{t('createSession')}</Button>
        <div className="x-chat-active-config">
          <Text type="secondary">{activeRuntime ? (isZh ? '临时只读上下文' : 'Temporary read-only context') : t('symbol')}</Text>
          <Text strong>{activeSubtitle || '-'}</Text>
          {activeRuntime ? <Tag color="purple">{isZh ? '临时' : 'Temporary'}</Tag> : null}
        </div>
        {!isMobile ? memoryCard : null}
        <div className="x-chat-session-actions">
          <Popconfirm title={t('confirmDelete')} onConfirm={clearSession} disabled={!currentSessionId || streaming}>
            <Button icon={<ClearOutlined />} disabled={!currentSessionId || streaming} block>{t('clearMessages')}</Button>
          </Popconfirm>
          <Popconfirm title={t('confirmDeleteSession')} onConfirm={deleteSession} disabled={!currentSessionId || streaming}>
            <Button icon={<DeleteOutlined />} disabled={!currentSessionId || streaming} danger block>{t('deleteSession')}</Button>
          </Popconfirm>
        </div>
      </div>
      <div className="x-chat-sidebar-list">
        <Conversations items={conversationItems} activeKey={currentSessionId} onActiveChange={(id) => { setCurrentSessionId(id); if (isMobile) setSidebarOpen(false); }} groupable />
      </div>
    </div>
  );

  return (
    <XProvider>
      <div className="chat-console-page">
        {error ? <Alert type="error" message={error} showIcon closable onClose={() => setError('')} /> : null}
        {loading ? (
          <Card className="panel-card loading-card"><Spin /></Card>
        ) : (
          <>
            {isMobile ? (
              <Drawer className="chat-sidebar-drawer" placement="left" title={t('history')} width="min(86vw, 320px)" open={sidebarOpen} onClose={() => setSidebarOpen(false)}>
                {sidebarContent}
              </Drawer>
            ) : null}
            {isMobile ? (
              <Drawer className="chat-memory-drawer" placement="right" title={isZh ? '当前会话记忆' : 'Conversation memory'} width="min(90vw, 420px)" open={memoryOpen} onClose={() => setMemoryOpen(false)}>
                {memoryCard}
              </Drawer>
            ) : null}
            <div className={`x-chat-shell ${isMobile ? 'is-mobile' : ''}`}>
              {!isMobile ? <aside className="x-chat-sidebar">{sidebarContent}</aside> : null}
              <section className="x-chat-main">
                <div className="x-chat-main-header">
                  <Space size="middle" className="x-chat-main-heading">
                    {isMobile ? <Button icon={<MenuOutlined />} onClick={() => setSidebarOpen(true)} /> : null}
                    <div className="x-chat-main-titles">
                      <Space size={8} wrap>
                        <Title level={4} style={{ margin: 0 }}>{isZh ? '即时市场研究' : 'Live market research'}</Title>
                        {activeRuntime ? <Tag color="purple">{isZh ? '临时只读' : 'Temporary read-only'}</Tag> : null}
                      </Space>
                      <Text type="secondary">{activeSubtitle}</Text>
                    </div>
                  </Space>
                  <Space size={8}>
                    {isMobile ? <Button icon={<DatabaseOutlined />} onClick={() => setMemoryOpen(true)} /> : null}
                    <Button icon={<PlusOutlined />} onClick={openCreateSessionModal}>{isMobile ? null : t('createSession')}</Button>
                  </Space>
                </div>
                <div className="x-chat-main-body">
                  <div className="x-chat-window" ref={chatWindowRef}>
                    {bubbleItems.length ? (
                      <Bubble.List
                        items={bubbleItems}
                        role={{
                          user: {
                            placement: 'end', variant: 'filled', shape: 'round', rootClassName: 'x-bubble-user',
                            contentRender: (content, info) => <MessageContent content={content} role={info?.extraInfo?.originalRole} />,
                          },
                          ai: {
                            placement: 'start', variant: 'shadow', shape: 'round', rootClassName: 'x-bubble-ai',
                            contentRender: (content, info) => <MessageContent content={content} reasoning={info?.extraInfo?.reasoning} role="assistant" streaming={info?.extraInfo?.streaming} status={info?.extraInfo?.status} />,
                          },
                          system: {
                            placement: 'start', variant: 'outlined', shape: 'round', rootClassName: 'x-bubble-system',
                            contentRender: (content, info) => <MessageContent content={content} role={info?.extraInfo?.originalRole} />,
                          },
                        }}
                      />
                    ) : (
                      <div className="x-chat-empty">
                        <Empty description={t('emptySessions')}>
                          <Paragraph type="secondary">{isZh ? '选择交易所、市场、标的与模型后即可开始。每次提问都会刷新行情、技术面与最新消息。' : 'Choose an exchange, market, symbol, and model. Each message refreshes live market, technical, and news context.'}</Paragraph>
                          <Button type="primary" icon={<PlusOutlined />} onClick={openCreateSessionModal}>{t('createSession')}</Button>
                        </Empty>
                      </div>
                    )}
                    {showScrollToBottom ? <Button className="chat-scroll-bottom" shape="circle" icon={<DownOutlined />} onClick={scrollToBottom} aria-label={isZh ? '回到底部' : 'Scroll to bottom'} /> : null}
                  </div>
                  <ToolApproval approval={pendingApproval} loading={streaming} onApprove={() => handleApproval(true)} onReject={() => handleApproval(false)} locale={locale} />
                  <PersistenceWarning warning={persistenceWarning} locale={locale} onDismiss={() => setPersistenceWarning('')} />
                  <StreamFailureCard failure={streamFailure} onRetry={retryFailedMessage} onEdit={editFailedMessage} locale={locale} />
                  <div className="x-chat-sender-wrap">
                    <Sender value={input} onChange={setInput} onSubmit={handleSend} onCancel={stopStreaming} loading={streaming} placeholder={t('chatPlaceholder')} autoSize={{ minRows: 1, maxRows: isMobile ? 5 : 7 }} submitType="enter" />
                  </div>
                </div>
              </section>
            </div>
          </>
        )}

        <Modal
          className="chat-create-modal"
          title={t('createSession')}
          open={createModalOpen}
          width={isMobile ? 'calc(100vw - 24px)' : 760}
          onOk={createSession}
          onCancel={() => setCreateModalOpen(false)}
          confirmLoading={creating}
          okText={isZh ? '创建并开始' : 'Create chat'}
        >
          <div className="chat-create-mode">
            <Segmented
              block
              value={createMode}
              options={[
                { label: isZh ? '临时聊天' : 'Temporary chat', value: 'temporary' },
                { label: isZh ? '任务配置聊天' : 'Task chat', value: 'task' },
              ]}
              onChange={(value) => setCreateMode(value)}
            />
          </div>
          {createMode === 'task' ? (
            <div className="chat-create-section">
              <Text type="secondary">{isZh ? '继续使用现有任务的标的、模型、Prompt 与工具审批规则。' : 'Use the existing task symbol, model, prompt, and tool-approval rules.'}</Text>
              <Select style={{ width: '100%' }} value={creatingConfigId || undefined} options={configOptions.map((item) => ({ label: `${item.symbol} / ${item.mode} · ${item.model || '-'}`, value: item.config_id }))} onChange={setCreatingConfigId} />
            </div>
          ) : (
            <div className="chat-create-section">
              <Alert type="info" showIcon message={isZh ? '临时聊天为只读，不会写入任务配置或执行交易。' : 'Temporary chats are read-only and never change task configuration or trade.'} />
              <div className="chat-create-grid">
                <label className="form-field">
                  <span>{isZh ? '交易所账户' : 'Exchange profile'}</span>
                  <Select
                    value={temporaryRuntime.exchange_profile_id || undefined}
                    options={exchangeProfiles.map((item) => ({
                      value: item.profile_id,
                      disabled: !item.configured,
                      label: `${item.name || item.profile_id} · ${String(item.exchange || '').toUpperCase()}`,
                    }))}
                    onChange={(value) => {
                      const profile = exchangeProfiles.find((item) => item.profile_id === value);
                      updateRuntime({ exchange_profile_id: value, market_type: profile?.supported_market_types?.includes('spot') ? 'spot' : profile?.supported_market_types?.[0] || 'spot', symbol: '' });
                      setSymbolOptions([]);
                    }}
                  />
                  <Text type="secondary" className="chat-create-select-note">
                    {selectedProfile?.configured
                      ? (isZh ? `已选择 ${String(selectedProfile.exchange || '').toUpperCase()} 账户，API Key 已配置。` : `${String(selectedProfile.exchange || '').toUpperCase()} account selected; API key configured.`)
                      : (isZh ? '仅显示已保存账户；密钥不会在此页面展示。' : 'Saved accounts only; keys are never displayed here.')}
                  </Text>
                </label>
                <label className="form-field">
                  <span>{isZh ? '市场分类' : 'Market'}</span>
                  <Segmented
                    block
                    value={temporaryRuntime.market_type}
                    options={(selectedProfile?.supported_market_types || ['spot', 'swap']).map((value) => ({
                      value,
                      label: value === 'spot' ? (isZh ? '现货' : 'Spot') : (isZh ? '永续合约' : 'Perpetual'),
                    }))}
                    onChange={(value) => {
                      updateRuntime({ market_type: value, symbol: '' });
                      setSymbolOptions([]);
                    }}
                  />
                </label>
                <label className="form-field">
                  <span>{isZh ? '交易标的' : 'Symbol'}</span>
                  <Select
                    showSearch
                    filterOption={false}
                    value={temporaryRuntime.symbol || undefined}
                    loading={symbolLoading}
                    options={symbolOptions.map((item) => ({ value: item.symbol, label: item.display_name || item.symbol }))}
                    placeholder={isZh ? '先选择账户，然后搜索或展开标的列表' : 'Choose an account, then search symbols'}
                    onFocus={() => searchSymbols('')}
                    onSearch={searchSymbols}
                    onChange={(value) => updateRuntime({ symbol: value })}
                  />
                </label>
                <label className="form-field">
                  <span>{isZh ? '模型服务商 / API Key' : 'LLM provider / API key'}</span>
                  <Select
                    value={temporaryRuntime.llm_provider_id || undefined}
                    options={providerOptions.map((item) => ({
                      value: item.provider_id,
                      disabled: !item.api_key_configured,
                      label: `${item.name || item.provider_id} · ${item.model || '-'}`,
                    }))}
                    onChange={(value) => {
                      const provider = providerOptions.find((item) => item.provider_id === value);
                      updateRuntime({ llm_provider_id: value, system_prompt_role: provider?.system_prompt_role || 'system' });
                    }}
                  />
                  <Text type="secondary" className="chat-create-select-note">
                    {selectedProvider?.api_key_configured
                      ? (isZh ? '该模型服务商的 API Key 已配置。' : 'This provider API key is configured.')
                      : (isZh ? '请选择已配置 API Key 的模型服务商。' : 'Choose a provider with an API key configured.')}
                  </Text>
                  {selectedProvider ? <Text type="secondary" className="chat-create-select-note">{isZh ? `服务商默认使用${selectedProvider.system_prompt_role === 'user' ? '用户消息（兼容模式）' : 'System 消息'}。` : `Provider default: ${selectedProvider.system_prompt_role === 'user' ? 'user message (compatibility mode)' : 'system message'}.`}</Text> : null}
                </label>
                <label className="form-field">
                  <span>{isZh ? '当前模型' : 'Model'}</span>
                  <Input value={selectedProvider?.model || ''} disabled placeholder={isZh ? '选择模型服务商后显示' : 'Choose a provider'} />
                </label>
                <label className="form-field">
                  <span>{isZh ? '提示词角色' : 'Prompt role'}</span>
                  <Select
                    value={temporaryRuntime.system_prompt_role || selectedProvider?.system_prompt_role || 'system'}
                    options={[
                      { value: 'system', label: isZh ? 'System 消息' : 'System message' },
                      { value: 'user', label: isZh ? 'User 消息（兼容模式）' : 'User message (compatibility)' },
                    ]}
                    onChange={(value) => updateRuntime({ system_prompt_role: value })}
                  />
                  <Text type="secondary" className="chat-create-select-note">{isZh ? '模型不支持 system role 时请选择 User。' : 'Choose User if the model rejects system roles.'}</Text>
                </label>
                <label className="form-field field-span-2">
                  <span>{isZh ? '全局需求' : 'Global requirement'}</span>
                  <TextArea value={temporaryRuntime.global_requirement} onChange={(event) => updateRuntime({ global_requirement: event.target.value })} autoSize={{ minRows: 3, maxRows: 6 }} maxLength={4000} placeholder={isZh ? '例如：以 1–3 天的持仓决策为目标，优先分析风险、关键价位和失效条件。此需求会用于本设备后续创建的临时聊天。' : 'For example: focus on 1–3 day holding decisions, risk, key levels, and invalidation. This will be reused for future temporary chats on this device.'} />
                </label>
              </div>
              <Space direction="vertical" size={3} className="chat-create-hints">
                <Text type="secondary" className="chat-create-hint">
                  {isZh
                    ? `每次提问都会刷新 ${temporaryTimeframes.join(' / ')} K 线技术面与消息面：EMA、RSI、ATR、MACD、ADX、布林带、成交量、成交量分布、SMC、流动性 / iFVG、VWAP 和加密/宏观新闻。`
                    : `Each message refreshes ${temporaryTimeframes.join(' / ')} technical and news context: EMA, RSI, ATR, MACD, ADX, Bollinger Bands, volume, volume profile, SMC, liquidity / iFVG, VWAP, and crypto/macro headlines.`}
                </Text>
                <Text type="secondary" className="chat-create-hint">
                  {isZh
                    ? '临时聊天不加载任务短期记忆、每日总结或策略历史，但会保留当前临时会话内的对话上下文。'
                    : 'Temporary chats do not load task short-term memory, daily summaries, or strategy history, but retain the current conversation context.'}
                </Text>
                <Text type="secondary" className="chat-create-hint">
                  {selectedProvider?.thinking_enabled ? (isZh ? `已启用思考流${selectedProvider.reasoning_effort ? ` · ${selectedProvider.reasoning_effort}` : ''}` : `Thinking stream enabled${selectedProvider.reasoning_effort ? ` · ${selectedProvider.reasoning_effort}` : ''}`) : (isZh ? '模型是否输出思考流由服务商配置决定。' : 'Reasoning-stream availability is determined by the selected provider.')}
                </Text>
              </Space>
            </div>
          )}
        </Modal>
      </div>
    </XProvider>
  );
}
