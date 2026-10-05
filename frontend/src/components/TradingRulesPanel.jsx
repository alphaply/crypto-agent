import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Form, Input, List, Modal, Select, Space, Spin, Switch, Tag, Typography, message } from 'antd';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

const { Text, Paragraph } = Typography;

function TaskTradingRules({ configId }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({ content: '', enabled: true, locked: true, reason: '' });
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [historyRule, setHistoryRule] = useState(null);
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const loadSequence = useRef(0);
  const historySequence = useRef(0);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError('');
    try {
      const response = await api.get('/history/trading-rules', { params: { config_id: configId } });
      if (mounted.current && sequence === loadSequence.current) setRules(response.data.rules || []);
    } catch (err) {
      if (mounted.current && sequence === loadSequence.current) setError(err.message);
    } finally {
      if (mounted.current && sequence === loadSequence.current) setLoading(false);
    }
  }, [configId]);

  useEffect(() => {
    mounted.current = true;
    const timer = window.setTimeout(() => load(), 0);
    return () => { mounted.current = false; window.clearTimeout(timer); };
  }, [load]);

  const openEditor = (rule = {}) => {
    setEditing(rule);
    setDraft({ content: rule.content || '', enabled: rule.enabled ?? true, locked: rule.locked ?? true, reason: '' });
    setConflict(false);
    setEditError('');
  };

  const save = async () => {
    if (!draft.content.trim() || saving || conflict) return;
    setSaving(true);
    setEditError('');
    try {
      const payload = { config_id: configId, ...draft, content: draft.content.trim() };
      if (editing.rule_id) {
        await api.patch(`/history/trading-rules/${encodeURIComponent(editing.rule_id)}`, { ...payload, expected_revision: editing.revision });
      } else {
        await api.post('/history/trading-rules', payload);
      }
      if (!mounted.current) return;
      setEditing(null);
      message.success(zh ? '交易规则已保存，下次决策生效' : 'Rule saved for the next decision');
      await load();
    } catch (err) {
      if (!mounted.current) return;
      const stale = err.response?.status === 409;
      setConflict(stale);
      setEditError(stale
        ? (zh ? '规则已被其他操作更新。草稿已保留，请复制需要保留的内容，关闭后重新打开最新规则。' : 'This rule changed elsewhere. Your draft is preserved. Copy any changes you need, then close and reopen the latest rule.')
        : err.message);
      if (stale) await load();
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const showHistory = async (rule) => {
    const sequence = ++historySequence.current;
    setHistoryRule(rule);
    setHistory([]);
    setHistoryError('');
    setHistoryLoading(true);
    try {
      const response = await api.get(`/history/trading-rules/${encodeURIComponent(rule.rule_id)}/history`, { params: { config_id: configId } });
      if (mounted.current && sequence === historySequence.current) setHistory(response.data.history || []);
    } catch (err) {
      if (mounted.current && sequence === historySequence.current) setHistoryError(err.message);
    } finally {
      if (mounted.current && sequence === historySequence.current) setHistoryLoading(false);
    }
  };

  const actorLabel = (actor) => actor === 'model' ? (zh ? '模型' : 'Model') : actor === 'human' ? (zh ? '人工' : 'Human') : (actor || '—');
  const flags = (rule) => <Space size={4} wrap><Tag color={rule.enabled ? 'green' : 'default'}>{rule.enabled ? (zh ? '启用' : 'Enabled') : (zh ? '停用' : 'Disabled')}</Tag><Tag color={rule.locked ? 'gold' : 'blue'}>{rule.locked ? (zh ? '人工锁定' : 'Human locked') : (zh ? '模型可修改' : 'Model editable')}</Tag><Tag>v{rule.revision}</Tag></Space>;

  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Space wrap>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor()}>{zh ? '添加规则' : 'Add rule'}</Button>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>{zh ? '刷新' : 'Refresh'}</Button>
    </Space>
    {error ? <Alert showIcon type="error" title={error} /> : null}
    <List loading={loading} dataSource={rules} locale={{ emptyText: <Empty description={zh ? '此任务暂无交易规则' : 'No trading rules for this task'} /> }} renderItem={(rule) => (
      <List.Item>
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          <Space wrap>{flags(rule)}<Text type="secondary">{actorLabel(rule.updated_by)} · {rule.updated_at}</Text></Space>
          <Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBottom: 0 }}>{rule.content}</Paragraph>
          <Space><Button size="small" onClick={() => openEditor(rule)}>{zh ? '编辑 / 状态' : 'Edit / status'}</Button><Button size="small" onClick={() => showHistory(rule)}>{zh ? '版本记录' : 'Version history'}</Button></Space>
        </Space>
      </List.Item>
    )} />
    <Modal title={editing?.rule_id ? (zh ? '编辑交易规则' : 'Edit trading rule') : (zh ? '添加交易规则' : 'Add trading rule')} open={Boolean(editing)} onCancel={() => { if (!saving) setEditing(null); }} onOk={save} confirmLoading={saving} okText={zh ? '保存规则' : 'Save rule'} cancelText={zh ? '取消' : 'Cancel'} okButtonProps={{ disabled: !draft.content.trim() || conflict }} width={720}>
      <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
        {editError ? <Alert type="error" showIcon title={editError} /> : null}
        <Form layout="vertical" style={{ width: '100%' }}>
          <Form.Item label={zh ? '规则内容' : 'Rule'} required><Input.TextArea aria-label={zh ? '规则内容' : 'Rule content'} rows={6} maxLength={1200} showCount value={draft.content} onChange={(event) => setDraft((previous) => ({ ...previous, content: event.target.value }))} disabled={saving} /></Form.Item>
          <Form.Item label={zh ? '启用规则' : 'Enabled'}><Switch checked={draft.enabled} onChange={(enabled) => setDraft((previous) => ({ ...previous, enabled }))} disabled={saving} /></Form.Item>
          <Form.Item label={zh ? '人工锁定' : 'Human lock'} extra={zh ? '锁定后，模型不能修改或停用此规则；管理员仍可编辑。' : 'The model cannot edit or disable a locked rule. Administrators can still edit it.'}><Switch checked={draft.locked} onChange={(locked) => setDraft((previous) => ({ ...previous, locked }))} disabled={saving} /></Form.Item>
          <Form.Item label={zh ? '修改原因（可选）' : 'Reason (optional)'}><Input.TextArea rows={2} maxLength={1000} value={draft.reason} onChange={(event) => setDraft((previous) => ({ ...previous, reason: event.target.value }))} disabled={saving} /></Form.Item>
        </Form>
      </Space>
    </Modal>
    <Modal title={zh ? '交易规则版本记录' : 'Trading rule history'} open={Boolean(historyRule)} footer={null} onCancel={() => { ++historySequence.current; setHistoryRule(null); }} width={760}>
      {historyError ? <Alert type="error" showIcon title={historyError} /> : null}
      {historyLoading ? <Spin /> : <List dataSource={history} locale={{ emptyText: zh ? '暂无版本记录' : 'No version history' }} renderItem={(entry) => <List.Item><Space direction="vertical" style={{ width: '100%' }}><Space wrap>{flags(entry)}<Text type="secondary">{actorLabel(entry.actor)} · {entry.created_at}</Text></Space><Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBottom: 0 }}>{entry.content}</Paragraph>{entry.reason ? <Text type="secondary">{zh ? '原因：' : 'Reason: '}{entry.reason}</Text> : null}</Space></List.Item>} />}
    </Modal>
  </Space>;
}

export default function TradingRulesPanel({ agents = [] }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const [requestedConfigId, setRequestedConfigId] = useState(null);
  const configId = agents.some((agent) => agent.config_id === requestedConfigId) ? requestedConfigId : agents[0]?.config_id;
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Text type="secondary">{zh ? '规则按任务独立保存。交易 Agent 只读取并执行规则；记忆复盘 Agent 可维护未锁定的规则。人工新增默认锁定，停用保留版本记录。' : 'Rules belong to each task. The trading agent reads and follows them; the memory review agent can maintain unlocked rules. Human-created rules are locked by default. Disabling keeps their history.'}</Text>
    <Select aria-label={zh ? '规则所属任务' : 'Rule task'} value={configId} onChange={setRequestedConfigId} style={{ width: '100%', maxWidth: 440 }} options={agents.map((agent) => ({ value: agent.config_id, label: `${agent.title || agent.config_id} · ${agent.symbol || ''}` }))} placeholder={zh ? '选择任务' : 'Select task'} />
    {configId ? <TaskTradingRules key={configId} configId={configId} /> : <Empty description={zh ? '请先创建并保存一个任务' : 'Create and save a task first'} />}
  </Space>;
}
