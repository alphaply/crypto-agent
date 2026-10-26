import { Tag } from 'antd';
import { ArrowUpOutlined, ArrowDownOutlined, EditOutlined, CloseOutlined, SafetyOutlined } from '@ant-design/icons';
import MarkdownBlock from './MarkdownBlock';
import './ToolResultContent.css';
import { receiptSummary, isTradeReceipt, tradeReceiptRows, tradeReceiptLabels } from '../lib/toolReceipt';

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

function displayNumber(value, zh) {
  if (value == null) return zh ? '未提供' : 'Not provided';
  if (value === 'all') return zh ? '全部' : 'All';
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(zh ? 'zh-CN' : 'en-US', { maximumFractionDigits: 20 }) : String(value);
}

const actionIcons = { open: ArrowUpOutlined, close: ArrowDownOutlined, edit: EditOutlined, cancel: CloseOutlined, protect: SafetyOutlined };

function TradingResult({ content, zh, toolName, fallbackStatus, request }) {
  const rows = tradeReceiptRows(content, { request, toolName, fallbackStatus });
  if (!rows) return <div className="tool-result-content tool-result-card"><MarkdownBlock content={String(content || '').replace(/\s+/g, ' ').slice(0, 180)} /></div>;
  return <div className="tool-result-content tool-trade-results">{rows.map((row, index) => {
    const editing = row.action === 'edit' || row.action?.startsWith('amend_');
    const action = editing ? 'edit' : row.action;
    const Icon = actionIcons[action] || SafetyOutlined;
    const [base, quote] = (row.symbol || '').split(':')[0].split('/');
    const { action: actionLabel, side: sideLabel } = tradeReceiptLabels(row, zh);
    const priceLabel = row.priceFromRequest ? zh ? '委托价格' : 'Requested price'
      : row.action === 'cancel' ? zh ? '原挂单价' : 'Order price'
        : editing ? zh ? '挂单价格' : 'Order price' : zh ? '限价' : 'Limit price';
    const metrics = [];
    if (row.price != null || (row.triggerPrice == null && row.stopLoss == null && row.takeProfit == null)) metrics.push({
      label: row.orderType === 'market' ? zh ? '委托方式' : 'Order type' : priceLabel,
      value: row.orderType === 'market' ? zh ? '市价' : 'Market' : displayNumber(row.price, zh),
      unit: row.orderType !== 'market' && row.price != null ? quote : null,
      primary: true, missing: row.price == null && row.orderType !== 'market',
    });
    if (row.triggerPrice != null) metrics.push({ label: row.triggerFromRequest ? zh ? '委托触发价' : 'Requested trigger' : zh ? '触发价' : 'Trigger price', value: displayNumber(row.triggerPrice, zh), unit: quote, primary: true });
    if (row.requestedPrice != null) metrics.push({ label: zh ? '申请改为' : 'Requested price', value: displayNumber(row.requestedPrice, zh), unit: quote, primary: true });
    if (row.requestedTriggerPrice != null) metrics.push({ label: zh ? '申请触发价' : 'Requested trigger', value: displayNumber(row.requestedTriggerPrice, zh), unit: quote, primary: true });
    for (const [value, label] of [[row.stopLoss, zh ? '止损触发价' : 'Stop trigger'], [row.takeProfit, zh ? '止盈触发价' : 'Take-profit trigger'], [row.stopLimitPrice, zh ? '止损限价' : 'Stop limit'], [row.takeProfitLimitPrice, zh ? '止盈限价' : 'Take-profit limit']]) {
      if (value != null) metrics.push({ label, value: displayNumber(value, zh), unit: quote, primary: true });
    }
    metrics.push({ label: row.amountIsRemaining ? zh ? '目标剩余数量' : 'Target remaining' : zh ? '数量' : 'Amount', value: displayNumber(row.amount, zh), unit: row.amount != null && row.amount !== 'all' ? base : null, missing: row.amount == null });
    const statusLabel = row.result === 'failed' ? zh ? '失败' : 'Failed'
      : row.result === 'success' ? (zh ? '成功' : 'Success') + (row.status === 'submitted' ? zh ? ' · 已提交' : ' · Submitted' : '')
        : zh ? states[row.status]?.[0] || '待确认' : row.status || 'Pending';
    return <article className="tool-result-card tool-trade-row" data-action={action} key={index}>
      <div className="tool-trade-heading">
        <span className="tool-trade-icon" aria-hidden="true"><Icon /></span>
        <div className="tool-trade-title"><span className="tool-trade-action">{actionLabel}</span>
          <strong>{row.symbol || (zh ? '标的未提供' : 'Symbol not provided')}</strong>
          {sideLabel && <span className="tool-trade-side">{sideLabel}</span>}
        </div>
        <span className={`tool-trade-status is-${row.result || 'pending'}`}><i aria-hidden="true" />{statusLabel}</span>
      </div>
      <dl className="tool-trade-metrics">{metrics.map(({ label, value, unit, primary, missing }) =>
        <div className={`${primary ? 'is-price' : ''} ${missing ? 'is-missing' : ''}`} key={label}>
          <dt>{label}</dt><dd>{value}{unit && <span className="tool-trade-unit">{unit}</span>}</dd>
        </div>)}</dl>
    </article>;
  })}</div>;
}

export default function ToolResultContent({ content, zh, toolName, fallbackStatus, request }) {
  if (isTradeReceipt(content, toolName)) return <TradingResult {...{ content, zh, toolName, fallbackStatus, request }} />;
  return <GenericResult {...{ content, zh, toolName, fallbackStatus }} />;
}

function GenericResult({ content, zh, toolName, fallbackStatus }) {
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
