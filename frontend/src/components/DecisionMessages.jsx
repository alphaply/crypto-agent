import { Alert, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import { splitThinkingContent } from '../lib/thinking';
import AuditDisclosure from './AuditDisclosure';
import ToolResultContent from './ToolResultContent';
import { isTradeReceipt, toolRequest } from '../lib/toolReceipt';
import { pendingDecisionTools } from '../lib/toolActivity';
import { ModelToolDiagnostic, ToolActivity } from './ToolActivity';

export function ToolReceipt({ item, zh, request }) {
  const trading = isTradeReceipt(item.current_receipt ?? item.result, item.tool || item.name);
  return <div className="decision-tool-result">
    <ToolResultContent content={item.current_receipt ?? item.result} zh={zh} toolName={item.tool || item.name} fallbackStatus={item.status} request={request ?? item.args ?? item.arguments} />
    {!trading && item.audit?.url && <details className="content-disclosure"><summary>{zh ? '审计记录' : 'Audit record'}</summary><AuditDisclosure url={item.audit.url} zh={zh} /></details>}
  </div>;
}

export default function DecisionMessages({ messages, zh, active = false, phase, toolCalls = [], startedAt }) {
  return <div className="decision-message-list">{messages.map((entry, index) => {
    if (entry.role === 'tool') {
      return <ToolReceipt key={`${index}-${entry.tool_call_id}`} item={entry} zh={zh} request={toolRequest(messages, entry)} />;
    }
    const turn = messages.slice(0, index + 1).filter((item) => item.role === 'assistant').length;
    const text = splitThinkingContent(entry.content || '', entry.reasoning_content || '');
    const streaming = active && phase === 'thinking' && index === messages.length - 1;
    return <section className="decision-model-output" key={index}>
      <Typography.Text strong>{zh ? `第 ${turn} 轮 · 模型输出` : `Turn ${turn} · Model output`}</Typography.Text>
      {entry.output_warning && <Alert type="warning" showIcon title={entry.output_warning} />}
      <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={text.reasoning} reasoningTokens={entry.reasoning_tokens || 0} diagnostics={entry.reasoning_diagnostics} streaming={streaming} startedAt={startedAt} />
      <MarkdownBlock content={text.content} streaming={streaming} collapseJsonLabel={zh ? '查看模型给出的 JSON（仅文本）' : 'View model JSON (text only)'} />
      {text.content && <ModelToolDiagnostic calls={entry.tool_calls} diagnostics={entry.reasoning_diagnostics} zh={zh} streaming={streaming} />}
      {pendingDecisionTools(entry.tool_calls || [], messages.slice(index + 1), toolCalls, { active, streaming }).map((call, callIndex) =>
        <ToolActivity key={call.id || callIndex} name={call.name} args={call.args} status={call.status} zh={zh} />)}
    </section>;
  })}
    {active && phase === 'thinking' && messages.at(-1)?.role === 'tool' && <ReasoningBlock streaming startedAt={startedAt} />}
  </div>;
}
