import { Alert, Space, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import { splitThinkingContent } from '../lib/thinking';

const colors = { failed: 'error', unknown: 'warning', pending: 'warning', completed: 'success', running: 'processing' };
const labels = { failed: '失败', unknown: '待核验', pending: '待确认', completed: '已返回', not_executed: '未执行', skipped: '未执行', planned: '已规划', running: '执行中' };

function formatResult(result) {
  try { return JSON.stringify(typeof result === 'string' ? JSON.parse(result) : result, null, 2); }
  catch { return String(result || ''); }
}

export function ToolReceipt({ item, zh }) {
  return <div className="decision-tool-result">
    <Space wrap><Typography.Text strong>{item.tool || 'tool'}</Typography.Text>{item.status && <Tag color={colors[item.status]}>{zh ? labels[item.status] || item.status : item.status}</Tag>}<Typography.Text type="secondary">{item.tool_call_id}</Typography.Text></Space>
    <details className="content-disclosure"><summary>{zh ? '工具回执' : 'Tool result'}</summary><pre>{formatResult(item.result) || (zh ? '未返回内容' : 'No output')}</pre></details>
  </div>;
}

export default function DecisionMessages({ messages, zh, active = false, phase, toolCalls = [], startedAt }) {
  return <div className="decision-message-list">{messages.map((entry, index) => {
    if (entry.role === 'tool') return <ToolReceipt key={`${index}-${entry.tool_call_id}`} item={entry} zh={zh} />;
    const turn = messages.slice(0, index + 1).filter((item) => item.role === 'assistant').length;
    const text = splitThinkingContent(entry.content || '', entry.reasoning_content || '');
    const streaming = active && phase === 'thinking' && index === messages.length - 1;
    return <section className="decision-model-output" key={index}>
      <Typography.Text strong>{zh ? `第 ${turn} 轮 · 模型输出` : `Turn ${turn} · Model output`}</Typography.Text>
      {entry.output_warning && <Alert type="warning" showIcon title={entry.output_warning} />}
      <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={text.reasoning} reasoningTokens={entry.reasoning_tokens || 0} streaming={streaming} startedAt={startedAt} />
      <MarkdownBlock content={text.content} streaming={streaming} />
      {(entry.tool_calls || []).map((call, callIndex) => {
        const receipt = messages.slice(index + 1).find((item) => item.role === 'tool' && item.tool_call_id === call.id);
        const progress = toolCalls.find((item) => item.id === call.id);
        const status = receipt?.status || progress?.status || (active ? 'planned' : 'unknown');
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
