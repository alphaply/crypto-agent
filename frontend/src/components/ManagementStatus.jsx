import { Alert, Space, Tag, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { managementLabel } from '../lib/positionManagement';

export function ManagedResource({ resource }) {
  if (!resource) return null;
  if (resource.management_state === 'conflict' || resource.error) {
    return (
      <Space direction="vertical" size={2} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
        {resource.management_state === 'conflict' ? <Tag color="red">{managementLabel(resource, 'zh')}</Tag> : null}
        {resource.error && <Typography.Text type="warning">{resource.error}</Typography.Text>}
      </Space>
    );
  }
  return null;
}

export default function ManagementStatus({ management }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!management || !['visible_first_claim', 'exclusive_scope', 'shared'].includes(management.policy)) return null;
  const conflicts = Object.entries(management.resource_states || {}).filter(([, state]) => state.state === 'conflict');
  if (!management.error && conflicts.length === 0) return null;
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {management.error && <Alert type="warning" showIcon title={zh ? '管理记录暂不可读取，后台将继续核验。' : 'Management records are unavailable. Background verification continues.'} />}
      {conflicts.map(([side, state]) => (
        <Alert key={side} type="warning" showIcon title={`${zh ? '资源事实需要核验' : 'Resource evidence needs verification'}: ${side} · ${state.owner}`} />
      ))}
    </Space>
  );
}
