import { Alert, Empty, Space, Table, Tag, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { formatExitNumber, exitOrderLabel, exitQuantityLabel } from '../lib/exitManagement';

export default function ExitManagementPanel({ management, hideEmpty = false }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!management || management.mode !== 'independent_exits') return null;
  const exits = management.exits || [];
  const uncovered = ['LONG', 'SHORT'].map((side) => ({ side, amount: management.error ? null : management.uncovered?.[side] }));
  const hasUncovered = uncovered.some(({ amount }) => Number(amount) > 0);
  // Hide only confirmed zero exposure; unknown coverage and pending/error states stay visible.
  if (hideEmpty && !exits.length && !management.error && !management.pending
      && uncovered.every(({ amount }) => amount !== null && amount !== undefined && Number(amount) === 0)) return null;
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {management.error ? <Alert type="error" showIcon title={management.error} /> : null}
      {management.pending ? <Alert type="warning" showIcon title={zh ? '退出单有待核验操作，请等待状态确认。' : 'Exit orders have pending operations. Await confirmation.'} /> : null}
      {!!management.unresolved_orders?.length && <Alert type="warning" showIcon title={zh ? '具体待核验订单（不计入有效保护）' : 'Unverified orders (excluded from coverage)'} description={management.unresolved_orders.map((order) => <div key={order.order_id || order.client_id} style={{ overflowWrap: 'anywhere' }}>
        {order.order_id || order.client_id} · {order.kind} · {order.reason_code}
        {order.operation_id && <div>{zh ? '原操作' : 'Original operation'}：{order.operation_id}</div>}
      </div>)} />}
      {management.quantity_conflict ? <Alert type="warning" showIcon title={zh ? '固定退出档合计超过当前仓位，数量保持不变，可修改或撤销。' : 'Fixed exits exceed the current position. Quantities remain unchanged; edit or cancel as needed.'} /> : null}
      <Space wrap>
        <Typography.Text type={hasUncovered ? 'warning' : 'secondary'}>
          {zh ? '未被有效止损覆盖的数量' : 'Quantity without active stop coverage'}
        </Typography.Text>
        {uncovered.map(({ side, amount }) => <Tag key={side} color={Number(amount) > 0 ? 'orange' : 'default'}>{side}: {formatExitNumber(amount)}</Tag>)}
      </Space>
      <Typography.Text type="secondary">{zh ? '覆盖量为当前挂单估计，不代表成交保证；条件限价单触发后仍需成交。“全部”条件单按触发时该账户该方向仓位执行。' : 'Coverage is an estimate, not a fill guarantee. Triggered limit orders still need a fill. Conditional All exits use the account position on that side at trigger time.'}</Typography.Text>
      <Table
        size="small"
        rowKey={(row) => row.order_id || row.client_id}
        pagination={false}
        dataSource={exits}
        scroll={{ x: 1180 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '暂无独立退出单' : 'No independent exits'} /> }}
        columns={[
          { title: zh ? '订单' : 'Order', dataIndex: 'order_id', width: 170, ellipsis: true },
          { title: zh ? '方向' : 'Side', dataIndex: 'pos_side', width: 90 },
          { title: zh ? '退出方式' : 'Exit type', width: 145, render: (_, row) => exitOrderLabel(row, locale) },
          { title: zh ? '委托价' : 'Limit price', dataIndex: 'price', width: 110, render: formatExitNumber },
          { title: zh ? '触发价' : 'Trigger price', dataIndex: 'trigger_price', width: 110, render: formatExitNumber },
          { title: zh ? '委托数量' : 'Quantity', render: (_, row) => exitQuantityLabel(row, 'amount', locale) },
          { title: zh ? '剩余数量' : 'Remaining', render: (_, row) => exitQuantityLabel(row, 'remaining', locale) },
          { title: zh ? '实际成交' : 'Filled', render: (_, row) => formatExitNumber(row.filled_amount ?? row.filled) },
          { title: zh ? '覆盖估计' : 'Coverage estimate', dataIndex: 'coverage_estimate', render: formatExitNumber },
          { title: zh ? '订单状态' : 'Order status', dataIndex: 'status', width: 150, render: (status, row) => <Space direction="vertical" size={2}><Tag color={row.error ? 'red' : 'default'}>{row.order_status || status || (zh ? '未知' : 'Unknown')}</Tag>{row.error ? <Typography.Text type="danger" style={{ overflowWrap: 'anywhere' }}>{row.error}</Typography.Text> : null}</Space> },
        ]}
      />
    </Space>
  );
}
