import React from 'react';
import { Alert, Button, Card, Space, Tag, Typography } from 'antd';
import MarkdownBlock from './MarkdownBlock';
import { usePreferences } from '../app/usePreferences';
export default function TradeReportPanel({ report, summaryOnly = false, onOpenReport }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  if (!report) return null;
  const decision = report.decision || {};
  const labels = { BUY: '买入', SELL: '卖出', HOLD: '观望', CLOSE: '平仓', MANAGE: '管理持仓' };
  if (summaryOnly) return <Card className="panel-card agent-decision-summary" size="small">
    <div className="agent-decision-heading"><Space wrap><Typography.Text strong>{zh ? '最近决策' : 'Latest decision'}</Typography.Text>{decision.action && <Tag color={decision.action === 'HOLD' ? 'default' : 'blue'}>{zh ? labels[decision.action] || decision.action : decision.action}</Tag>}</Space><Button type="link" size="small" onClick={onOpenReport}>{zh ? '查看完整报告' : 'Full report'}</Button></div>
    {report.validation_status === 'invalid' ? <Alert type="warning" showIcon title={zh ? '报告格式未通过校验，请查看运行原文。' : 'Report validation failed. Review the original output.'} /> : <MarkdownBlock content={decision.rationale || report.strategy || (zh ? '本轮未提供决策摘要。' : 'No decision summary for this run.')} />}
  </Card>;
  return <Card className="panel-card trade-report" title={zh ? '运行报告' : 'Run report'} extra={<Tag color={decision.action === 'HOLD' ? 'default' : 'blue'}>{zh ? labels[decision.action] || decision.action : decision.action}</Tag>}>
    {report.validation_status === 'invalid' && <Alert type="warning" showIcon title={zh ? '报告格式未通过校验，请查看本次运行原文。' : 'Report validation failed. Review the original run output.'} />}
    <div className="trade-report-grid">
      <section><Typography.Text strong>{zh ? '行情解析' : 'Market analysis'}</Typography.Text><MarkdownBlock content={report.market_analysis || '—'} /></section>
      <section><Typography.Text strong>{zh ? '主策略与交易决策' : 'Strategy and decision'}</Typography.Text><MarkdownBlock content={[report.strategy, decision.rationale].filter(Boolean).join('\n\n')} /></section>
      <section><Typography.Text strong>{zh ? '未来 1h' : 'Next hour'}</Typography.Text><MarkdownBlock content={report.forecast?.next_1h || '—'} /></section>
      <section><Typography.Text strong>{zh ? '未来 4h' : 'Next four hours'}</Typography.Text><MarkdownBlock content={report.forecast?.next_4h || '—'} /></section>
    </div>
    <Space direction="vertical" style={{ width: '100%' }}><Typography.Text strong>{zh ? '风险与失效条件' : 'Risks and invalidation'}</Typography.Text><MarkdownBlock content={[...(report.risks || []).map((item) => '- ' + item), report.invalidation].filter(Boolean).join('\n')} /><Typography.Text strong>{zh ? '下次观察点' : 'Next watchpoints'}</Typography.Text><MarkdownBlock content={(report.next_watchpoints || []).map((item) => '- ' + item).join('\n')} /></Space>
    <details className="report-receipts"><summary>{zh ? '实际执行回执' : 'Execution receipts'} ({report.execution_results?.length || 0})</summary>{report.execution_results?.length ? report.execution_results.map((item, index) => <div key={item.tool_call_id || index}><Tag>{item.tool}</Tag><pre>{typeof item.result === 'string' ? item.result : JSON.stringify(item.result, null, 2)}</pre></div>) : <Typography.Text type="secondary">{zh ? '本次未执行交易操作。' : 'No trading operation was executed.'}</Typography.Text>}</details>
  </Card>;
}
