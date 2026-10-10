import { Alert, Card, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import ReasoningBlock from './ReasoningBlock';
import DecisionMessages, { ToolReceipt } from './DecisionMessages';
import { splitThinkingContent } from '../lib/thinking';
import { usePreferences } from '../app/usePreferences';

export default function DecisionRunPanel({ agent }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const record = agent.decision;
  const messages = record?.messages || [];
  const fallback = splitThinkingContent(agent.content || '', agent.reasoning_content || '');
  return <Card className="panel-card decision-run-panel" title={zh ? '1 · 决策与工具调用' : '1 · Decision and tools'} extra={<Typography.Text type="secondary">{agent.timestamp}</Typography.Text>}>
    <Typography.Paragraph type="secondary">{zh ? '决策模型的实际输出，按调用顺序展示；工具返回成功不代表订单已成交。' : 'Actual model output in execution order. A successful tool response does not prove an order filled.'}</Typography.Paragraph>
    {record?.unavailable && <Alert type="warning" showIcon title={zh ? '调用记录无法读取，以下保留已保存的决策正文。' : 'Trace unavailable. Showing saved decision text.'} />}
    {messages.length ? <DecisionMessages messages={messages} zh={zh} /> : <>
      {record?.legacy && <Typography.Paragraph type="secondary">{zh ? '历史记录：展示保存的决策原文与回执，旧数据未记录完整调用顺序及参数。' : 'Historical record: original decision and receipts; complete call order and arguments were not recorded.'}</Typography.Paragraph>}
      <ReasoningBlock title={zh ? '推理过程' : 'Reasoning'} content={fallback.reasoning} reasoningTokens={agent.reasoning_tokens || 0} />
      <MarkdownBlock content={fallback.content || (zh ? '暂无已保存的决策输出。' : 'No saved decision output yet.')} />
      {(record?.execution_results || []).map((item, index) => <ToolReceipt key={index} item={item} zh={zh} />)}
    </>}
  </Card>;
}
