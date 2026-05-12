import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Typography,
} from 'antd';
import { ClearOutlined, EditOutlined, PlusOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { Bubble, Conversations, Sender, XProvider } from '@ant-design/x';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock, { splitThinkingContent } from '../components/ReasoningBlock';
import { api, streamSse } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Title, Paragraph, Text } = Typography;

function normalizeAssistantDraft(draft) {
  return {
    role: 'assistant',
    content: draft.content || '',
    reasoning_content: draft.reasoning_content || '',
  };
}

function MessageContent({ content, reasoning }) {
  const { t } = usePreferences();
  const normalized = splitThinkingContent(content || '', reasoning || '');
  if (!normalized.content?.trim() && !normalized.reasoning?.trim()) return null;
  return (
    <div className="chat-message-content">
      <MarkdownBlock content={normalized.content || ''} />
      <ReasoningBlock title={t('reasoning')} content={normalized.reasoning} />
    </div>
  );
}

function hasRenderableMessage(message) {
  if (message.role !== 'assistant') return true;
  return Boolean(String(message.content || '').trim() || String(message.reasoning_content || '').trim());
}

function ToolApproval({ approval, loading, onApprove, onReject }) {
  const { t } = usePreferences();
  if (!approval) return null;

  return (
    <Alert
      className="tool-approval-card"
      type="warning"
      showIcon
      icon={<SafetyCertificateOutlined />}
      message={t('toolApproval')}
      description={
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text strong>{approval.value?.tool_name || 'Unknown tool'}</Text>
          <pre className="approval-json">
            {JSON.stringify(approval.value?.tool_args || {}, null, 2)}
          </pre>
        </Space>
      }
      action={
        <Space>
          <Button size="small" type="primary" onClick={onApprove} loading={loading}>
            {t('approve')}
          </Button>
          <Button size="small" danger onClick={onReject} loading={loading}>
            {t('reject')}
          </Button>
        </Space>
      }
    />
  );
}

export default function ChatPage({ token }) {
  const { t } = usePreferences();
  const [bootstrap, setBootstrap] = useState(null);
  const [currentSessionId, setCurrentSessionId] = useState('');
  const [currentConfigId, setCurrentConfigId] = useState('');
  const [messages, setMessages] = useState([]);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const [error, setError] = useState('');
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [creatingConfigId, setCreatingConfigId] = useState('');
  const draftRef = useRef({ content: '', reasoning_content: '' });
  const flushTimerRef = useRef(null);

  const flushDraftMessage = (immediate = false) => {
    if (flushTimerRef.current) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }

    const flush = () => {
      const draft = normalizeAssistantDraft(draftRef.current);
      if (!draft.content.trim() && !draft.reasoning_content.trim()) return;
      setMessages((prev) => {
        const next = [...prev];
        if (!next.length || next[next.length - 1].role !== 'assistant') {
          next.push(draft);
        } else {
          next[next.length - 1] = draft;
        }
        return next;
      });
    };

    if (immediate) {
      flush();
    } else {
      flushTimerRef.current = window.setTimeout(flush, 50);
    }
  };

  const appendDraftMessage = () => {
    draftRef.current = { content: '', reasoning_content: '' };
  };

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const response = await api.get('/chat/bootstrap');
        if (!mounted) return;
        setBootstrap(response.data);
        const firstConfig = response.data.configs?.[0]?.config_id || '';
        setCurrentConfigId(firstConfig);
        setCreatingConfigId(firstConfig);
        if (response.data.sessions?.[0]?.session_id) {
          setCurrentSessionId(response.data.sessions[0].session_id);
        }
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load chat bootstrap');
      } finally {
        if (mounted) setLoading(false);
      }
    }
    load();
    return () => {
      mounted = false;
      if (flushTimerRef.current) window.clearTimeout(flushTimerRef.current);
    };
  }, []);

  useEffect(() => {
    let mounted = true;
    async function loadMessages() {
      if (!currentSessionId) {
        setMessages([]);
        setPendingApproval(null);
        return;
      }
      try {
        const response = await api.get(`/chat/sessions/${currentSessionId}`);
        if (!mounted) return;
        setMessages((response.data.messages || []).filter(hasRenderableMessage));
        setPendingApproval(response.data.pending_approval || null);
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load session');
      }
    }
    loadMessages();
    return () => {
      mounted = false;
    };
  }, [currentSessionId]);

  const sessionItems = useMemo(() => bootstrap?.sessions || [], [bootstrap]);
  const configOptions = useMemo(() => bootstrap?.configs || [], [bootstrap]);

  const conversationItems = useMemo(
    () =>
      sessionItems.map((item) => ({
        key: item.session_id,
        label: item.title || item.session_id,
        timestamp: item.updated_at,
        group: item.symbol || item.config_id,
      })),
    [sessionItems],
  );

  const bubbleItems = useMemo(
    () =>
      messages.filter(hasRenderableMessage).map((message, index) => {
        const role = message.role === 'assistant' ? 'ai' : message.role === 'user' ? 'user' : 'system';
        return {
          key: `${message.role}-${index}`,
          role,
          content: message.content || '',
          extraInfo: {
            reasoning: message.reasoning_content || '',
            originalRole: message.role,
          },
          streaming: streaming && index === messages.length - 1 && message.role === 'assistant',
        };
      }),
    [messages, streaming],
  );

  const ensureSession = async () => {
    if (currentSessionId) return currentSessionId;
    const configId = currentConfigId || configOptions[0]?.config_id;
    const response = await api.post('/chat/sessions', { config_id: configId });
    const sessionId = response.data.session_id;
    setCurrentSessionId(sessionId);
    setBootstrap((prev) => ({
      ...prev,
      sessions: [{ session_id: sessionId, title: 'New chat', config_id: configId }, ...(prev?.sessions || [])],
    }));
    return sessionId;
  };

  const runStream = async (url) => {
    setStreaming(true);
    setStreamStatus('');
    appendDraftMessage();
    try {
      await streamSse(url, token, (event) => {
        if (event.type === 'token') {
          draftRef.current.content += event.token;
          flushDraftMessage();
        } else if (event.type === 'reasoning_token') {
          draftRef.current.reasoning_content += event.token;
          flushDraftMessage();
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
          flushDraftMessage(true);
          setMessages((event.messages || []).filter(hasRenderableMessage));
          setPendingApproval(event.pending_approval || null);
          setStreamStatus('');
        } else if (event.type === 'error') {
          setError(event.message || 'Stream failed');
        }
      });
    } finally {
      flushDraftMessage(true);
      setStreaming(false);
    }
  };

  const handleSend = async (value = input) => {
    if (!value.trim() || streaming) return;
    const message = value.trim();
    setInput('');
    setPendingApproval(null);
    const sessionId = await ensureSession();
    setMessages((prev) => [...prev, { role: 'user', content: message }]);
    await runStream(`/api/chat/sessions/${sessionId}/stream?message=${encodeURIComponent(message)}`);
  };

  const handleApproval = async (approved) => {
    if (!currentSessionId || streaming) return;
    setPendingApproval(null);
    await runStream(`/api/chat/sessions/${currentSessionId}/stream?approval=${approved}`);
  };

  const createSession = async () => {
    if (!creatingConfigId) return;
    const response = await api.post('/chat/sessions', { config_id: creatingConfigId });
    const sessionId = response.data.session_id;
    setBootstrap((prev) => ({
      ...prev,
      sessions: [{ session_id: sessionId, title: 'New chat', config_id: creatingConfigId }, ...(prev?.sessions || [])],
    }));
    setCurrentSessionId(sessionId);
    setCurrentConfigId(creatingConfigId);
    setCreateModalOpen(false);
  };

  const summarizeTitle = async () => {
    if (!currentSessionId) return;
    const response = await api.post(`/chat/sessions/${currentSessionId}/summarize-title`);
    setBootstrap((prev) => ({
      ...prev,
      sessions: (prev?.sessions || []).map((item) =>
        item.session_id === currentSessionId ? { ...item, title: response.data.title } : item,
      ),
    }));
  };

  const clearSession = async () => {
    if (!currentSessionId) return;
    await api.post(`/chat/sessions/${currentSessionId}/clear`);
    setMessages([]);
    setPendingApproval(null);
    draftRef.current = { content: '', reasoning_content: '' };
  };

  return (
    <XProvider>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Card className="hero-card">
          <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
            <div>
              <Title level={2} style={{ margin: 0 }}>
                {t('chat')}
              </Title>
              <Paragraph type="secondary" style={{ marginBottom: 0 }}>
                {t('chatPageDesc')}
              </Paragraph>
            </div>
            <Space wrap>
              <Select
                style={{ minWidth: 240 }}
                value={currentConfigId || undefined}
                options={configOptions.map((item) => ({ label: `${item.symbol} / ${item.mode}`, value: item.config_id }))}
                onChange={setCurrentConfigId}
              />
              <Button icon={<PlusOutlined />} onClick={() => setCreateModalOpen(true)}>
                {t('createSession')}
              </Button>
              <Button icon={<EditOutlined />} onClick={summarizeTitle} disabled={!currentSessionId}>
                {t('generateTitle')}
              </Button>
              <Popconfirm title={t('confirmDelete')} onConfirm={clearSession} disabled={!currentSessionId}>
                <Button icon={<ClearOutlined />} disabled={!currentSessionId}>
                  {t('clearMessages')}
                </Button>
              </Popconfirm>
            </Space>
          </Space>
        </Card>

        {error ? <Alert type="error" message={error} showIcon closable onClose={() => setError('')} /> : null}

        {loading ? (
          <Card className="panel-card loading-card">
            <Spin />
          </Card>
        ) : (
          <div className="chat-layout x-chat-layout">
            <Card className="panel-card session-panel x-session-panel" title={t('history')}>
              <Conversations
                items={conversationItems}
                activeKey={currentSessionId}
                onActiveChange={setCurrentSessionId}
                groupable
                creation={{
                  onClick: () => setCreateModalOpen(true),
                  label: t('createSession'),
                }}
              />
            </Card>

            <Card className="panel-card x-chat-card" title={currentSessionId ? `Session ${currentSessionId.slice(0, 8)}` : t('chat')}>
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div className="x-chat-window">
                  {bubbleItems.length ? (
                    <Bubble.List
                      autoScroll
                      items={bubbleItems}
                      role={{
                        user: {
                          placement: 'end',
                          variant: 'filled',
                          shape: 'round',
                          rootClassName: 'x-bubble-user',
                          contentRender: (content) => <MessageContent content={content} />,
                        },
                        ai: {
                          placement: 'start',
                          variant: 'shadow',
                          shape: 'round',
                          rootClassName: 'x-bubble-ai',
                          typing: { effect: 'fade-in', step: [6, 12], interval: 30, keepPrefix: true },
                          contentRender: (content, info) => (
                            <MessageContent content={content} reasoning={info?.extraInfo?.reasoning} />
                          ),
                        },
                        system: {
                          placement: 'start',
                          variant: 'outlined',
                          shape: 'round',
                          rootClassName: 'x-bubble-system',
                          contentRender: (content) => <MessageContent content={content} />,
                        },
                      }}
                    />
                  ) : (
                    <div className="x-chat-empty">
                      <Empty description={t('emptySessions')} />
                    </div>
                  )}
                </div>

                {streamStatus ? <Alert type="info" showIcon message={streamStatus} /> : null}

                <ToolApproval
                  approval={pendingApproval}
                  loading={streaming}
                  onApprove={() => handleApproval(true)}
                  onReject={() => handleApproval(false)}
                />

                <Sender
                  value={input}
                  onChange={setInput}
                  onSubmit={handleSend}
                  loading={streaming}
                  placeholder={t('chatPlaceholder')}
                  autoSize={{ minRows: 3, maxRows: 8 }}
                  submitType="enter"
                />
              </Space>
            </Card>
          </div>
        )}

        <Modal title={t('createSession')} open={createModalOpen} onOk={createSession} onCancel={() => setCreateModalOpen(false)}>
          <Select
            style={{ width: '100%' }}
            value={creatingConfigId || undefined}
            options={configOptions.map((item) => ({ label: `${item.symbol} / ${item.mode}`, value: item.config_id }))}
            onChange={setCreatingConfigId}
          />
        </Modal>
      </Space>
    </XProvider>
  );
}
