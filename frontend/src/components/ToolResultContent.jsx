import { Tag } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import './ToolResultContent.css';
import { receiptSummary } from '../lib/toolReceipt';

// How a receipt was produced and stored, not what happened. These stay in the
// raw JSON view so the card reads as execution facts instead of an API dump.
const internalKeys = new Set([
  'contract_version', 'receipt_version', 'operation_id', 'parent_operation_id',
  'audit_reference', 'details_omitted', 'execution_policy', 'receipt_updated_at',
  'projected_at', 'evidence_source', 'current_evidence_status',
]);

// Boolean envelope flags whose negative value tells the reader nothing.
const silentWhenFalse = new Set(['partial_execution', 'rejected_by_user']);

const labels = {
  results: '操作结果', result: '执行详情', summary: '结果统计', status: '执行状态',
  query: '核验情况', errors: '异常详情', error: '异常原因', error_code: '异常代码',
  action: '操作', symbol: '交易对', side: '方向', order_id: '订单编号', order_status: '订单状态',
  requested_amount: '请求数量', amount: '数量', filled_amount: '已确认成交量', remaining_amount: '剩余数量',
  price: '价格', trigger_price: '触发价', stop_loss: '止损价', take_profit: '止盈价',
  account_summary: '账户摘要', balances: '可用余额', positions: '持仓',
  balance: '余额', reason: '原因', message: '说明', cleanup: '清理情况', recovery: '后台恢复',
  reason_code: '原因代码', verified: '已核验', missing_value: '缺失字段',
  equity: '权益', cash: '现金', free: '可用', used: '占用', observed_at: '观察时间', generated_at: '生成时间',
  continuation: '后续操作提示', current_scope: '当前范围', current_position: '当前仓位',
  admission: '执行前核验', verification: '核验信息', execution: '执行过程',
  index: '操作序号', partial_execution: '部分执行', rejected_by_user: '用户拒绝',
  submitted: '已提交', completed: '已完成', failed: '失败',
  unknown: '待核验', pending: '待确认', not_executed: '未执行', skipped: '已跳过',
};
const states = {
  returned: ['已返回', 'default'],
  completed: ['已完成', 'success'], submitted: ['已提交', 'processing'], running: ['执行中', 'processing'],
  failed: ['失败', 'error'], rejected: ['已拒绝', 'error'], unknown: ['待核验', 'warning'],
  pending: ['待确认', 'warning'], partial: ['部分核验', 'warning'], verified: ['已核验', 'success'],
  filled: ['已成交', 'success'], open: ['挂单中', 'processing'], waiting_trigger: ['等待触发', 'processing'],
  canceled: ['已撤销', 'default'], cancelled: ['已撤销', 'default'], not_executed: ['未执行', 'default'],
  skipped: ['已跳过', 'default'], not_requested: ['未请求', 'default'],
};
const actions = { open: '开仓', close: '平仓', edit: '改单', cancel: '撤单', protect: '设置保护' };

function Value({ value, field, zh, depth = 0 }) {
  if (value === null || value === undefined) return <span className="tool-result-muted">{zh ? '未提供' : 'Not provided'}</span>;
  if (typeof value !== 'object') {
    if (['status', 'order_status'].includes(field) && states[value]) {
      const [label, color] = states[value];
      return <Tag color={color}>{zh ? label : value}</Tag>;
    }
    if (typeof value === 'boolean') return <span>{zh ? (value ? '是' : '否') : String(value)}</span>;
    return <span className={field === 'error' || field === 'errors' ? 'tool-result-error' : ''}>{zh && field === 'action' ? actions[value] || value : String(value)}</span>;
  }
  if (Array.isArray(value)) return value.length ? <ol className="tool-result-list">{value.map((item, index) => <li key={index}>
    <Value value={item} field={field} zh={zh} depth={depth + 1} />
  </li>)}</ol> : <span className="tool-result-muted">{zh ? '空' : 'Empty'}</span>;
  const entries = Object.entries(value).filter(([key, item]) => !internalKeys.has(key)
    && !(depth === 0 && key === 'status') && !(item === false && silentWhenFalse.has(key)));
  if (!entries.length) return <span className="tool-result-muted">{zh ? '空' : 'Empty'}</span>;
  return <dl className="tool-result-fields">{entries.map(([key, item]) => {
    const label = zh ? (key === 'status' && field === 'query' ? '核验状态' : labels[key] || key) : key.replaceAll('_', ' ');
    const nested = item !== null && typeof item === 'object'
      && (Array.isArray(item) ? item.length > 0 : Object.keys(item).some((child) => !internalKeys.has(child)));
    return nested ? <div className="tool-result-section" key={key}>
      <details>
        <summary>{label}{Array.isArray(item) ? ` (${item.length})` : ''}</summary>
        <Value value={item} field={key} zh={zh} depth={depth + 1} />
      </details>
    </div> : <div className="tool-result-field" key={key}><dt>{label}</dt><dd><Value value={item} field={key} zh={zh} depth={depth + 1} /></dd></div>;
  })}</dl>;
}

export default function ToolResultContent({ content, zh, toolName, fallbackStatus }) {
  const { value, status: resultStatus, text } = receiptSummary(content, zh);
  const status = resultStatus || fallbackStatus || 'returned';
  const state = states[status] || [status, 'default'];
  return <div className="tool-result-content tool-result-card">
    <div className="tool-result-card-header">
      <strong>{toolName || (zh ? '工具结果' : 'Tool result')}</strong>
      {status && <Tag color={state[1]}>{zh ? state[0] : status}</Tag>}
    </div>
    <p className="tool-result-card-summary">{text}</p>
    <details className="tool-result-disclosure">
      <summary>{zh ? '展开完整结果' : 'Show full result'}</summary>
      {typeof value === 'string' ? <MarkdownBlock content={value} /> : <Value value={value} zh={zh} />}
    {value !== null && typeof value === 'object' && <details className="tool-result-raw">
      <summary>{zh ? '查看原始 JSON' : 'View raw JSON'}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>}
    </details>
  </div>;
}
