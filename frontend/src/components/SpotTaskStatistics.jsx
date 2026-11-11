import { Alert, Grid, Segmented, Table } from 'antd';
import { Fees, HistoryIssues } from './PositionHistory';
import { historyNumber, historyDate, pnlClass } from '../lib/historyPresentation';

export default function SpotTaskStatistics({ data, period, setPeriod, zh }) {
  const screens = Grid.useBreakpoint();
  const current = data[period];
  const counts = (value, complete) => <>{historyNumber(value, 0)}{!complete && <small>{zh ? '已记录 · 明细不完整' : 'Recorded · incomplete'}</small>}</>;
  const fields = [
    ['fill_count', zh ? '成交总笔数' : 'Total fills'], ['buy_fill_count', zh ? '买入成交' : 'Buy fills'],
    ['sell_fill_count', zh ? '卖出成交' : 'Sell fills'], ['realized_net_pnl', zh ? '已实现净盈亏' : 'Realized net PnL'],
  ];
  const columns = [
    { title: zh ? '标的' : 'Symbol', dataIndex: 'symbol', fixed: 'left' },
    { title: zh ? '买入笔数' : 'Buy fills', render: (_, row) => counts(row[period].buy_fill_count, row[period].counts_complete) },
    { title: zh ? '卖出笔数' : 'Sell fills', render: (_, row) => counts(row[period].sell_fill_count, row[period].counts_complete) },
    { title: zh ? '净盈亏' : 'Net PnL', render: (_, row) => <span className={pnlClass(row[period].realized_net_pnl)}>{row[period].realized_net_pnl == null ? (zh ? '待核实' : 'Unconfirmed') : historyNumber(row[period].realized_net_pnl)}</span> },
  ];
  return <div className="spot-task-statistics">
    {Object.keys(data.errors || {}).length > 0 && <Alert type="warning" title={zh ? '部分统计待核实' : 'Some statistics need verification'} description={Object.values(data.errors).join(' · ')} showIcon />}
    <section className="spot-allocation"><div className="spot-section-heading"><div><h3>{zh ? '累计投入' : 'Cumulative investment'}</h3><p>{zh ? '全部历史 · 按成交买入金额分配' : 'All history · allocated by executed buy value'}</p></div><strong>{historyNumber(data.cumulative_buy_amount)} <small>{data.currency}</small></strong></div>
      <div className="allocation-list">{data.by_symbol.map((row, index) => <div className="allocation-row" key={row.symbol}>
        <span className="allocation-symbol"><i style={{ background: `var(--allocation-${index % 5})` }} />{row.symbol}</span>
        <div className="allocation-track" role="img" aria-label={`${row.symbol} ${row.investment_share_pct == null ? '—' : historyNumber(row.investment_share_pct) + '%'}`}><span style={{ width: `${row.investment_share_pct ?? 0}%`, background: `var(--allocation-${index % 5})` }} /></div>
        <span>{historyNumber(row.cumulative_buy_amount)} <small>{data.currency}</small></span><b>{row.investment_share_pct == null ? '—' : `${historyNumber(row.investment_share_pct)}%`}</b>
      </div>)}</div>
    </section>
    <section className="spot-execution-stats"><div className="spot-section-heading"><div><h3>{zh ? '成交与已实现盈亏' : 'Executions & realized PnL'}</h3><p>{zh ? '逐笔成交计数 · 买入手续费随卖出份额结算' : 'Individual fills · entry fees allocated on sale'}</p></div>
      <Segmented size="small" value={period} onChange={setPeriod} options={[{ value: 'seven_days', label: zh ? '最近 7 天' : 'Last 7 days' }, { value: 'all_history', label: zh ? '全部历史' : 'All history' }]} /></div>
      <div className="spot-metric-grid">{fields.map(([key, label]) => <div className="task-stat" key={key}><span>{label}</span><strong className={key === 'realized_net_pnl' ? pnlClass(current[key]) : ''}>{key === 'realized_net_pnl' ? current[key] == null ? (zh ? '待核实' : 'Unconfirmed') : historyNumber(current[key]) : historyNumber(current[key], 0)}</strong><small>{key === 'realized_net_pnl' ? data.currency : !current.counts_complete ? (zh ? '已记录 · 明细不完整' : 'Recorded · incomplete') : (zh ? '按实际成交明细' : 'Recorded individual fills')}</small></div>)}</div>
      {screens.md ? <Table size="small" rowKey="symbol" pagination={false} dataSource={data.by_symbol} columns={columns} expandable={{ expandedRowRender: row => <><Fees fees={row[period].fees_by_currency} zh={zh} /><HistoryIssues issues={row[period].issues} zh={zh} /></> }} /> : <div className="spot-execution-cards">{data.by_symbol.map(row => <article key={row.symbol}><header><strong>{row.symbol}</strong><b className={pnlClass(row[period].realized_net_pnl)}>{historyNumber(row[period].realized_net_pnl)} {data.currency}</b></header><p>{zh ? '买入' : 'Buy'} {row[period].buy_fill_count} · {zh ? '卖出' : 'Sell'} {row[period].sell_fill_count}{!row[period].counts_complete && (zh ? ' · 已记录，明细不完整' : ' · recorded, incomplete')}</p><details><summary>{zh ? '费用与记录说明' : 'Fees & evidence'}</summary><Fees fees={row[period].fees_by_currency} zh={zh} /><HistoryIssues issues={row[period].issues} zh={zh} /></details></article>)}</div>}
    </section>
    <p className="history-footnote">{zh ? '仅当前任务 · 本地已归属记录 · 更新于 ' : 'This task · attributed local records · Updated '}{historyDate(data.as_of, zh)}</p>
  </div>;
}
