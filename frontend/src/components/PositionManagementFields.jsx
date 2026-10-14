import { Button, Space, Tag, Typography } from 'antd';

export default function PositionManagementFields({ value, onChange, savedPolicy, locale = 'zh' }) {
  const zh = locale === 'zh';
  const automatic = !value.position_management_policy || value.position_management_policy === 'visible_first_claim';
  return <Space direction="vertical" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text strong>{zh ? '仓位与挂单管理' : 'Position and order management'}</Typography.Text>
    <Tag color="blue">{zh ? '可见范围自动管理 · 先到先得' : 'Automatic visible management · First claim wins'}</Tag>
    {savedPolicy && savedPolicy !== 'visible_first_claim' && <Typography.Text type="secondary">{zh ? `原配置策略：${savedPolicy}。保存此草稿后启用自动管理；旧版本请求仍按原兼容契约解释。` : `Saved policy: ${savedPolicy}. Saving this draft enables automatic management; old requests retain their versioned contract.`}</Typography.Text>}
    <Typography.Text type="secondary">{zh ? '后台发现已有仓位、挂单，或首次有效交易请求时，自动取得当前账户可见范围内未被占用资源的管理权。人工加减仓、改单和撤单会持续对账。' : 'Background discovery or the first valid trading request claims available positions and orders in this account’s visible markets. External changes are reconciled continuously.'}</Typography.Text>
    <Typography.Text type="secondary">{zh ? '同一资源由一个管理者负责。平仓、暂停和重启后，管理权仍随任务保留；需要离开时，在管理状态中释放或交接。查询不会取得管理权。' : 'Each resource has one manager. Ownership remains with the task after closing, pausing or restarting; release or transfer it from management status. Queries do not claim resources.'}</Typography.Text>
    {!automatic && <>
      <Typography.Text type="secondary">{zh ? '此配置保留旧管理策略，可在下次保存时启用自动管理。' : 'This configuration retains a legacy policy. Enable automatic management when saving it.'}</Typography.Text>
      <Button onClick={() => onChange({ position_management_policy: 'visible_first_claim', management_scopes: [] })}>{zh ? '启用自动管理' : 'Enable automatic management'}</Button>
    </>}
  </Space>;
}
