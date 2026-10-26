import { useRef, useState } from 'react';
import { Alert, Button, Form, InputNumber, Modal, Space, Typography, message } from 'antd';
import { EditOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';

function editableNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export default function SpotCostBasisEditor({ configId, symbol, stats }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const label = (chinese, english) => (zh ? chinese : english);
  const [form] = Form.useForm();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const savingRef = useRef(false);
  const quantity = Form.useWatch('quantity', form);
  const avgCost = Form.useWatch('avg_cost', form);
  const quote = symbol?.split('/')[1] || '';
  const base = symbol?.split('/')[0] || '';
  const totalCost = quantity !== null && quantity !== undefined && avgCost !== null && avgCost !== undefined
    ? quantity * avgCost : null;

  const showEditor = () => {
    setError('');
    form.setFieldsValue({
      quantity: editableNumber(stats?.cost_basis_current_quantity ?? stats?.total_qty),
      avg_cost: editableNumber(stats?.cost_basis_current_avg_cost ?? stats?.avg_cost),
    });
    setOpen(true);
  };

  const save = async () => {
    if (savingRef.current) return;
    let values;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError('');
    try {
      const response = await api.patch(`/config/${encodeURIComponent(configId)}/spot-cost-basis`, {
        symbol,
        quantity: values.quantity,
        avg_cost: values.avg_cost,
      });
      if (!response.data.success) {
        throw new Error(response.data.error || label('持仓成本未能保存，请重试。', 'Holdings could not be saved. Please retry.'));
      }
      setOpen(false);
      message.success(label('持仓成本已更新，后续买入会自动累加。', 'Holdings updated. Future buys will be added automatically.'));
      window.dispatchEvent(new Event('crypto-agent-dashboard-refresh'));
    } catch (err) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : (err.message || label('保存失败，请重试。', 'Save failed. Please retry.')));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const finiteNonnegative = (_, value) => (
    typeof value === 'number' && Number.isFinite(value) && value >= 0
      ? Promise.resolve()
      : Promise.reject(new Error(label('请填写大于或等于 0 的有效数字。', 'Enter a valid number of 0 or more.')))
  );

  return <>
    <Button size="small" icon={<EditOutlined aria-hidden />} onClick={showEditor} disabled={!configId || !symbol}>
      {label('修改持仓成本', 'Adjust holdings')}
    </Button>
    <Modal
      open={open}
      title={`${label('修改持仓成本', 'Adjust holdings')} · ${symbol}`}
      onCancel={() => { if (!savingRef.current) setOpen(false); }}
      onOk={save}
      confirmLoading={saving}
      cancelButtonProps={{ disabled: saving }}
      closable={!saving}
      maskClosable={!saving}
      keyboard={!saving}
      okText={label('保存新起点', 'Save starting point')}
      cancelText={label('取消', 'Cancel')}
      forceRender
    >
      <Space direction="vertical" size="middle" style={{ width: '100%', marginTop: 16 }}>
        <Alert type="info" showIcon description={label(
          '请填写保存时的全部持有数量和平均成本价（包含之前已买入的部分）。保存后以此为新起点，旧买入不会重复计算，后续新增买入会自动累加。',
          'Enter your total holdings and average cost at the time of saving, including previous buys. This becomes the new starting point: previous buys are not counted again, and future buys are added automatically.',
        )} />
        {error ? <Alert type="error" showIcon description={error} /> : null}
        <Form form={form} layout="vertical" name={`spot-cost-${configId}-${symbol}`} disabled={saving} style={{ width: '100%' }}>
          <Form.Item name="quantity" label={`${label('当前总持有数量', 'Current total quantity')} (${base})`} rules={[{ validator: finiteNonnegative }]}>
            <InputNumber min={0} style={{ width: '100%' }} placeholder={label('填写当前全部数量', 'Enter your full current quantity')} />
          </Form.Item>
          <Form.Item
            name="avg_cost"
            label={`${label('平均成本价', 'Average cost price')} (${quote})`}
            dependencies={['quantity']}
            rules={[
              { validator: finiteNonnegative },
              ({ getFieldValue }) => ({ validator: (_, value) => (
                getFieldValue('quantity') > 0 && value === 0
                  ? Promise.reject(new Error(label('持有数量大于 0 时，请填写大于 0 的平均成本价。', 'Average cost must be greater than 0 when you hold a positive quantity.')))
                  : Promise.resolve()
              ) }),
            ]}
          >
            <InputNumber min={0} style={{ width: '100%' }} placeholder={label('填写每枚币的平均买入价格', 'Enter the average purchase price per coin')} />
          </Form.Item>
          <Typography.Text>
            {label('新起点总成本', 'Starting total cost')}: {totalCost !== null && Number.isFinite(totalCost) ? totalCost.toLocaleString(zh ? 'zh-CN' : 'en-US', { maximumFractionDigits: 12 }) : '—'} {quote}
          </Typography.Text>
        </Form>
        <Typography.Text type="secondary">{label('只修改本任务的持仓统计，不会在交易所买卖或转币。', 'This updates this task’s holdings statistics. It does not place trades or transfer funds.')}</Typography.Text>
      </Space>
    </Modal>
  </>;
}
