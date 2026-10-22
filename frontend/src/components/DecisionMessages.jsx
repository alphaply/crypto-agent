import { Alert, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import { splitThinkingContent } from '../lib/thinking';
import AuditDisclosure from './AuditDisclosure';
import ToolResultContent from './ToolResultContent';
import { isTradeReceipt, isTradingTool, toolRequest } from '../lib/toolReceipt';

const colors = { failed: 'error', unknown: 'warning', pending: 'warning', partial: 'warning', completed: 'success', submitted: 'processing', running: 'processing' };
const labels = { failed: '失败', unknown: '待核验', pending: '待确认', partial: '部分返回', submitted: '已提交', completed: '已返回', not_executed: '未执行', skipped: '未执行', planned: '已规划', running: '执行中' };


function invocationStatus(item) {
  if (!item) return null;
  let receipt = item.current_receipt;
  try { receipt ||= typeof item.result === 'string' ? JSON.parse(item.result) : item.result; } catch { /* legacy text */ }
  return item.tool === 'get_trade_operation' && receipt?.query ? receipt.query.status : receipt?.status || item.status;
}

function formatResult(result) {
  try { return JSON.stringify(typeof result === 'string' ? JSON.parse(result) : result, null, 2); }
  catch { return String(result || ''); }
}

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
      <MarkdownBlock content={text.content} streaming={streaming} />
      {(entry.tool_calls || []).map((call, callIndex) => {
        if (isTradingTool(call.name)) return null;
        const receipt = messages.slice(index + 1).find((item) => item.role === 'tool' && item.tool_call_id === call.id);
        const progress = toolCalls.find((item) => item.id === call.id);
          const status = invocationStatus(receipt) || progress?.status || (active ? 'planned' : 'unknown');
        return <details className="content-disclosure decision-tool-call" key={call.id || callIndex}>
          <summary>{zh ? '调用工具' : 'Tool call'} · {call.name} <Tag color={colors[status]}>{zh ? labels[status] || status : status}</Tag></summary>
          <pre>{formatResult(call.args)}</pre>
        </details>;
      })}
    </section>;
  })}
    {active && phase === 'thinking' && messages.at(-1)?.role === 'tool' && <ReasoningBlock streaming startedAt={startedAt} />}
  </div>;
}
