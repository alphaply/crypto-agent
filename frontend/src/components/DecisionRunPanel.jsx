import { Alert, Card, Space, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';

function Receipt({ item, zh }) {
  const colors = { failed: 'error', unknown: 'warning', pending: 'warning', completed: 'success' };
  const labels = { failed: '失败', unknown: '待核验', pending: '待确认', completed: '已返回', not_executed: '未执行' };
  let result = item.result;
  try { result = JSON.stringify(typeof result === 'string' ? JSON.parse(result) : result, null, 2); } catch { /* Keep non-JSON tool output intact. */ }
  return <div className="decision-tool-result">
    <Space wrap><Typography.Text strong>{item.tool || 'tool'}</Typography.Text>{item.status && <Tag color={colors[item.status]}>{zh ? labels[item.status] || item.status : item.status}</Tag>}<Typography.Text type="secondary">{item.tool_call_id}</Typography.Text></Space>
    <pre>{result || (zh ? '未返回内容' : 'No output')}</pre>
  </div>;
}

export default function DecisionRunPanel({ agent }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const record = agent.decision;
  const messages = record?.messages || [];
  const fallback = splitThinkingContent(agent.content || '', agent.reasoning_content || '');
  return <Card className="panel-card decision-run-panel" title={zh ? '1 · 决策与工具调用' : '1 · Decision and tools'} extra={<Typography.Text type="secondary">{agent.timestamp}</Typography.Text>}>
    <Typography.Paragraph type="secondary">{zh ? '决策模型的实际输出，按调用顺序展示；工具返回成功不代表订单已成交。' : 'Actual model output in execution order. A successful tool response does not prove an order filled.'}</Typography.Paragraph>
    {record?.unavailable && <Alert type="warning" showIcon title={zh ? '调用记录无法读取，以下保留已保存的决策正文。' : 'Trace unavailable. Showing saved decision text.'} />}
    {messages.length ? <div className="decision-message-list">{messages.map((entry, index) => {
      if (entry.role === 'tool') return <Receipt key={`${index}-${entry.tool_call_id}`} item={entry} zh={zh} />;
      const text = splitThinkingContent(entry.content || '', entry.reasoning_content || '');
      return <section className="decision-model-output" key={index}>
        <Typography.Text strong>{zh ? '模型输出' : 'Model output'}</Typography.Text>
        {entry.output_warning && <Alert type="warning" showIcon title={entry.output_warning} />}
        <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={text.reasoning} reasoningTokens={entry.reasoning_tokens || 0} />
        <MarkdownBlock content={text.content} />
        {(entry.tool_calls || []).map((call) => <details className="content-disclosure decision-tool-call" key={call.id}>
          <summary>{zh ? '调用工具' : 'Tool call'} · {call.name}</summary><pre>{JSON.stringify(call.args, null, 2)}</pre>
        </details>)}
      </section>;
    })}</div> : <>
      {record?.legacy && <Typography.Paragraph type="secondary">{zh ? '历史记录：展示保存的决策原文与回执，旧数据未记录完整调用顺序及参数。' : 'Historical record: original decision and receipts; complete call order and arguments were not recorded.'}</Typography.Paragraph>}
      <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={fallback.reasoning} reasoningTokens={agent.reasoning_tokens || 0} />
      <MarkdownBlock content={fallback.content || (zh ? '暂无已保存的决策输出。' : 'No saved decision output yet.')} />
      {(record?.execution_results || []).map((item, index) => <Receipt key={index} item={item} zh={zh} />)}
    </>}
  </Card>;
}
