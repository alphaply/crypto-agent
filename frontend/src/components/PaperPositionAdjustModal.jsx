import React, { useRef, useState } from 'react';
import { Alert, Button, InputNumber, Modal, Segmented } from 'antd';
import { api } from '../lib/api';
import { formatPaperAmount } from '../lib/paperDisplay';
import { newPaperPositionOperationId, paperPositionIntent, paperPositionPayload } from '../lib/paperPositionAdjustment';

export default function PaperPositionAdjustModal({ position, maxLeverage, pendingIntents, zh, onClose, onCompleted }) {
  const label = (cn, en) => zh ? cn : en;
  const format = (value) => formatPaperAmount(value, zh);
  const [action, setAction] = useState('set_leverage');
  const [leverage, setLeverage] = useState(position.leverage);
  const [amount, setAmount] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [refreshError, setRefreshError] = useState(false);
  const sending = useRef(false);
  const parsed = paperPositionPayload(action, leverage, amount, maxLeverage);
  const descriptions = {
    set_leverage: label('持仓数量和开仓价不变。基础保证金按开仓名义金额 ÷ 新杠杆重新计算，多余部分退回现金，不足部分补入；已追加的保证金保留。', 'Quantity and entry price stay unchanged. Base margin becomes entry notional ÷ new leverage: excess returns to cash, or the difference is funded from cash. Added margin is retained.'),
    add_margin: label('从账户可用现金转入这笔逐仓持仓，增加保证金；持仓数量、开仓价和杠杆不变。', 'Move available cash into this isolated position as additional margin. Quantity, entry price and leverage stay unchanged.'),
    reduce_margin: label('仅可取回手动追加的保证金，退回账户可用现金。剩余保证金不能低于基础保证金，也不能让仓位立即达到强平条件；以服务端检查为准。', 'Return manually added margin to available cash. Remaining margin must cover the base requirement and keep the position above liquidation. The server validates the request.'),
  };
  const submit = async () => {
    if (sending.current || parsed.error || receipt) return;
    sending.current = true;
    setSubmitting(true);
    setError(null);
    const intent = paperPositionIntent(position, parsed.payload);
    let operationId = pendingIntents.current.get(intent);
    if (!operationId) {
      operationId = newPaperPositionOperationId();
      pendingIntents.current.set(intent, operationId);
    }
    let completed = false;
    try {
      const url = '/mcp/paper/accounts/' + encodeURIComponent(position.namespace) + '/' + encodeURIComponent(position.user_id) + '/positions/' + encodeURIComponent(position.resource_id);
      const { data } = await api.post(url, { ...parsed.payload, operation_id: operationId });
      if (data.success !== true || data.status !== 'completed') {
        setError({ uncertain: true, detail: data.error || data.message || label('服务端尚未确认完成。', 'The server has not confirmed completion.') });
        return;
      }
      setReceipt(data);
      pendingIntents.current.delete(intent);
      completed = true;
    } catch (err) {
      setError({ uncertain: !err.response || err.response.status >= 500, detail: err.message });
    } finally {
      sending.current = false;
      setSubmitting(false);
    }
    if (completed) {
      try { await onCompleted(); }
      catch { setRefreshError(true); }
    }
  };
  const cashChange = receipt?.cash_change;
  return <Modal open width={620} title={label('调整当前仓位', 'Adjust current position')} onCancel={() => { if (!submitting) onClose(); }} closable={!submitting} maskClosable={!submitting}
    footer={receipt ? <Button type="primary" onClick={onClose}>{label('完成', 'Done')}</Button> : <><Button disabled={submitting} onClick={onClose}>{label('取消', 'Cancel')}</Button><Button type="primary" loading={submitting} disabled={Boolean(parsed.error)} onClick={submit}>{error ? label('重试此操作', 'Retry this action') : label('提交调整', 'Submit adjustment')}</Button></>}>
    <div className="paper-position-modal">
      <div className="paper-position-identity"><strong>{position.symbol} · {position.side === 'LONG' ? label('多', 'Long') : label('空', 'Short')}</strong><span>{position.account_name || position.user_id} · {position.user_id}</span></div>
      <div className="paper-detail-facts paper-position-facts">
        <div><span>{receipt ? label('调整前杠杆', 'Leverage before') : label('当前杠杆', 'Current leverage')}</span><strong className="paper-number">{format(position.leverage)}×</strong></div>
        <div><span>{receipt ? label('调整前保证金', 'Margin before') : label('当前保证金', 'Current margin')}</span><strong className="paper-number">{format(position.margin)} USDT</strong></div>
        <div><span>{receipt ? label('调整前可用现金', 'Cash before') : label('账户可用现金', 'Available cash')}</span><strong className="paper-number">{format(position.account_available_cash)} USDT</strong></div>
      </div>
      {!receipt && <>
        <Segmented block disabled={submitting} value={action} options={[{ value: 'set_leverage', label: label('修改杠杆', 'Leverage') }, { value: 'add_margin', label: label('追加保证金', 'Add margin') }, { value: 'reduce_margin', label: label('减少保证金', 'Reduce margin') }]} onChange={(value) => { setAction(value); setError(null); }} />
        <div className="form-field paper-position-input">
          <label htmlFor="paper-position-value">{action === 'set_leverage' ? label('新的仓位杠杆', 'New position leverage') : action === 'add_margin' ? label('追加金额 · USDT', 'Margin to add · USDT') : label('减少金额 · USDT', 'Margin to reduce · USDT')}</label>
          {action === 'set_leverage'
            ? <InputNumber id="paper-position-value" min={1} max={maxLeverage} step={1} changeOnBlur={false} disabled={submitting} value={leverage} onChange={(value) => { setLeverage(value); setError(null); }} suffix="×" />
            : <InputNumber id="paper-position-value" stringMode min="0.00000001" step="0.00000001" changeOnBlur={false} disabled={submitting} value={amount} onChange={(value) => { setAmount(value); setError(null); }} placeholder={label('输入正数，最多 8 位小数', 'Positive amount, up to 8 decimals')} />}
          {parsed.error && <span className="paper-cell-note">{parsed.error === 'leverage' ? label('请输入 1 至 ' + format(maxLeverage) + ' 之间的整数杠杆。', 'Enter an integer leverage between 1 and ' + format(maxLeverage) + '.') : label('金额须大于 0，最多 8 位小数。', 'Enter an amount above zero with at most 8 decimals.')}</span>}
        </div>
        <p className="paper-position-description">{descriptions[action]}</p>
        <p className="paper-cell-note">{label('如同标的已有入场挂单影响资金一致性，服务端会拒绝并说明原因；此操作不会自动撤单或改单。', 'If entry orders for this symbol conflict with the adjustment, the server will reject it and explain why. This action does not cancel or edit those orders.')}</p>
      </>}
      {error && <Alert type={error.uncertain ? 'warning' : 'error'} showIcon title={error.uncertain ? label('暂未确认操作结果', 'Result not yet confirmed') : label('调整未完成', 'Adjustment not completed')} description={<><p>{error.detail}</p>{error.uncertain && <span>{label('原请求可能已经处理；重试相同参数会核对同一笔操作，不会另发一笔。', 'The original request may have been processed. Retrying unchanged parameters checks the same operation instead of creating another.')}</span>}</>} />}
      {receipt && <>
        <Alert type="success" showIcon title={label('仓位调整已完成', 'Position adjustment completed')} />
        <div className="paper-position-result">
          <div><span>{label('杠杆', 'Leverage')}</span><strong>{format(receipt.leverage_before)}× → {format(receipt.leverage_after)}×</strong></div>
          <div><span>{label('保证金', 'Margin')}</span><strong>{format(receipt.margin_before)} → {format(receipt.margin_after)} USDT</strong></div>
          <div><span>{label('现金变化', 'Cash change')}</span><strong className={cashChange > 0 ? 'paper-positive' : cashChange < 0 ? 'paper-negative' : ''}>{cashChange > 0 ? '+' : ''}{format(cashChange)} USDT</strong></div>
          <div><span>{label('调整后可用现金', 'Available cash after')}</span><strong>{format(receipt.available_cash)} USDT</strong></div>
        </div>
        <p className="paper-cell-note">{label('以上为本次操作回执。现金增加表示保证金退回账户，现金减少表示资金转入持仓。', 'These values are from this operation’s receipt. Positive cash change returns margin to the account; negative cash change funds the position.')}</p>
        {refreshError && <Alert type="warning" showIcon title={label('调整已完成，列表刷新失败；请稍后刷新查看。', 'The adjustment completed, but the list could not refresh. Refresh again later.')} />}
      </>}
    </div>
  </Modal>;
}
