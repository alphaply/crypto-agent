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
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import {
  BulbOutlined,
  ApartmentOutlined,
  ClearOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  LeftOutlined,
  MenuOutlined,
  PlusOutlined,
  ReloadOutlined,
  RightOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Bubble, Conversations, Sender, XProvider } from '@ant-design/x';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock from '../components/ReasoningBlock';
import ChatRunProgress from '../components/ChatRunProgress';
import { api, streamSse } from '../lib/api';
import { createChatStreamLifecycle, reduceChatStreamLifecycle } from '../lib/chatStreamLifecycle';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';
import { buildInitialRuntime, buildChatTaskPayload, saveLastRuntime } from '../lib/chatTask';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;
const { useBreakpoint } = Grid;

function normalizeAssistantDraft(draft) {
  return {
    role: 'assistant',
    content: draft.content || '',
    reasoning_content: draft.reasoning_content || '',
    reasoning_tokens: Number(draft.reasoning_tokens || 0),
    reasoning_started_at: draft.reasoning_started_at || null,
    pending: Boolean(draft.pending),
    draft: true,
  };
}

function hasRenderableMessage(message) {
  if (message.role !== 'assistant') return true;
  return Boolean(message.pending || String(message.content || '').trim() || String(message.reasoning_content || '').trim() || Number(message.reasoning_tokens || 0));
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

function MessageBranchActions({ branches, activeSessionId, onSelect, onEdit, disabled, locale }) {
  const activeIndex = Math.max(0, branches.findIndex((item) => item.session_id === activeSessionId));
  const hasBranches = branches.length > 1;
  return (
    <div className="chat-message-actions">
      {hasBranches ? (
        <div className="chat-branch-switcher" aria-label={locale === 'zh' ? '回答分支' : 'Response branches'}>
          <Button
            type="text"
            size="small"
            icon={<LeftOutlined />}
            disabled={disabled || activeIndex <= 0}
            onClick={() => onSelect(branches[activeIndex - 1]?.session_id)}
          />
          <span><ApartmentOutlined /> {activeIndex + 1}/{branches.length}</span>
          <Button
            type="text"
            size="small"
            icon={<RightOutlined />}
            disabled={disabled || activeIndex >= branches.length - 1}
            onClick={() => onSelect(branches[activeIndex + 1]?.session_id)}
          />
        </div>
      ) : null}
      <Button type="text" size="small" icon={<EditOutlined />} disabled={disabled} onClick={onEdit}>
        {locale === 'zh' ? '编辑并分支' : 'Edit & branch'}
      </Button>
    </div>
  );
}

function MessageContent({ content, reasoning, reasoningTokens = 0, reasoningStartedAt, role, streaming, status, actions }) {
  const { t, locale } = usePreferences();
  if (role === 'tool') return <ToolMessage content={content} locale={locale} />;
  const normalized = splitThinkingContent(content || '', reasoning || '');
  const hasAnswer = Boolean(normalized.content?.trim());
  const hasReasoning = Boolean(normalized.reasoning?.trim());
  const reasoningStreaming = Boolean(streaming && (!hasAnswer || normalized.thinkingOpen));
  if (!normalized.content?.trim() && !normalized.reasoning?.trim()) {
    return streaming ? (
      <div className="chat-message-content is-streaming">
        <div className="chat-waiting-inline">
          <span className="chat-stream-status-dot" />
          <span>{status || (locale === 'zh' ? '正在等待模型响应…' : 'Waiting for model response…')}</span>
        </div>
      </div>
    ) : null;
  }
  return (
    <div className={`chat-message-content ${streaming ? 'is-streaming' : ''}`}>
      <ReasoningBlock
        title={t('reasoning')}
        content={normalized.reasoning}
        streaming={reasoningStreaming}
        reasoningTokens={reasoningTokens}
        startedAt={reasoningStartedAt}
      />
      {!hasReasoning && reasoningTokens > 0 ? (
        <Alert
          className="reasoning-usage-note"
          type="info"
          showIcon
          message={locale === 'zh'
            ? `模型使用了 ${reasoningTokens} 个推理 token，但上游接口未返回可展示的思考摘要。`
            : `The model used ${reasoningTokens} reasoning tokens, but the upstream API did not expose a displayable summary.`}
        />
      ) : null}
      {hasAnswer ? <div className="chat-answer-content"><MarkdownBlock content={normalized.content} streaming={streaming} /></div> : null}
      {actions || null}
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
  const [run, setRun] = useState(null);
  const [sessionQuery, setSessionQuery] = useState('');
  const sendingRef = useRef(false);
  const pendingQuestionRef = useRef('');
  const [sessionLoading, setSessionLoading] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const [streamFailure, setStreamFailure] = useState(null);
  const [persistenceWarning, setPersistenceWarning] = useState('');
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [editingFailedMessage, setEditingFailedMessage] = useState('');
  const [editingMessage, setEditingMessage] = useState(null);
  const [branching, setBranching] = useState(false);
  const [conversationMemory, setConversationMemory] = useState(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [error, setError] = useState('');
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [creatingConfigId, setCreatingConfigId] = useState('');
  const createMode = creatingConfigId ? 'task' : 'temporary';
  const [temporaryRuntime, setTemporaryRuntime] = useState({
    exchange_profile_id: '', market_type: 'spot', symbol: '', llm_provider_id: '', temperature: null, global_requirement: '', system_prompt_role: 'system',
  });
  const [symbolOptions, setSymbolOptions] = useState([]);
  const [symbolLoading, setSymbolLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const symbolSearchTimerRef = useRef(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const draftRef = useRef({ content: '', reasoning_content: '', reasoning_tokens: 0, reasoning_started_at: null, pending: false });
  const incomingDraftRef = useRef({ content: '', reasoning_content: '' });
  const animationFrameRef = useRef(null);
  const lastDraftPaintRef = useRef(0);
  const abortRef = useRef(null);
  const symbolRequestRef = useRef(0);
  const chatWindowRef = useRef(null);
  const followOutputRef = useRef(true);
  const pendingBranchSessionRef = useRef('');

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
    // Paint everything received during this frame. Artificially slicing a
    // Markdown token (especially backticks) creates invalid intermediate ASTs
    // and lets the visual draft lag behind the actual model stream.
    draftRef.current.reasoning_content += pending.reasoning_content;
    draftRef.current.content += pending.content;
    pending.reasoning_content = '';
    pending.content = '';
    draftRef.current.pending = false;
    paintDraftMessage();
  };

  const scheduleDraftAnimation = () => {
    if (animationFrameRef.current) return;
    const animate = (timestamp) => {
      animationFrameRef.current = null;
      if (timestamp - lastDraftPaintRef.current >= 40) {
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
    draftRef.current = {
      content: '',
      reasoning_content: '',
      reasoning_tokens: 0,
      reasoning_started_at: Date.now(),
      pending: true,
    };
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
        setTemporaryRuntime(buildInitialRuntime(data));
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
      window.clearTimeout(symbolSearchTimerRef.current);
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
      if (pendingBranchSessionRef.current === currentSessionId) {
        pendingBranchSessionRef.current = '';
        return;
      }
      setSessionLoading(true);
      setMessages([]);
      setRun(null);
      setInput(pendingQuestionRef.current);
      pendingQuestionRef.current = '';
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
        }
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load session');
      } finally {
        if (mounted) setSessionLoading(false);
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
  const activeModelConfig = activeRuntime || activeConfig;
  const activeSubtitle = activeRuntime
    ? runtimeSubtitle(activeRuntime, locale)
    : (activeConfig ? `${activeConfig.symbol} / ${activeConfig.mode} · ${activeConfig.model || ''}` : t('chatPageDesc'));
  const selectedProfile = exchangeProfiles.find((item) => item.profile_id === temporaryRuntime.exchange_profile_id);
  const selectedProvider = providerOptions.find((item) => item.provider_id === temporaryRuntime.llm_provider_id);
  const temporaryTimeframes = bootstrap?.temporary_chat?.timeframes || ['1h', '4h', '1d'];

  const branchFamilies = useMemo(() => {
    const families = new Map();
    if (!currentSession) return families;
    messages.forEach((message) => {
      if (message.role !== 'user') return;
      const messageIndex = Number.isInteger(message.message_index) ? message.message_index : null;
      if (messageIndex === null) return;
      const visited = new Map();
      const queue = [currentSession];
      while (queue.length) {
        const item = queue.shift();
        if (!item?.session_id || visited.has(item.session_id)) continue;
        visited.set(item.session_id, item);
        if (Number(item.fork_message_index) === messageIndex && item.parent_session_id) {
          queue.push(sessionItems.find((candidate) => candidate.session_id === item.parent_session_id));
        }
        sessionItems
          .filter((candidate) => candidate.parent_session_id === item.session_id && Number(candidate.fork_message_index) === messageIndex)
          .forEach((candidate) => queue.push(candidate));
      }
      families.set(
        messageIndex,
        [...visited.values()].sort((left, right) => String(left.created_at || '').localeCompare(String(right.created_at || ''))),
      );
    });
    return families;
  }, [currentSession, messages, sessionItems]);

  const conversationItems = useMemo(
    () => sessionItems.filter((item) => `${item.title || ''} ${item.symbol || ''} ${item.config_id || ''}`.toLowerCase().includes(sessionQuery.toLowerCase())).map((item) => ({
      key: item.session_id,
      label: item.title || item.session_id,
      timestamp: item.updated_at,
      group: isZh ? '任务历史' : 'Task history',
    })),
    [isZh, sessionItems, sessionQuery],
  );

  const bubbleItems = useMemo(
    () => messages.filter(hasRenderableMessage).map((message, index) => {
      const role = message.role === 'assistant' ? 'ai' : message.role === 'user' ? 'user' : 'system';
      const messageIndex = Number.isInteger(message.message_index) ? message.message_index : null;
      return {
        key: message.id || `${message.role}-${index}`,
        role,
        content: message.content || '',
        extraInfo: {
          reasoning: message.reasoning_content || '',
          reasoningTokens: Number(message.reasoning_tokens || 0),
          reasoningStartedAt: message.reasoning_started_at || null,
          originalRole: message.role,
          streaming: streaming && message === messages.at(-1) && message.role === 'assistant',
          status: streamStatus,
          messageIndex,
          branches: messageIndex === null ? [] : (branchFamilies.get(messageIndex) || []),
        },
        streaming: streaming && message === messages.at(-1) && message.role === 'assistant',
      };
    }),
    [branchFamilies, messages, streaming, streamStatus],
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
    throw new Error(isZh ? '请先新建任务。' : 'Create a task first.');
  };

  const runStream = async (url, messageText = '', requestBody = null) => {
    setStreaming(true);
    const startedAt = Date.now();
    setRun({ startedAt, endedAt: null, characters: 0, events: [{ at: startedAt, label: isZh ? '整理上下文与行情' : 'Preparing context and market data' }] });
    const recordStage = (label, characters = 0) => setRun((prev) => prev ? ({ ...prev, characters: prev.characters + characters, events: prev.events.at(-1)?.label === label ? prev.events : [...prev.events.slice(-49), { at: Date.now(), label }] }) : prev);
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
        const labels = { token: isZh ? '生成回答' : 'Writing answer', reasoning_token: isZh ? '接收推理摘要' : 'Receiving reasoning', tool_calls: isZh ? '准备工具调用' : 'Preparing tools', approval_required: isZh ? '等待工具审批' : 'Awaiting approval', tool_result: isZh ? '工具已返回结果' : 'Tool result received', done: isZh ? '本轮请求完成' : 'Request completed', error: isZh ? '请求遇到错误' : 'Request error' };
        if (event.type === 'status' || labels[event.type]) recordStage(event.type === 'status' ? event.message : labels[event.type], ['token', 'reasoning_token'].includes(event.type) ? String(event.token || '').length : 0);
        if (event.type === 'token') {
          incomingDraftRef.current.content += event.token;
          setStreamStatus(isZh ? '正在生成回答' : 'Writing the answer');
          scheduleDraftAnimation();
        } else if (event.type === 'reasoning_token') {
          incomingDraftRef.current.reasoning_content += event.token;
          setStreamStatus(isZh ? '模型正在思考' : 'Model is thinking');
          scheduleDraftAnimation();
        } else if (event.type === 'status') {
          setStreamStatus(event.message || '');
        } else if (event.type === 'session_title') {
          if (event.session_id && event.title) {
            setBootstrap((prev) => ({
              ...prev,
              sessions: (prev?.sessions || []).map((session) => session.session_id === event.session_id
                ? { ...session, title: event.title } : session),
            }));
          }
        } else if (event.type === 'tool_calls') {
          setStreamStatus(t('toolApproval'));
        } else if (event.type === 'approval_required') {
          setPendingApproval(event.approval || null);
          setStreamStatus(t('toolApproval'));
        } else if (event.type === 'tool_result') {
          setMessages((prev) => [...prev, { role: 'tool', content: event.content }]);
        } else if (event.type === 'done') {
          drainDraftBuffer(true);
          if (Array.isArray(event.messages)) {
            setMessages(event.messages.filter(hasRenderableMessage));
          } else if (event.completion && !event.completion.has_tool_calls) {
            draftRef.current = {
              content: event.completion.content || draftRef.current.content,
              reasoning_content: event.completion.reasoning_content || draftRef.current.reasoning_content,
              reasoning_tokens: Number(event.completion.reasoning_tokens || draftRef.current.reasoning_tokens || 0),
              reasoning_started_at: draftRef.current.reasoning_started_at,
              pending: false,
            };
            paintDraftMessage();
          }
          setPendingApproval(event.pending_approval || null);
          if (event.session) addOrUpdateSession(event.session);
          if (event.conversation_memory) setConversationMemory(event.conversation_memory);
          setPersistenceWarning(event.persisted === false ? (event.persistence_error || (isZh ? '回答已生成，但暂时无法保存到历史会话。' : 'The response was generated but could not be saved.')) : '');
          setStreamStatus('');
        } else if (event.type === 'error') {
          if (event.phase === 'persistence') {
            setPersistenceWarning(event.message || (isZh ? '回答处理失败，请重试。' : 'The response could not be finalized.'));
          } else {
            setStreamFailure({ message: event.message || 'Stream failed', messageText, phase: event.phase || 'generation' });
          }
          setStreamStatus('');
        }
      }, controller.signal, requestBody);
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
      setRun((prev) => prev ? ({ ...prev, endedAt: Date.now(), events: [...prev.events, { at: Date.now(), label: aborted ? (isZh ? '已停止' : 'Stopped') : lifecycle.completed ? (isZh ? '完成' : 'Complete') : (isZh ? '请求结束，请查看结果状态' : 'Request ended; review result') }] }) : prev);
      abortRef.current = null;
      setStreaming(false);
      setStreamStatus('');
    }
    return { ...lifecycle, aborted };
  };

  const handleSend = async (value = input, { appendUserMessage = true, retryBackend = false, replaceLastUserMessage = false } = {}) => {
    if (!value.trim() || streaming || sendingRef.current || sessionLoading) return;
    if (!currentSessionId) { pendingQuestionRef.current = value; openCreateSessionModal(); return; }
    sendingRef.current = true;
    const message = value.trim();
    const replacingFailedMessage = replaceLastUserMessage || Boolean(editingFailedMessage);
    setInput('');
    setPendingApproval(null);
    setError('');
    try {
      const sessionId = await ensureSession();
      if (appendUserMessage && !replacingFailedMessage) setMessages((prev) => [...prev, { role: 'user', content: message }]);
      const result = await runStream(
        `/api/chat/sessions/${sessionId}/stream`,
        message,
        { message, retry: retryBackend || replacingFailedMessage, replace_last: replacingFailedMessage },
      );
      setEditingFailedMessage('');
      if (!result.completed && !result.aborted && !result.handledTerminalEvent) {
        setStreamFailure({ message: isZh ? '连接已结束，但没有收到完成事件。' : 'The connection closed without a completion event.', messageText: message, phase: 'network' });
      }
    } catch (err) {
      setError(err.message || 'Failed to start chat');
      setInput(message);
    } finally {
      sendingRef.current = false;
    }
  };

  const handleApproval = async (approved) => {
    if (!currentSessionId || streaming) return;
    setPendingApproval(null);
    await runStream(`/api/chat/sessions/${currentSessionId}/stream`, '', { approval: approved });
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

  const selectMessageBranch = (sessionId) => {
    if (!sessionId || sessionId === currentSessionId || streaming) return;
    setCurrentSessionId(sessionId);
  };

  const openMessageEditor = (messageIndex) => {
    const message = messages[messageIndex];
    if (!message || message.role !== 'user' || streaming) return;
    setEditingMessage({ messageIndex, content: String(message.content || '') });
  };

  const createMessageBranch = async () => {
    if (!editingMessage || !currentSessionId || branching || streaming) return;
    const content = String(editingMessage.content || '').trim();
    if (!content) return;
    setBranching(true);
    setError('');
    try {
      const response = await api.post(`/chat/sessions/${currentSessionId}/branches`, {
        message_index: editingMessage.messageIndex,
        content,
      });
      const branchSession = response.data.session;
      const branchId = response.data.session_id;
      if (!branchId || !branchSession) throw new Error('Branch session was not created');

      const prefix = messages.slice(0, editingMessage.messageIndex);
      pendingBranchSessionRef.current = branchId;
      addOrUpdateSession(branchSession);
      setMessages([...prefix, { role: 'user', content }]);
      setPendingApproval(null);
      setConversationMemory(null);
      setStreamFailure(null);
      setPersistenceWarning('');
      setEditingMessage(null);
      setCurrentSessionId(branchId);
      await runStream(`/api/chat/sessions/${branchId}/stream`, content, { message: content });
    } catch (err) {
      setError(err.message || (isZh ? '创建回答分支失败。' : 'Failed to create response branch.'));
    } finally {
      setBranching(false);
    }
  };

  const compactConversationMemory = async () => {
    if (!currentSessionId || memoryLoading) return;
    setMemoryLoading(true);
    try {
      const response = await api.post(`/chat/sessions/${currentSessionId}/memory/compact`);
      setConversationMemory(response.data.conversation_memory || null);
      if (response.data.session) addOrUpdateSession(response.data.session);
    } catch (err) {
      setError(err.message || 'Failed to compact conversation memory');
    } finally {
      setMemoryLoading(false);
    }
  };

  const createSession = async () => {
    if (creating) return;
    setCreateError('');
    try {
      setCreating(true);
      if (createMode === 'temporary') {
        if (!temporaryRuntime.exchange_profile_id || !temporaryRuntime.symbol || !temporaryRuntime.llm_provider_id) {
          throw new Error(isZh ? '请选择交易所账户、市场标的和模型服务商。' : 'Choose an exchange profile, symbol, and LLM provider.');
        }
        if (!temporaryRuntime.global_requirement.trim()) {
          throw new Error(isZh ? '请填写全局需求。' : 'Enter a global requirement.');
        }
      } else {
        if (!creatingConfigId) throw new Error(isZh ? '请选择任务配置。' : 'Choose a task configuration.');
      }
      const response = await api.post('/chat/sessions', buildChatTaskPayload(temporaryRuntime, creatingConfigId));
      const session = response.data.session || {
        session_id: response.data.session_id,
        title: t('createSession'),
        config_id: createMode === 'task' ? creatingConfigId : '',
        symbol: createMode === 'temporary' ? temporaryRuntime.symbol : '',
        session_type: createMode,
        runtime: createMode === 'temporary' ? temporaryRuntime : {},
      };
      if (createMode === 'temporary') {
        saveLastRuntime(temporaryRuntime);
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
      setCreateError(err.message || 'Failed to create chat');
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
    draftRef.current = { content: '', reasoning_content: '', reasoning_tokens: 0, reasoning_started_at: null, pending: false };
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
      draftRef.current = { content: '', reasoning_content: '', reasoning_tokens: 0, reasoning_started_at: null, pending: false };
      incomingDraftRef.current = { content: '', reasoning_content: '' };
    }
    if (isMobile) setSidebarOpen(false);
  };

  const openCreateSessionModal = () => {
    if (streaming || sendingRef.current) return;
    setCreateError('');
    setCreatingConfigId('');
    setTemporaryRuntime((prev) => buildInitialRuntime(bootstrap, prev));
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
        <Button type="primary" icon={<PlusOutlined />} block disabled={streaming} onClick={openCreateSessionModal}>{t('createSession')}</Button>
        <div className="x-chat-active-config">
          <Text type="secondary">{isZh ? '当前任务' : 'Current task'}</Text>
          <Text strong>{activeSubtitle || '-'}</Text>

        </div>
        {!isMobile ? memoryCard : null}
      </div>
      <div className="x-chat-sidebar-list">
        <Input.Search aria-label={isZh ? '搜索会话' : 'Search conversations'} placeholder={isZh ? '搜索会话、标的' : 'Search chats or symbols'} value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} allowClear />
        <Conversations items={conversationItems} activeKey={currentSessionId} onActiveChange={(id) => { if (streaming || sendingRef.current) return; setCurrentSessionId(id); if (isMobile) setSidebarOpen(false); }} groupable />
      </div>
      <details className="content-disclosure chat-session-management">
        <summary>{isZh ? '管理当前会话' : 'Manage this conversation'}</summary>
        <div className="x-chat-session-actions">
          <Popconfirm title={t('confirmDelete')} onConfirm={clearSession} disabled={!currentSessionId || streaming}>
            <Button icon={<ClearOutlined />} disabled={!currentSessionId || streaming} block>{t('clearMessages')}</Button>
          </Popconfirm>
          <Popconfirm title={t('confirmDeleteSession')} onConfirm={deleteSession} disabled={!currentSessionId || streaming}>
            <Button icon={<DeleteOutlined />} disabled={!currentSessionId || streaming} danger block>{t('deleteSession')}</Button>
          </Popconfirm>
        </div>
      </details>
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
                    {isMobile ? <Button aria-label={t('history')} icon={<MenuOutlined />} onClick={() => setSidebarOpen(true)} /> : null}
                    <div className="x-chat-main-titles">
                      <Space size={8} wrap>
                        <Title level={4} style={{ margin: 0 }} title={currentSession?.title || undefined}>{currentSession?.title || (isZh ? '即时市场研究' : 'Live market research')}</Title>
                        {currentSession ? <Tag color={activeRuntime ? 'blue' : 'orange'}>{activeRuntime ? (isZh ? '分析' : 'Analysis') : (isZh ? '已关联交易配置' : 'Trading configuration linked')}</Tag> : null}
                        {activeModelConfig?.thinking_enabled ? (
                          <Tag color="geekblue" icon={<BulbOutlined />}>
                            {isZh ? '思考模型' : 'Reasoning model'}
                            {activeModelConfig.reasoning_effort ? ` · ${activeModelConfig.reasoning_effort}` : ''}
                          </Tag>
                        ) : null}
                      </Space>
                      <Text type="secondary" title={activeSubtitle}>{activeSubtitle}</Text>
                    </div>
                  </Space>
                  <Space size={8}>
                    {isMobile ? <Button aria-label={isZh ? '会话记忆' : 'Conversation memory'} icon={<DatabaseOutlined />} onClick={() => setMemoryOpen(true)} /> : null}
                    <Button aria-label={t('createSession')} disabled={streaming} icon={<PlusOutlined />} onClick={openCreateSessionModal}>{isMobile ? null : t('createSession')}</Button>
                  </Space>
                </div>
                <div className="x-chat-main-body">
                  <div className="x-chat-window" ref={chatWindowRef}>
                    {sessionLoading ? <div className="loading-card"><Spin /></div> : bubbleItems.length ? (
                      <Bubble.List
                        items={bubbleItems}
                        role={{
                          user: {
                            placement: 'end', variant: 'filled', shape: 'round', rootClassName: 'x-bubble-user',
                            contentRender: (content, info) => (
                              <MessageContent
                                content={content}
                                role={info?.extraInfo?.originalRole}
                                actions={info?.extraInfo?.originalRole === 'user' && Number.isInteger(info?.extraInfo?.messageIndex) ? (
                                  <MessageBranchActions
                                    branches={info?.extraInfo?.branches || []}
                                    activeSessionId={currentSessionId}
                                    onSelect={selectMessageBranch}
                                    onEdit={() => openMessageEditor(info?.extraInfo?.messageIndex)}
                                    disabled={streaming}
                                    locale={locale}
                                  />
                                ) : null}
                              />
                            ),
                          },
                          ai: {
                            placement: 'start', variant: 'shadow', shape: 'round', rootClassName: 'x-bubble-ai',
                            contentRender: (content, info) => <MessageContent content={content} reasoning={info?.extraInfo?.reasoning} reasoningTokens={info?.extraInfo?.reasoningTokens} reasoningStartedAt={info?.extraInfo?.reasoningStartedAt} role="assistant" streaming={info?.extraInfo?.streaming} status={info?.extraInfo?.status} />,
                          },
                          system: {
                            placement: 'start', variant: 'outlined', shape: 'round', rootClassName: 'x-bubble-system',
                            contentRender: (content, info) => <MessageContent content={content} role={info?.extraInfo?.originalRole} />,
                          },
                        }}
                      />
                    ) : (
                      <div className="x-chat-empty">
                        <Empty description={currentSessionId ? (isZh ? '从一个好问题开始' : 'Start with a question') : t('emptySessions')}>
                          <Paragraph type="secondary">{isZh ? '新建任务，描述你想分析的问题。任务会结合行情与共享消息摘要回答。' : 'Create a task and describe your question. Answers use market data and shared news summaries.'}</Paragraph>
                          {currentSessionId ? <div className="chat-starters">{(isZh ? ['分析当前趋势、关键价位与失效条件', '对比多空证据，哪些信号存在冲突？', '复盘近期交易，区分数据问题与执行问题'] : ['Analyze trend, levels and invalidation', 'Compare bullish and bearish evidence', 'Review recent trades and execution']).map((text) => <Button key={text} onClick={() => setInput(text)}>{text}</Button>)}</div> : <Button type="primary" icon={<PlusOutlined />} onClick={openCreateSessionModal}>{t('createSession')}</Button>}
                        </Empty>
                      </div>
                    )}
                    {showScrollToBottom ? <Button className="chat-scroll-bottom" shape="circle" icon={<DownOutlined />} onClick={scrollToBottom} aria-label={isZh ? '回到底部' : 'Scroll to bottom'} /> : null}
                  </div>
                  <ToolApproval approval={pendingApproval} loading={streaming} onApprove={() => handleApproval(true)} onReject={() => handleApproval(false)} locale={locale} />
                  <PersistenceWarning warning={persistenceWarning} locale={locale} onDismiss={() => setPersistenceWarning('')} />
                  <StreamFailureCard failure={streamFailure} onRetry={retryFailedMessage} onEdit={editFailedMessage} locale={locale} />
                  <ChatRunProgress run={run} streaming={streaming} locale={locale} />
                  <div className="x-chat-sender-wrap">
                    <Sender disabled={sessionLoading || !!pendingApproval} value={input} onChange={setInput} onSubmit={handleSend} onCancel={stopStreaming} loading={streaming} placeholder={t('chatPlaceholder')} autoSize={{ minRows: 1, maxRows: isMobile ? 5 : 7 }} submitType={isMobile ? "shiftEnter" : "enter"} />
                    <div className="chat-composer-hint">{pendingApproval ? (isZh ? "请先处理工具审批" : "Review the pending tool approval") : isZh ? (isMobile ? "点击发送 · 回车换行" : "Enter 发送 · Shift+Enter 换行") : (isMobile ? "Tap to send · Enter for newline" : "Enter to send · Shift+Enter for newline")}</div>
                  </div>
                </div>
              </section>
            </div>
          </>
        )}

        <Modal
          title={isZh ? '编辑消息并创建分支' : 'Edit message and create branch'}
          open={Boolean(editingMessage)}
          onOk={createMessageBranch}
          onCancel={() => setEditingMessage(null)}
          confirmLoading={branching}
          okButtonProps={{ disabled: !String(editingMessage?.content || '').trim() }}
          okText={isZh ? '创建分支回答' : 'Create branch'}
          cancelText={t('cancel')}
          width={isMobile ? 'calc(100vw - 24px)' : 640}
        >
          <Paragraph type="secondary">
            {isZh
              ? '原对话会保留。系统会复制这条消息之前的上下文，并从编辑后的问题生成一条新分支。'
              : 'The original conversation is preserved. A new branch copies the context before this message and answers the edited prompt.'}
          </Paragraph>
          <TextArea
            value={editingMessage?.content || ''}
            onChange={(event) => setEditingMessage((prev) => (prev ? { ...prev, content: event.target.value } : prev))}
            autoSize={{ minRows: 4, maxRows: 12 }}
            maxLength={12000}
            autoFocus
          />
        </Modal>

        <Modal
          className="chat-create-modal"
          title={t('createSession')}
          open={createModalOpen}
          width={isMobile ? 'calc(100vw - 24px)' : 640}
          onOk={createSession}
          onCancel={() => setCreateModalOpen(false)}
          confirmLoading={creating}
          okText={isZh ? '开始聊天' : 'Start chatting'}
          maskClosable={!creating}
          closable={!creating}
          cancelButtonProps={{ disabled: creating }}
          okButtonProps={{ disabled: createMode === 'task' ? !creatingConfigId : !temporaryRuntime.exchange_profile_id || !temporaryRuntime.symbol || !temporaryRuntime.llm_provider_id || !temporaryRuntime.global_requirement.trim() }}
        >
          {createError ? <Alert type="error" showIcon message={createError} style={{ marginBottom: 12 }} /> : null}
          <div className="chat-create-section">
            <Paragraph type="secondary">{isZh ? '默认分析行情与消息，选择标的后即可开始聊天。' : 'Start with market and news analysis. Choose a symbol and begin chatting.'}</Paragraph>
            <label className="form-field">
              <span>{isZh ? '关联交易配置（可选）' : 'Linked trading configuration (optional)'}</span>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder={isZh ? '仅分析行情' : 'Analyze only'}
                value={creatingConfigId || undefined}
                options={configOptions.map((item) => ({ label: `${item.name || item.config_id} · ${item.symbol} / ${item.mode}`, value: item.config_id }))}
                onChange={(value) => setCreatingConfigId(value || '')}
                style={{ width: '100%' }}
              />
            </label>
            {createMode === 'task' ? (
              <Alert type="info" showIcon message={isZh ? '使用关联配置的行情、模型和策略记忆。交易工具执行前仍需你审批。' : 'Uses the linked market, model, and strategy memory. Trading tools still require your approval.'} />
            ) : (
              <>
                <div className="chat-create-grid">
                  <label className="form-field">
                    <span>{isZh ? '标的' : 'Symbol'}</span>
                    <Select
                      showSearch
                      filterOption={false}
                      value={temporaryRuntime.symbol || undefined}
                      loading={symbolLoading}
                      options={symbolOptions.map((item) => ({ value: item.symbol, label: item.display_name || item.symbol }))}
                      placeholder={isZh ? '搜索标的' : 'Search symbols'}
                      onFocus={() => searchSymbols('')}
                      onSearch={(keyword) => { window.clearTimeout(symbolSearchTimerRef.current); symbolSearchTimerRef.current = window.setTimeout(() => searchSymbols(keyword), 300); }}
                      onChange={(value) => updateRuntime({ symbol: value })}
                    />
                  </label>
                  <label className="form-field">
                    <span>{isZh ? '模型' : 'Model'}</span>
                    <Select
                      value={temporaryRuntime.llm_provider_id || undefined}
                      options={providerOptions.map((item) => ({ value: item.provider_id, disabled: !item.api_key_configured, label: `${item.name || item.provider_id} · ${item.model || '-'}` }))}
                      placeholder={isZh ? '选择已配置的模型' : 'Choose a configured model'}
                      onChange={(value) => {
                        const provider = providerOptions.find((item) => item.provider_id === value);
                        updateRuntime({ llm_provider_id: value, system_prompt_role: provider?.system_prompt_role || 'system' });
                      }}
                    />
                  </label>
                </div>
                {!selectedProfile?.configured || !selectedProvider?.api_key_configured ? <Alert type="info" showIcon message={isZh ? '请在后台配置交易所账户和模型，再创建分析任务。' : 'Configure an exchange account and model in settings to start an analysis task.'} /> : null}
                <details className="chat-advanced-options">
                  <summary>{isZh ? '更多设置' : 'More settings'}</summary>
                  <div className="chat-create-grid">
                    <label className="form-field">
                      <span>{isZh ? '行情账户' : 'Market data account'}</span>
                      <Select
                        value={temporaryRuntime.exchange_profile_id || undefined}
                        options={exchangeProfiles.map((item) => ({ value: item.profile_id, disabled: !item.configured, label: `${item.name || item.profile_id} · ${String(item.exchange || '').toUpperCase()}` }))}
                        onChange={(value) => {
                          const profile = exchangeProfiles.find((item) => item.profile_id === value);
                          updateRuntime({ exchange_profile_id: value, market_type: profile?.supported_market_types?.includes('spot') ? 'spot' : profile?.supported_market_types?.[0] || 'spot', symbol: 'BTC/USDT' });
                          setSymbolOptions([]);
                        }}
                      />
                    </label>
                    <label className="form-field">
                      <span>{isZh ? '市场' : 'Market'}</span>
                      <Select
                        value={temporaryRuntime.market_type}
                        options={(selectedProfile?.supported_market_types || ['spot', 'swap']).map((value) => ({ value, label: value === 'spot' ? (isZh ? '现货' : 'Spot') : (isZh ? '永续合约' : 'Perpetual') }))}
                        onChange={(value) => { updateRuntime({ market_type: value, symbol: 'BTC/USDT' }); setSymbolOptions([]); }}
                      />
                    </label>
                    <label className="form-field field-span-2">
                      <span>{isZh ? '分析偏好' : 'Analysis preferences'}</span>
                      <TextArea value={temporaryRuntime.global_requirement} onChange={(event) => updateRuntime({ global_requirement: event.target.value })} autoSize={{ minRows: 2, maxRows: 5 }} maxLength={4000} placeholder={isZh ? '关注的周期、风险、关键价位与失效条件' : 'Time horizon, risks, key levels, and invalidation'} />
                    </label>
                    <label className="form-field">
                      <span>{isZh ? '提示词角色' : 'Prompt role'}</span>
                      <Select
                        value={temporaryRuntime.system_prompt_role || selectedProvider?.system_prompt_role || 'system'}
                        options={[{ value: 'system', label: 'System' }, { value: 'user', label: isZh ? 'User（兼容模式）' : 'User (compatibility)' }]}
                        onChange={(value) => updateRuntime({ system_prompt_role: value })}
                      />
                    </label>
                    <label className="form-field">
                      <span>{isZh ? '采样温度' : 'Temperature'}</span>
                      <InputNumber min={0} max={2} step={0.1} value={temporaryRuntime.temperature} placeholder={isZh ? '服务商默认值' : 'Provider default'} onChange={(value) => updateRuntime({ temperature: value })} style={{ width: '100%' }} />
                    </label>
                  </div>
                  <Text type="secondary">{isZh ? `分析周期：${temporaryTimeframes.join(' / ')}。结合共享消息摘要，保留任务中的对话上下文。` : `Analysis timeframes: ${temporaryTimeframes.join(' / ')}. Uses shared news summaries and keeps this task’s conversation context.`}</Text>
                </details>
              </>
            )}
          </div>
        </Modal>
      </div>
    </XProvider>
  );
}
