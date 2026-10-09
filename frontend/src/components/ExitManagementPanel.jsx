import { Alert, Empty, Space, Table, Tag, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { formatExitNumber } from '../lib/exitManagement';

export default function ExitManagementPanel({ management, hideEmpty = false }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!management || management.mode !== 'independent_exits') return null;
  const exits = management.exits || [];
  const types = zh
    ? { market: '市价退出', take_profit_limit: '限价止盈', stop_market: '触发市价止损' }
    : { market: 'Market exit', take_profit_limit: 'Limit take profit', stop_market: 'Stop market' };
  const uncovered = ['LONG', 'SHORT'].map((side) => ({ side, amount: management.error ? null : management.uncovered?.[side] }));
  const hasUncovered = uncovered.some(({ amount }) => Number(amount) > 0);
  // Hide only confirmed zero exposure; unknown coverage and pending/error states stay visible.
  if (hideEmpty && !exits.length && !management.error && !management.pending
      && uncovered.every(({ amount }) => amount !== null && amount !== undefined && Number(amount) === 0)) return null;
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {management.error ? <Alert type="error" showIcon title={management.error} /> : null}
      {management.pending ? <Alert type="warning" showIcon title={zh ? '退出单有待核验操作，请等待状态确认。' : 'Exit orders have pending operations. Await confirmation.'} /> : null}
      <Space wrap>
        <Typography.Text type={hasUncovered ? 'warning' : 'secondary'}>
          {zh ? '未被有效止损覆盖的数量' : 'Quantity without active stop coverage'}
        </Typography.Text>
        {uncovered.map(({ side, amount }) => <Tag key={side} color={Number(amount) > 0 ? 'orange' : 'default'}>{side}: {formatExitNumber(amount)}</Tag>)}
      </Space>
      <Table
        size="small"
        rowKey={(row) => row.order_id || row.client_id}
        pagination={false}
        dataSource={exits}
        scroll={{ x: 780 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '暂无独立退出单' : 'No independent exits'} /> }}
        columns={[
          { title: zh ? '订单' : 'Order', dataIndex: 'order_id', width: 170, ellipsis: true },
          { title: zh ? '方向' : 'Side', dataIndex: 'pos_side', width: 90 },
          { title: zh ? '退出方式' : 'Exit type', dataIndex: 'exit_type', width: 145, render: (value) => types[value] || value },
          { title: zh ? '价格 / 触发价' : 'Price / trigger', width: 145, render: (_, row) => row.exit_type === 'market' ? (zh ? '市价' : 'Market') : formatExitNumber(row.exit_type === 'stop_market' ? row.trigger_price : row.price) },
          { title: zh ? '委托数量' : 'Quantity', dataIndex: 'amount', render: formatExitNumber },
          { title: zh ? '剩余数量' : 'Remaining', dataIndex: 'remaining', render: formatExitNumber },
          { title: zh ? '状态' : 'Status', dataIndex: 'status', width: 150, render: (status, row) => <Space direction="vertical" size={2}><Tag color={row.error ? 'red' : 'default'}>{status || (zh ? '未知' : 'Unknown')}</Tag>{row.error ? <Typography.Text type="danger" style={{ overflowWrap: 'anywhere' }}>{row.error}</Typography.Text> : null}</Space> },
        ]}
      />
    </Space>
  );
}
