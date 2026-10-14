import { Alert, Space, Tag, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { managementLabel, observationTime, sourceLabel } from '../lib/positionManagement';

export function ManagedResource({ resource }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!resource || (!resource.source && resource.managed == null && !resource.supported_actions)) return null;
  const actions = resource.supported_actions || resource.allowed_actions || [];
  return <Space direction="vertical" size={2} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <Space wrap size={4}><Tag>{sourceLabel(resource.source, locale)}</Tag><Tag color={resource.managed ? 'green' : resource.management_state === 'conflict' ? 'red' : 'orange'}>{managementLabel(resource, locale)}</Tag></Space>
    {(resource.owner || resource.owner_config_id) && <Typography.Text type="secondary">{zh ? '管理者' : 'Owner'}: {resource.owner || resource.owner_config_id}</Typography.Text>}
    <Typography.Text type="secondary">{zh ? '可用动作' : 'Available actions'}: {actions.length ? actions.join(' · ') : (zh ? '待核验或无支持动作' : 'Pending verification or unsupported')}</Typography.Text>
    <Typography.Text type="secondary">{zh ? '观察于' : 'Observed'}: {observationTime(resource.observed_at, locale)}</Typography.Text>
    {resource.change_source === 'external' && <Typography.Text type="secondary">{zh ? '外部调整已同步' : 'External change synchronized'}</Typography.Text>}
    {resource.error && <Typography.Text type="warning">{resource.error}</Typography.Text>}
  </Space>;
}

export default function ManagementStatus({ management }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!management || !['visible_first_claim', 'exclusive_scope'].includes(management.policy)) return null;
  return <Space direction="vertical" size="small" style={{ width: '100%' }}>
    <Typography.Text strong>{management.policy === 'visible_first_claim'
      ? (zh ? '自动纳管 · 管理权持续保留' : 'Automatic management · Persistent ownership')
      : (zh ? '独占管理 · 后台持续对账' : 'Exclusive management · Background reconciliation')}</Typography.Text>
    {management.error && <Alert type="warning" showIcon title={zh ? '管理记录暂不可读取，后台将继续核验。' : 'Management records are unavailable. Background verification continues.'} />}
    {Object.entries(management.resource_states || {}).filter(([, state]) => state.state === 'conflict').map(([side, state]) => <Alert key={side} type="warning" showIcon title={`${zh ? '资源由其他管理者占用' : 'Resource is managed elsewhere'}: ${side} · ${state.owner}`} />)}
    {(management.scopes || []).map((scope, index) => <div key={`${scope.symbol}:${scope.side}:${index}`}>
      <Space wrap><Tag color="blue">{scope.symbol} · {scope.side}</Tag><Typography.Text>{zh ? '管理者' : 'Owner'}: {scope.owner || scope.owner_config_id || '—'}</Typography.Text><Tag color={scope.sync_status === 'verified' ? 'green' : 'orange'}>{({ verified: zh ? '已核验' : 'Verified', partial: zh ? '部分核验' : 'Partial', conflict: zh ? '范围冲突' : 'Scope conflict' })[scope.sync_status] || (zh ? '后台核验中' : 'Verifying')}</Tag><Tag>{sourceLabel(scope.source, locale)}</Tag></Space>
      {scope.claimed_at != null && <div><Typography.Text type="secondary">{zh ? '认领于' : 'Claimed'}: {observationTime(scope.claimed_at, locale)}</Typography.Text></div>}
      <div><Typography.Text type="secondary">{zh ? '观察于' : 'Observed'}: {observationTime(scope.observed_at, locale)}</Typography.Text></div>
      {(scope.change_summary || scope.changes)?.length > 0 && <Typography.Paragraph style={{ marginBottom: 4 }}>{typeof (scope.change_summary || scope.changes) === 'string' ? (scope.change_summary || scope.changes) : (scope.changes || []).map((change) => typeof change === 'string' ? change : change.summary || change.kind).join(' · ')}</Typography.Paragraph>}
      {scope.error && <Alert type="warning" showIcon title={scope.error} />}
    </div>)}
    {!management.scopes?.length && <Typography.Text type="secondary">{zh ? '尚无已认领资源；后台发现资源或首次有效管理时自动认领。' : 'No resources have been claimed. Discovery or the first valid management request establishes ownership.'}</Typography.Text>}
    <Typography.Text type="secondary">{zh ? '当前数量以交易所观察为准。人工变化核验后继续管理；缺失证据只影响依赖它的具体动作。' : 'Current quantities follow exchange observations. Verified external changes remain manageable; evidence gaps affect only dependent actions.'}</Typography.Text>
  </Space>;
}
