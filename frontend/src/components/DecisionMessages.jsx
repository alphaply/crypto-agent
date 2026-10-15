import { Alert, Space, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import { splitThinkingContent } from '../lib/thinking';
import AuditDisclosure from './AuditDisclosure';

const colors = { failed: 'error', unknown: 'warning', pending: 'warning', partial: 'warning', completed: 'success', submitted: 'processing', running: 'processing' };
const labels = { failed: '失败', unknown: '待核验', pending: '待确认', partial: '部分返回', submitted: '已提交', completed: '已返回', not_executed: '未执行', skipped: '未执行', planned: '已规划', running: '执行中' };
const reasons = { order_evidence_unverified: '相关订单执行结果尚未核实', position_evidence_unverified: '当前仓位尚未核实',
  cycle_cleanup_unverified: '当前周期仍有相关清理事项', one_way_shared_position_unverified: '单向模式共享净仓尚未核实',
  fill_unverified: '成交量待核验', order_unresolved: '订单结果待核验', previous_order_rejected: '旧订单已明确拒绝，不阻塞新请求',
  historical_diagnostic: '历史维护告警，仅作诊断' };

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

export function ToolReceipt({ item, zh }) {
  let original;
  try { original = typeof item.result === 'string' ? JSON.parse(item.result) : item.result; } catch { original = null; }
  const receipt = item.current_receipt || original;
  const structured = receipt?.receipt_version === 1;
  const filled = structured && receipt.status === 'completed' && receipt.order_status === 'filled';
  const cleanupPending = structured && receipt.cleanup?.status === 'pending';
  const observed = receipt?.verified_at || receipt?.receipt_updated_at || receipt?.position?.observed_at;
  const status = invocationStatus(item);
  const isQuery = item.tool === 'get_trade_operation' && receipt?.query;
  const continuation = receipt?.continuation;
  const scope = receipt?.current_scope || continuation?.scope;
  return <div className="decision-tool-result" style={{ overflowWrap: 'anywhere', minWidth: 0 }}>
    <Space wrap><Typography.Text strong>{item.tool || 'tool'}</Typography.Text>{status && <Tag color={colors[status]}>{isQuery ? (zh ? `查询${labels[status] || status}` : `Query ${status}`) : filled ? (zh ? '本次已成交' : 'Execution filled') : (zh ? labels[status] || status : status)}</Tag>}<Typography.Text type="secondary">{item.tool_call_id}</Typography.Text></Space>
    {structured && <div style={{ marginTop: 10, overflowWrap: 'anywhere' }}>
      <Space wrap><Typography.Text strong>{isQuery ? (zh ? '原交易结果' : 'Original trade result') : (zh ? '当前核验结果' : 'Current verification')}</Typography.Text>
        <Tag color={colors[receipt.status]}>{filled ? (zh ? '本次已成交' : 'Execution filled') : (zh ? labels[receipt.status] || receipt.status : receipt.status)}</Tag>
        {cleanupPending && <Tag color="warning">{zh ? '清理待核验' : 'Cleanup pending'}</Tag>}
      </Space>
      {receipt.order_status && <p>{zh ? '订单状态' : 'Order state'}: {receipt.order_status} · {zh ? '请求数量' : 'Requested amount'}: {receipt.requested_amount ?? receipt.amount ?? '—'} · {zh ? '已确认成交量' : 'Verified filled amount'}: {receipt.filled_amount ?? '—'}</p>}
      {(receipt.order_id || receipt.client_id) && <p>{zh ? '订单身份' : 'Order identity'}: {receipt.order_id || receipt.client_id}</p>}
      {isQuery && receipt.order_status === 'rejected' && <p>{zh ? '原交易已拒绝；查询返回不代表交易成功。' : 'Original trade rejected; a successful query is not a successful trade.'}</p>}
      {scope && <p>{zh ? '当前作用范围' : 'Current scope'}：{scope.symbol || '—'} · {scope.side || '—'}</p>}
      {continuation && <p>{continuation.assessment_state === 'needs_refresh'
        ? (zh ? '建议待刷新；新动作会在本次调用内重新取证。' : 'Advisory needs refresh; each action refreshes its own evidence.')
        : (zh ? '以下限制仅适用于所列范围；每笔新动作会自动核验，无需先查询解锁。' : 'Restrictions apply only to the listed scope; each action assesses its own dependencies.')}</p>}
      {(continuation?.assessment_state === 'needs_refresh' ? [] : continuation?.rules || []).map((rule, index) => <Alert key={index} type="warning" showIcon style={{ marginBottom: 8 }}
        title={`${rule.action} · ${rule.target || rule.scope?.side || ''}`}
        description={<>{zh ? reasons[rule.reason_code] || rule.reason_code : rule.reason_code}{(rule.blockers || []).map((blocker, i) => <div key={i}>{blocker.order_id || blocker.client_id || blocker.missing_evidence} · {zh ? reasons[blocker.reason_code] || blocker.reason_code : blocker.reason_code}</div>)}
          <div>{zh ? '系统在后台更新相关事实，无关动作单独判断。' : 'The system updates related evidence in the background. Unrelated actions are assessed separately.'}</div></>} />)}
      {(receipt.admission?.warnings || []).length > 0 && <p>{zh ? '历史告警：' : 'Historical warnings: '}{receipt.admission.warnings.map(w => zh ? reasons[w.reason_code] || w.reason_code : w.reason_code).join(' · ')}</p>}
      {receipt.query?.errors?.length > 0 && <Alert type="warning" title={zh ? '部分证据未返回' : 'Evidence unavailable'} description={receipt.query.errors.join(' · ')} />}
      {filled && <p>{zh ? '已成交数量' : 'Filled amount'}：{receipt.filled_amount ?? '—'} · {zh ? '当前方向仓位' : 'Current position'}：{receipt.position?.status === 'verified' ? receipt.position?.amount : (zh ? '待核验' : 'Unverified')}</p>}
      {cleanupPending && <Alert type="warning" showIcon title={zh ? '相关订单核验后才能继续受影响的交易' : 'Affected trades await order verification'} description={<>
        {(receipt.cleanup.orders || []).map((order) => <div key={order.order_id || order.client_id}>{order.order_id || order.client_id} · {order.reason_code || order.order_status}</div>)}
        {receipt.cleanup.error && <div>{receipt.cleanup.error}</div>}
      </>} />}
      {observed && <Typography.Text type="secondary">{zh ? '核验时间' : 'Verified at'}：{new Date(observed * 1000).toLocaleString()}</Typography.Text>}
    </div>}
    <details className="content-disclosure"><summary>{zh ? '执行摘要' : 'Execution summary'}</summary><pre>{formatResult(receipt) || (zh ? '未返回内容' : 'No output')}</pre></details>
    {item.audit?.url && <AuditDisclosure url={item.audit.url} zh={zh} />}
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
