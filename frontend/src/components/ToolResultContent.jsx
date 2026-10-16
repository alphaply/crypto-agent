import { Tag } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import './ToolResultContent.css';
import { receiptSummary } from '../lib/toolReceipt';

// What a reader needs to judge one operation: did it work, on what, how much,
// and where the order stands. Every other field stays in the raw JSON view.
const visibleKeys = new Set([
  'results', 'result', 'fill',
  'status', 'order_status', 'order_id',
  'symbol', 'side',
  'amount', 'requested_amount', 'filled_amount', 'remaining_amount',
  'price', 'average_price', 'trigger_price',
  'error', 'errors', 'error_code', 'reason', 'message',
]);

const labels = {
  results: '操作结果', result: '结果', fill: '成交',
  status: '状态', order_status: '挂单状态', order_id: '订单编号',
  symbol: '标的', side: '方向',
  amount: '数量', requested_amount: '数量', filled_amount: '已成交', remaining_amount: '剩余',
  price: '价格', average_price: '成交均价', trigger_price: '触发价',
  error: '原因', errors: '原因', error_code: '原因代码', reason: '原因', message: '说明',
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

function Value({ value, field, zh, depth = 0 }) {
  if (typeof value !== 'object') {
    if (['status', 'order_status'].includes(field) && states[value]) {
      const [label, color] = states[value];
      return <Tag color={color}>{zh ? label : value}</Tag>;
    }
    return <span className={field === 'error' || field === 'errors' ? 'tool-result-error' : ''}>{String(value)}</span>;
  }
  if (Array.isArray(value)) return value.length ? <ol className="tool-result-list">{value.map((item, index) =>
    <li key={index}><Value value={item} field={field} zh={zh} depth={depth + 1} /></li>)}</ol> : null;
  const entries = Object.entries(value).filter(([key, item]) => visibleKeys.has(key)
    && item !== null && item !== undefined && !(depth === 0 && key === 'status'));
  if (!entries.length) return null;
  return <dl className="tool-result-fields">{entries.map(([key, item]) => {
    const label = zh ? labels[key] || key : key.replaceAll('_', ' ');
    const nested = item !== null && typeof item === 'object'
      && (Array.isArray(item) ? item.length > 0 : Object.keys(item).some((child) => visibleKeys.has(child)));
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
