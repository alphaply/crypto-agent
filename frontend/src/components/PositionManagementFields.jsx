import { Button, Space, Tag, Typography } from 'antd';

export default function PositionManagementFields({ value, onChange, savedPolicy, locale = 'zh' }) {
  const zh = locale === 'zh';
  const automatic = !value.position_management_policy || value.position_management_policy === 'visible_first_claim';
  return <Space direction="vertical" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text strong>{zh ? '仓位与挂单管理' : 'Position and order management'}</Typography.Text>
    <Tag color="blue">{zh ? '授权范围共享管理' : 'Shared management within authorized scope'}</Tag>
    {savedPolicy && savedPolicy !== 'visible_first_claim' && <Typography.Text type="secondary">{zh ? `原配置策略：${savedPolicy}。保存此草稿后启用自动管理；旧版本请求仍按原兼容契约解释。` : `Saved policy: ${savedPolicy}. Saving this draft enables automatic management; old requests retain their versioned contract.`}</Typography.Text>}
    <Typography.Text type="secondary">{zh ? '已授权连接和 Agent 可共管同一账户的仓位和挂单，不需要先释放其他管理者。人工及其他连接的变更会持续核验。' : 'Authorized connections and agents share account resources without releasing another manager. Current execution facts are reconciled continuously.'}</Typography.Text>
    <Typography.Text type="secondary">{zh ? '后续成功提交的明确修改生效，旧后台不会恢复被取代的参数。提交前仍核验权限、最新数量与共享预留，查询不会触发交易。' : 'Later successful explicit changes apply. Old maintenance cannot restore superseded settings. Permissions, current quantities and shared reservations are checked before writes; queries do not trade.'}</Typography.Text>
    {!automatic && <>
      <Typography.Text type="secondary">{zh ? '此配置保留旧管理策略，可在下次保存时启用自动管理。' : 'This configuration retains a legacy policy. Enable automatic management when saving it.'}</Typography.Text>
      <Button onClick={() => onChange({ position_management_policy: 'visible_first_claim', management_scopes: [] })}>{zh ? '启用自动管理' : 'Enable automatic management'}</Button>
    </>}
  </Space>;
}
