import { Alert, Space, Tag, Typography } from 'antd';
import { agentRuntimeStatus, queuedRunCount } from '../lib/agentRuntime';

export function AgentStatus({ agent, zh }) {
  const status = agentRuntimeStatus(agent);
  const labels = {
    RUNNING: zh ? '执行中' : 'Running',
    QUEUED: zh ? '排队中' : 'Queued',
    FAILED: zh ? '最近失败' : 'Last run failed',
    INTERRUPTED: zh ? '运行中断' : 'Interrupted',
    PAUSED: zh ? '已暂停' : 'Paused',
    SCHEDULED: zh ? '待调度' : 'Scheduled',
  };
  const color = ['RUNNING', 'QUEUED'].includes(status) ? 'processing'
    : ['FAILED', 'INTERRUPTED'].includes(status) ? 'error' : status === 'PAUSED' ? 'default' : 'success';
  const queued = queuedRunCount(agent);
  return <Space size={4} wrap>
    <Tag color={color}>{labels[status]}</Tag>
    {queued > 0 && <Tag>{zh ? `${queued} 轮待运行` : `${queued} queued`}</Tag>}
  </Space>;
}

export function AgentQueuePanel({ agent, locale }) {
  const count = queuedRunCount(agent);
  if (!count) return null;
  const zh = locale === 'zh';
  const next = agent.runtime?.next_queued;
  const source = { scheduled: zh ? '定时' : 'Schedule', schedule: zh ? '定时' : 'Schedule',
    news: zh ? '新闻' : 'News', dca: zh ? '定投' : 'DCA' }[next?.trigger_type];
  return <Alert className="agent-queue-panel" type="info" showIcon
    title={zh ? `${count} 轮任务排队中` : `${count} runs queued`}
    description={<Space direction="vertical" size={2}>
      <span>{zh ? '有可用名额后按顺序运行，执行时读取最新行情和账户。'
        : 'Runs start in eligible order when capacity is available, using current market and account data.'}</span>
      {next?.scheduled_at && <Typography.Text type="secondary">
        {zh ? '下一等待项原计划：' : 'Next queued run was scheduled for: '}{next.scheduled_at}{source ? ` · ${source}` : ''}
      </Typography.Text>}
    </Space>} />;
}
