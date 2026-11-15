import { Alert, Space, Tag, Typography } from 'antd';
import DecisionMessages from './DecisionMessages';
import ReasoningBlock from './ReasoningBlock';

const { Text } = Typography;

export default function TaskExecutionPanel({ execution, locale }) {
  const status = String(execution?.status || '').toUpperCase();
  if (!execution || !['QUEUED', 'RUNNING', 'FINISHED', 'FAILED', 'INTERRUPTED'].includes(status)) {
    return null;
  }
  const active = ['QUEUED', 'RUNNING'].includes(status);
  const completed = status === 'FINISHED';
  const failed = ['FAILED', 'INTERRUPTED'].includes(status);
  const stalled = status === 'RUNNING' && execution.stalled === true;
  const toolCalls = Array.isArray(execution.tool_calls) ? execution.tool_calls : [];
  const title = stalled
    ? (locale === 'zh' ? '任务长时间无进展，可能已卡住' : 'Task may be stuck: no recent progress')
    : active
    ? (locale === 'zh' ? '任务正在执行' : 'Task in progress')
    : completed
      ? (locale === 'zh' ? '最近任务已完成' : 'Latest task completed')
      : status === 'INTERRUPTED'
        ? (locale === 'zh' ? '最近任务运行中断' : 'Latest task interrupted')
        : (locale === 'zh' ? '最近任务执行失败' : 'Latest task failed');
  const alertType = failed ? 'error' : stalled ? 'warning' : completed ? 'success' : 'info';
  const statusColor = failed ? 'error' : stalled ? 'warning' : completed ? 'success' : 'processing';
  const phaseLabel = locale === 'zh'
    ? ({ preparing: '准备行情与账户', thinking: '模型分析', tool_planning: '调用已规划', tool_running: '工具执行中', summarizing: '生成摘要', finalizing: '整理结果', failed: '执行失败', interrupted: '运行中断' }[execution.phase] || execution.phase || execution.status)
    : execution.phase || execution.status;

  return (
    <Alert
      className={`task-execution-panel ${active ? 'is-active' : completed ? 'is-complete' : 'is-failed'}`}
      type={alertType}
      showIcon
      message={
        <Space size={8} wrap>
          <span>{title}</span>
          <Tag color={statusColor}>{phaseLabel}</Tag>
          {execution.finished_at || execution.updated_at ? (
            <Text type="secondary">{execution.finished_at || execution.updated_at}</Text>
          ) : null}
        </Space>
      }
      description={
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          {stalled ? <Text type="warning">{locale === 'zh'
            ? `已 ${Math.floor(execution.idle_seconds / 60)} 分钟未更新进度，后续轮次仍在排队。请检查系统日志中的 TaskStalled 记录。当前轮次尚未结束。`
            : `No progress for ${Math.floor(execution.idle_seconds / 60)} minutes; later runs remain queued. Check TaskStalled in system logs. The current worker has not finished.`}</Text> : null}
          <Text>{execution.progress_message || execution.error || ''}</Text>
          {!execution.decision?.messages?.length && toolCalls.length ? (
            <div className="task-execution-tools">
              {toolCalls.map((call, index) => (
                <Tag key={call.id || `${call.name}-${index}`} color={call.status === 'failed' ? 'error' : call.status === 'completed' ? 'success' : 'blue'}>
                  {call.name || 'tool'} · {call.status || 'pending'}
                </Tag>
              ))}
            </div>
          ) : null}
          {execution.decision?.messages?.length ? <DecisionMessages
            messages={execution.decision.messages}
            zh={locale === 'zh'} active={active} phase={execution.phase}
            toolCalls={toolCalls} startedAt={execution.started_at_iso || execution.started_at}
          /> : <ReasoningBlock
            title={active
              ? (locale === 'zh' ? '实时推理' : 'Live reasoning')
              : (locale === 'zh' ? '最近任务推理' : 'Latest task reasoning')}
            content={execution.reasoning_content || ''}
            streaming={active && execution.phase === 'thinking'}
            reasoningTokens={execution.reasoning_tokens || 0}
            diagnostics={execution.reasoning_diagnostics}
            startedAt={execution.started_at_iso || execution.started_at || execution.created_at || execution.scheduled_at}
          />}
        </Space>
      }
    />
  );
}
