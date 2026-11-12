import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button, Card, Segmented, theme } from 'antd';
import AccessibleConfigProvider from '../src/components/AccessibleConfigProvider';
import { PreferencesContext } from '../src/app/preferences-context';
import DecisionMessages from '../src/components/DecisionMessages';
import ToolResultContent from '../src/components/ToolResultContent';
import 'antd/dist/reset.css';
import '../src/index.css';
import '../src/workbench.css';
import '../src/pages/AgentsPage.css';

const params = new URLSearchParams(location.search);
const zh = params.get('locale') !== 'en';
const dark = params.get('theme') === 'dark';
document.documentElement.dataset.theme = dark ? 'dark' : 'light';
const calls = [
  { id: 'stop', name: 'close_position', args: { side: 'SHORT', amount: 0.15, order_type: 'stop_limit', trigger_price: 2115, price: 2116, reason: '为现有空头设置止损，控制反弹风险。' } },
  { id: 'profit', name: 'close_position', args: { side: 'SHORT', amount: 0.15, order_type: 'take_profit_limit', trigger_price: 2025, price: 2024, reason: '在目标价位设置止盈。' } },
];
const noCalls = [{ role: 'assistant', content: '当前持有空头仓位，计划设置止盈止损。\n\n```json\n' + JSON.stringify(calls.map(c => c.args), null, 2) + '\n```',
  reasoning_diagnostics: { tools_sent: 5, tool_calls_returned: 0, tool_choice: 'auto', finish_reason: 'stop', tool_evidence_state: 'no_calls' } }];
const results = calls.map(call => ({ role: 'tool', tool: call.name, tool_call_id: call.id,
  result: { action: 'close', status: 'submitted', symbol: 'ETH/USDT:USDT', ...call.args, filled_amount: 0, order_status: 'waiting_trigger' } }));

function Fixture() {
  const [state, setState] = useState(params.get('state') || 'running');
  const [mobile, setMobile] = useState(params.get('width') === '390');
  const messages = state === 'no_calls' ? noCalls : [{ role: 'assistant', content: '已完成账户核对，正在为现有空头设置退出订单。', tool_calls: calls }, ...(state === 'completed' ? results : [])];
  return <AccessibleConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
    <PreferencesContext.Provider value={{ locale: zh ? 'zh' : 'en', isDark: dark, t: value => value }}>
      <main className="agent-workspace-compact" style={{ maxWidth: mobile ? 390 : 880, margin: '24px auto', padding: '0 16px', width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 18 }}>
          <div><div className="workspace-eyebrow">AGENT WORKSPACE</div><h2 style={{ margin: 0 }}>{zh ? '工具调用与回执' : 'Tools & receipts'}</h2><p style={{ color: 'var(--text-secondary)', fontSize: 12, margin: '6px 0 0' }}>{zh ? '离线合成数据 · 不连接模型或交易所' : 'Synthetic data · no model or exchange connection'}</p></div>
          <Button onClick={() => setMobile(value => !value)}>{mobile ? '桌面宽度' : '手机宽度'}</Button>
        </div>
        <Segmented style={{ marginBottom: 16 }} value={state} onChange={setState} options={[{ value: 'running', label: zh ? '执行中' : 'Running' }, { value: 'completed', label: zh ? '已返回' : 'Returned' }, { value: 'no_calls', label: zh ? '仅输出文本' : 'Text only' }]} />
        <Card className="panel-card" title="ETH · Argon"><DecisionMessages messages={messages} zh={zh} active={state === 'running'} phase="tool_running" toolCalls={[{ id: 'stop', status: 'running' }, { id: 'profit', status: 'planned' }]} /></Card>
        <Card className="panel-card" style={{ marginTop: 18 }} title={zh ? '历史撤单回执' : 'Legacy cancellation'}>
          {['MUB', 'BNB'].map((symbol, i) => <ToolResultContent key={symbol} zh={zh} toolName="cancel_order" request={{ reason: `撤销旧${symbol}限价买单，释放资金后按最新结构重挂`, order_id: `regular:${39304557 + i}` }}
            content={`1. cancel: regular:${39304557 + i} buy 数量0.009 价格73.5 已成交数量0.0，撤单成功。`} />)}
        </Card>
      </main>
    </PreferencesContext.Provider>
  </AccessibleConfigProvider>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
