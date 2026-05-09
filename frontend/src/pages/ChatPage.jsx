import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  List,
  Modal,
  Select,
  Space,
  Spin,
  Typography,
} from 'antd';
import MarkdownBlock from '../components/MarkdownBlock';
import ReasoningBlock, { splitThinkingContent } from '../components/ReasoningBlock';
import { api, streamSse } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { TextArea } = Input;
const { Title, Paragraph, Text } = Typography;

function normalizeAssistantDraft(draft) {
  return {
    role: 'assistant',
    content: draft.content || '',
    reasoning_content: draft.reasoning_content || '',
  };
}

function MessageBubble({ message }) {
  const { t } = usePreferences();
  const normalized = splitThinkingContent(message.content || '', message.reasoning_content || '');
  const roleMap = { user: t('roleUser'), assistant: t('roleAssistant'), tool: t('roleTool'), system: t('roleSystem') };
  const roleLabel = roleMap[message.role] || message.role;

  return (
    <div className={`chat-bubble ${message.role}`}>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Text strong className="chat-role">
          {roleLabel}
        </Text>
        <MarkdownBlock content={normalized.content || '(empty)'} />
        <ReasoningBlock title={t('reasoning')} content={normalized.reasoning} />
      </Space>
    </div>
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
  const [error, setError] = useState('');
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [creatingConfigId, setCreatingConfigId] = useState('');
  const draftRef = useRef({ content: '', reasoning_content: '' });

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const response = await api.get('/chat/bootstrap');
        if (!mounted) {
          return;
        }
        setBootstrap(response.data);
        const firstConfig = response.data.configs?.[0]?.config_id || '';
        setCurrentConfigId(firstConfig);
        setCreatingConfigId(firstConfig);
        if (response.data.sessions?.[0]?.session_id) {
          setCurrentSessionId(response.data.sessions[0].session_id);
        }
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load chat bootstrap');
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      mounted = false;
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
        if (!mounted) {
          return;
        }
        setMessages(response.data.messages || []);
        setPendingApproval(response.data.pending_approval || null);
      } catch (err) {
        if (mounted) {
          setError(err.message || 'Failed to load session');
        }
      }
    }
    loadMessages();
    return () => {
      mounted = false;
    };
  }, [currentSessionId]);

  const sessionItems = useMemo(() => bootstrap?.sessions || [], [bootstrap]);
  const configOptions = useMemo(() => bootstrap?.configs || [], [bootstrap]);

  const appendDraftMessage = () => {
    draftRef.current = { content: '', reasoning_content: '' };
    setMessages((prev) => [...prev, normalizeAssistantDraft(draftRef.current)]);
  };

  const updateDraftMessage = () => {
    setMessages((prev) => {
      const next = [...prev];
      if (!next.length || next[next.length - 1].role !== 'assistant') {
        next.push(normalizeAssistantDraft(draftRef.current));
      } else {
        next[next.length - 1] = normalizeAssistantDraft(draftRef.current);
      }
      return next;
    });
  };

  const ensureSession = async () => {
    if (currentSessionId) {
      return currentSessionId;
    }
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
    appendDraftMessage();
    try {
      await streamSse(url, token, (event) => {
        if (event.type === 'token') {
          draftRef.current.content += event.token;
          updateDraftMessage();
        } else if (event.type === 'reasoning_token') {
          draftRef.current.reasoning_content += event.token;
          updateDraftMessage();
        } else if (event.type === 'done') {
          setMessages(event.messages || []);
          setPendingApproval(event.pending_approval || null);
        } else if (event.type === 'error') {
          setError(event.message || 'Stream failed');
        } else if (event.type === 'tool_result') {
          setMessages((prev) => [...prev, { role: 'tool', content: event.content }]);
        }
      });
    } finally {
      setStreaming(false);
    }
  };

  const handleSend = async () => {
    if (!input.trim()) {
      return;
    }
    const message = input;
    setInput('');
    const sessionId = await ensureSession();
    setMessages((prev) => [...prev, { role: 'user', content: message }]);
    await runStream(`/api/chat/sessions/${sessionId}/stream?message=${encodeURIComponent(message)}`);
  };

  const handleApproval = async (approved) => {
    if (!currentSessionId) {
      return;
    }
    await runStream(`/api/chat/sessions/${currentSessionId}/stream?approval=${approved}`);
  };

  const createSession = async () => {
    if (!creatingConfigId) {
      return;
    }
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
    if (!currentSessionId) {
      return;
    }
    const response = await api.post(`/chat/sessions/${currentSessionId}/summarize-title`);
    setBootstrap((prev) => ({
      ...prev,
      sessions: (prev?.sessions || []).map((item) =>
        item.session_id === currentSessionId ? { ...item, title: response.data.title } : item,
      ),
    }));
  };

  const clearSession = async () => {
    if (!currentSessionId) {
      return;
    }
    await api.post(`/chat/sessions/${currentSessionId}/clear`);
    setMessages([]);
    setPendingApproval(null);
  };

  return (
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
            <Button onClick={() => setCreateModalOpen(true)}>{t('createSession')}</Button>
            <Button onClick={summarizeTitle} disabled={!currentSessionId}>
              {t('generateTitle')}
            </Button>
            <Button onClick={clearSession} disabled={!currentSessionId}>
              {t('clearMessages')}
            </Button>
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="panel-card loading-card">
          <Spin />
        </Card>
      ) : (
        <div className="chat-layout">
          <Card className="panel-card session-panel" title={t('history')}>
            <List
              dataSource={sessionItems}
              locale={{ emptyText: <Empty description={t('emptySessions')} /> }}
              renderItem={(item) => (
                <List.Item
                  className={`session-row ${item.session_id === currentSessionId ? 'active' : ''}`}
                  onClick={() => setCurrentSessionId(item.session_id)}
                >
                  <List.Item.Meta title={item.title || item.session_id} description={item.symbol || item.config_id} />
                </List.Item>
              )}
            />
          </Card>

          <Card
            className="panel-card"
            title={currentSessionId ? `Session ${currentSessionId.slice(0, 8)}` : t('chat')}
          >
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div className="chat-window">
                {messages.length ? (
                  messages.map((message, index) => (
                    <MessageBubble key={`${message.role}-${index}`} message={message} />
                  ))
                ) : (
                  <Empty description={t('emptySessions')} />
                )}
              </div>

              {pendingApproval ? (
                <Alert
                  type="warning"
                  showIcon
                  message={t('toolApproval')}
                  description={
                    <div>
                      <Text strong>{pendingApproval.value?.tool_name || 'Unknown tool'}</Text>
                      <pre className="approval-json">
                        {JSON.stringify(pendingApproval.value?.tool_args || {}, null, 2)}
                      </pre>
                    </div>
                  }
                  action={
                    <Space>
                      <Button size="small" type="primary" onClick={() => handleApproval(true)} loading={streaming}>
                        {t('approve')}
                      </Button>
                      <Button size="small" danger onClick={() => handleApproval(false)} loading={streaming}>
                        {t('reject')}
                      </Button>
                    </Space>
                  }
                />
              ) : null}

              <TextArea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                rows={5}
                placeholder={t('chatPlaceholder')}
              />
              <Button type="primary" onClick={handleSend} loading={streaming}>
                {t('send')}
              </Button>
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
  );
}
