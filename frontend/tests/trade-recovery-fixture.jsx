import React from 'react';
import { createRoot } from 'react-dom/client';
import { Card, Typography } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import DecisionMessages, { ToolReceipt } from '../src/components/DecisionMessages';
import ReasoningBlock from '../src/components/ReasoningBlock';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';

const original = { status: 'unknown', contract_version: 2, error: 'Cancellation confirmed but cumulative fill is unverified; reconcile the existing order ID' };
const receipt = { receipt_version: 1, contract_version: 2, operation_id: 'example-close', action: 'close', status: 'completed', order_status: 'filled', order_id: 'regular:example-close', filled_amount: .5,
  position: { side: 'SHORT', amount: 0, status: 'verified', observed_at: 1791770400 }, verified_at: 1791770400,
  execution: { phase: 'verify', submission_state: 'submitted', fill_status: 'verified' },
  cleanup: { status: 'pending', orders: [{ order_id: 'algo:example-stop', reason_code: 'fill_unverified' }], error: '撤销已确认，累计成交量仍待核验。' },
  recovery: { tool: 'get_trade_operation', target_operation_id: 'example-close' } };
const diag = { model: 'claude-sonnet-5.5', protocol: 'openai', requested_effort: 'high', sent_parameters: { reasoning_effort: 'high' }, evidence_state: 'requested_no_evidence' };
createRoot(document.getElementById('root')).render(<AccessibleConfigProvider><PreferencesContext.Provider value={{ isDark: false, locale: 'zh', t: (value) => value }}>
  <main style={{ maxWidth: 1040, margin: 'auto', padding: 16, display: 'grid', gap: 18 }}>
    <Typography.Title level={2}>交易恢复与推理诊断 · 离线验收</Typography.Title>
    <Typography.Paragraph>合成数据，不代表线上订单状态。原消息保留，当前核验结果单独展示。</Typography.Paragraph>
    <Card title="原故障回执"><ToolReceipt zh item={{ tool: 'close', tool_call_id: 'example-close', status: 'unknown', result: original }} /></Card>
    <Card title="平仓成交，旧保护单待核验"><ToolReceipt zh item={{ tool: 'close', tool_call_id: 'example-close', status: 'unknown', result: original, current_receipt: receipt }} /></Card>
    <Card title="清理核实后，可显式继续交易"><DecisionMessages zh messages={[
      { role: 'assistant', content: '查询原操作后，读取最新核验结果。', tool_calls: [{ id: 'example-close', name: 'close', args: { side: 'SHORT', order_type: 'market', close_all: true } }] },
      { role: 'tool', tool: 'close', tool_call_id: 'example-close', status: 'unknown', result: original, current_receipt: { ...receipt, cleanup: { status: 'completed', orders: [] } } },
    ]} /></Card>
    <Card title="推理展示的四种证据状态">
      <ReasoningBlock diagnostics={diag} />
      <ReasoningBlock diagnostics={{ ...diag, protocol: 'anthropic', evidence_state: 'usage_only' }} reasoningTokens={145} />
      <ReasoningBlock diagnostics={{ ...diag, model: 'example-reasoning-model', requested_effort: 'none', evidence_state: 'disabled', sent_parameters: { reasoning_effort: 'none' } }} />
      <ReasoningBlock diagnostics={{ ...diag, evidence_state: 'text_available' }} content="服务商公开返回的简短推理摘要。" />
    </Card>
  </main>
</PreferencesContext.Provider></AccessibleConfigProvider>);
