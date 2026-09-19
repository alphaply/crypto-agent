import { Collapse, Tag, Typography } from 'antd';
import { ClockCircleOutlined } from '@ant-design/icons';

const { Text } = Typography;

export default function ScheduleDetails({ agents, activeTab, locale }) {
  const zh = locale === 'zh';
  const visible = activeTab === 'compare' ? agents : agents.filter((agent) => agent.config_id === activeTab);
  const days = zh ? ['一', '二', '三', '四', '五', '六', '日'] : ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  return (
    <section className="dashboard-schedules" aria-label={zh ? '运行计划' : 'Run schedule'}>
      {visible.map((agent) => {
        const schedule = agent.schedule;
        const paused = schedule?.state === 'paused';
        const status = String(agent.execution?.status || '').toUpperCase();
        const active = ['RUNNING', 'QUEUED'].includes(status);
        return (
          <div className="dashboard-schedule" key={agent.config_id}>
            <div className="dashboard-schedule-summary">
              <div className="dashboard-schedule-label"><ClockCircleOutlined /><Text strong>{zh ? '运行计划' : 'Run schedule'}</Text><Text type="secondary">{agent.title || agent.config_id}</Text></div>
              <div className="dashboard-schedule-next"><span>{zh ? '下次计划' : 'Next scheduled'}</span><strong>{paused ? (zh ? '已暂停' : 'Paused') : agent.next_run || '—'}</strong><span>{schedule?.timezone}</span></div>
              <Tag color={active ? 'processing' : status === 'FAILED' ? 'error' : 'default'}>{paused && !active ? (zh ? '调度已暂停' : 'Scheduler paused') : active ? (zh ? '执行中' : 'In progress') : status === 'FAILED' ? (zh ? '最近执行失败' : 'Last run failed') : (zh ? '等待下次调度' : 'Awaiting dispatch')}</Tag>
            </div>
            <Collapse ghost size="small" items={[{
              key: 'rules',
              label: `${zh ? '当前规则' : 'Current rule'}: ${schedule?.rule_name || '—'} · ${agent.freq || '—'}`,
              children: <div className="dashboard-schedule-rules">
                <Text type="secondary">{zh ? '计划时间按调度器规则计算；任务排队与执行耗时可能推迟实际开始时间。重叠时段使用第一条匹配规则。' : 'Dispatch follows scheduler rules; queues may delay the actual start. The first matching window takes priority.'}</Text>
                {(schedule?.rules || []).map((rule, index) => <div className={`dashboard-rule ${index === schedule.rule_index ? 'is-current' : ''}`} key={`${rule.name}-${index}`}>
                  <strong>{rule.name}</strong><span>{(rule.days || []).map((day) => days[day]).join(' / ')}</span><span>{rule.start}–{rule.end}</span><span>{rule.interval} min</span><span>{rule.timezone}</span>
                </div>)}
                {agent.mode !== 'SPOT_DCA' ? <Text>{zh ? '其他时段默认间隔' : 'Default outside windows'}: {schedule?.default_interval ?? '—'} min · {schedule?.timezone}</Text> : null}
              </div>,
            }]} />
          </div>
        );
      })}
    </section>
  );
}
