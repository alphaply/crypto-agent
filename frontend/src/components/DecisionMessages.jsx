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
  let original;
  try { original = typeof item.result === 'string' ? JSON.parse(item.result) : item.result; } catch { original = null; }
  const receipt = item.current_receipt || original;
  const structured = receipt?.receipt_version === 1;
  const filled = structured && receipt.status === 'completed' && receipt.order_status === 'filled';
  const cleanupPending = structured && receipt.cleanup?.status === 'pending';
  const observed = receipt?.verified_at || receipt?.receipt_updated_at || receipt?.position?.observed_at;
  return <div className="decision-tool-result">
    <Space wrap><Typography.Text strong>{item.tool || 'tool'}</Typography.Text>{(receipt?.status || item.status) && <Tag color={colors[item.current_receipt?.status || item.status]}>{filled ? (zh ? '本次已成交' : 'Execution filled') : (zh ? labels[item.current_receipt?.status || item.status] || item.status : item.current_receipt?.status || item.status)}</Tag>}<Typography.Text type="secondary">{item.tool_call_id}</Typography.Text></Space>
    {structured && <div style={{ marginTop: 10, overflowWrap: 'anywhere' }}>
      <Space wrap><Typography.Text strong>{zh ? '当前核验结果' : 'Current verification'}</Typography.Text>
        <Tag color={colors[receipt.status]}>{filled ? (zh ? '本次已成交' : 'Execution filled') : (zh ? labels[receipt.status] || receipt.status : receipt.status)}</Tag>
        {cleanupPending && <Tag color="warning">{zh ? '清理待核验' : 'Cleanup pending'}</Tag>}
      </Space>
      {filled && <p>{zh ? '已成交数量' : 'Filled amount'}：{receipt.filled_amount ?? '—'} · {zh ? '当前方向仓位' : 'Current position'}：{receipt.position?.status === 'verified' ? receipt.position?.amount : (zh ? '待核验' : 'Unverified')}</p>}
      {cleanupPending && <Alert type="warning" showIcon title={zh ? '相关订单核验后才能继续受影响的交易' : 'Affected trades await order verification'} description={<>
        {(receipt.cleanup.orders || []).map((order) => <div key={order.order_id || order.client_id}>{order.order_id || order.client_id} · {order.reason_code || order.order_status}</div>)}
        {receipt.cleanup.error && <div>{receipt.cleanup.error}</div>}
      </>} />}
      {observed && <Typography.Text type="secondary">{zh ? '核验时间' : 'Verified at'}：{new Date(observed * 1000).toLocaleString()}</Typography.Text>}
      {item.current_receipt && <details className="content-disclosure"><summary>{zh ? '最新结构化回执' : 'Latest receipt'}</summary><pre>{formatResult(receipt)}</pre></details>}
    </div>}
    <details className="content-disclosure"><summary>{item.current_receipt ? (zh ? `原始工具回执（${labels[item.status] || item.status}）` : `Original receipt (${item.status})`) : (zh ? '工具回执' : 'Tool result')}</summary><pre>{formatResult(item.result) || (zh ? '未返回内容' : 'No output')}</pre></details>
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
      <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={text.reasoning} reasoningTokens={entry.reasoning_tokens || 0} diagnostics={entry.reasoning_diagnostics} streaming={streaming} startedAt={startedAt} />
      <MarkdownBlock content={text.content} streaming={streaming} />
      {(entry.tool_calls || []).map((call, callIndex) => {
        const receipt = messages.slice(index + 1).find((item) => item.role === 'tool' && item.tool_call_id === call.id);
        const progress = toolCalls.find((item) => item.id === call.id);
          const status = receipt?.current_receipt?.status || receipt?.status || progress?.status || (active ? 'planned' : 'unknown');
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
