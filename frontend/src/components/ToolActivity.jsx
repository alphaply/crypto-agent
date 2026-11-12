import { ClockCircleOutlined, InfoCircleOutlined, LoadingOutlined, ToolOutlined } from '@ant-design/icons';
import { toolDisplayName } from '../lib/toolActivity';
import './ToolActivity.css';

const statuses = {
  preparing: ['生成参数中', 'Preparing arguments'], planned: ['等待执行', 'Queued'],
  running: ['执行中', 'Running'], awaiting_approval: ['等待审批', 'Awaiting approval'],
  unknown: ['未收到结果', 'No result received'], failed: ['执行失败', 'Failed'],
  not_executed: ['未执行', 'Not executed'],
};

export function ToolActivity({ name, args, status = 'planned', zh }) {
  const busy = status === 'running' || status === 'preparing';
  const Icon = busy ? LoadingOutlined : status === 'unknown' ? InfoCircleOutlined : ClockCircleOutlined;
  return <div className={`tool-activity is-${status}`}>
    <div className="tool-activity-header" role="status" aria-live="polite">
      <span className="tool-activity-icon"><ToolOutlined /></span>
      <strong>{toolDisplayName(name, zh)}</strong>
      <span className="tool-activity-state"><Icon spin={busy} />{statuses[status]?.[zh ? 0 : 1] || status}</span>
    </div>
    {args?.reason && <p className="tool-activity-reason">{args.reason}</p>}
    <p className="tool-activity-note">{zh
      ? busy ? '收到工具回执后，这里会显示实际结果。' : status === 'unknown' ? '尚无工具回执，无法确认执行结果。' : '执行情况以工具回执为准。'
      : busy ? 'The actual result will appear when the tool responds.' : status === 'unknown' ? 'No receipt is available to confirm the outcome.' : 'Check the tool receipt for the actual outcome.'}</p>
    <details className="tool-activity-details"><summary>{zh ? '查看调用参数' : 'View arguments'}</summary>
      <code>{name}</code><pre>{args == null ? (zh ? '当前进度事件未包含完整参数。' : 'Full arguments are not included in this progress event.') : JSON.stringify(args, null, 2)}</pre>
    </details>
  </div>;
}

export function ModelToolDiagnostic({ calls = [], diagnostics, zh, streaming }) {
  if (streaming || calls.length) return null;
  const evidence = diagnostics && typeof diagnostics === 'object' ? diagnostics : {};
  if (evidence.tools_sent === 0 && evidence.tool_evidence_state === 'no_tools_sent') return null;
  return <details className="model-tool-diagnostic">
    <summary><InfoCircleOutlined /> {zh ? '本轮未调用工具' : 'No tool calls in this turn'}</summary>
    <p>{zh ? '正文中的操作说明或 JSON 不代表已经执行。实际执行情况请查看工具回执；正常观望也可以不调用工具。' : 'Text or JSON in the answer does not confirm execution. Check tool receipts for actual outcomes. A decision to wait may need no tools.'}</p>
    {Number.isInteger(evidence.tools_sent) && <dl>
      <div><dt>{zh ? '请求携带工具' : 'Tools sent'}</dt><dd>{evidence.tools_sent}</dd></div>
      <div><dt>{zh ? '接口返回调用' : 'Calls returned'}</dt><dd>{evidence.tool_calls_returned ?? '—'}</dd></div>
      <div><dt>{zh ? '调用方式' : 'Tool choice'}</dt><dd>{evidence.tool_choice || '—'}</dd></div>
      {evidence.finish_reason && <div><dt>{zh ? '结束原因' : 'Finish reason'}</dt><dd>{evidence.finish_reason}</dd></div>}
    </dl>}
    {!Number.isInteger(evidence.tools_sent) && <p>{zh ? '这条历史记录没有保存请求诊断，无法据此判断接口是否支持工具。' : 'Request diagnostics were not saved with this record; endpoint tool support cannot be determined.'}</p>}
  </details>;
}
