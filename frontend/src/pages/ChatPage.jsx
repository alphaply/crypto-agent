import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Empty, Input, List, Modal, Select, Space, Spin, Typography } from 'antd';
import { api, streamSse } from '../lib/api';

const { TextArea } = Input;

function normalizeAssistantDraft(draft) {
  return {
    role: 'assistant',
    content: draft.content || '',
    reasoning_content: draft.reasoning_content || '',
  };
}

export default function ChatPage({ token }) {
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
      sessions: [{ session_id: sessionId, title: '新会话', config_id: configId }, ...(prev?.sessions || [])],
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
      sessions: [{ session_id: sessionId, title: '新会话', config_id: creatingConfigId }, ...(prev?.sessions || [])],
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
      <Card className="glass-card">
        <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>
              Chat
            </Typography.Title>
            <Typography.Text type="secondary">流式对话、工具审批和会话管理。</Typography.Text>
          </div>
          <Space wrap>
            <Select
              style={{ minWidth: 220 }}
              value={currentConfigId || undefined}
              options={configOptions.map((item) => ({ label: `${item.symbol} / ${item.mode}`, value: item.config_id }))}
              onChange={setCurrentConfigId}
            />
            <Button onClick={() => setCreateModalOpen(true)}>新建会话</Button>
            <Button onClick={summarizeTitle} disabled={!currentSessionId}>
              生成标题
            </Button>
            <Button onClick={clearSession} disabled={!currentSessionId}>
              清空消息
            </Button>
          </Space>
        </Space>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="glass-card">
          <Spin />
        </Card>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
          <Card className="glass-card" title="Sessions">
            <List
              dataSource={sessionItems}
              locale={{ emptyText: <Empty description="No sessions yet" /> }}
              renderItem={(item) => (
                <List.Item
                  style={{
                    cursor: 'pointer',
                    borderRadius: 12,
                    padding: '10px 12px',
                    background: item.session_id === currentSessionId ? '#eff6ff' : 'transparent',
                  }}
                  onClick={() => setCurrentSessionId(item.session_id)}
                >
                  <List.Item.Meta title={item.title || item.session_id} description={item.symbol || item.config_id} />
                </List.Item>
              )}
            />
          </Card>

          <Card className="glass-card" title={currentSessionId ? `Session ${currentSessionId.slice(0, 8)}` : 'Chat'}>
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
              <div className="chat-window">
                {messages.length ? (
                  messages.map((message, index) => (
                    <div key={`${message.role}-${index}`} className={`chat-bubble ${message.role}`}>
                      <Typography.Text strong>{message.role}</Typography.Text>
                      <div>{message.content || '(empty)'}</div>
                      {message.reasoning_content ? (
                        <div className="chat-reasoning">
                          <Typography.Text strong>Reasoning</Typography.Text>
                          <div>{message.reasoning_content}</div>
                        </div>
                      ) : null}
                    </div>
                  ))
                ) : (
                  <Empty description="Create or select a session to start chatting" />
                )}
              </div>

              {pendingApproval ? (
                <Alert
                  type="warning"
                  showIcon
                  message={`Tool approval required: ${pendingApproval.value?.tool_name || 'Unknown tool'}`}
                  description={JSON.stringify(pendingApproval.value?.tool_args || {}, null, 2)}
                  action={
                    <Space>
                      <Button size="small" type="primary" onClick={() => handleApproval(true)} loading={streaming}>
                        Approve
                      </Button>
                      <Button size="small" danger onClick={() => handleApproval(false)} loading={streaming}>
                        Reject
                      </Button>
                    </Space>
                  }
                />
              ) : null}

              <TextArea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                rows={5}
                placeholder="输入问题或交易指令"
              />
              <Button type="primary" onClick={handleSend} loading={streaming}>
                发送
              </Button>
            </Space>
          </Card>
        </div>
      )}

      <Modal title="新建会话" open={createModalOpen} onOk={createSession} onCancel={() => setCreateModalOpen(false)}>
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
