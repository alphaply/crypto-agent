import { Alert, Grid, Table, Tag } from 'antd';
import { usePreferences } from '../app/usePreferences';
import SpotCostBasisEditor from './SpotCostBasisEditor';
import { historyNumber, historyDate, pnlClass } from '../lib/historyPresentation';

export default function SpotHoldings({ configId, symbols, stats, authenticated }) {
  const { locale } = usePreferences();
  const zh = locale === 'zh';
  const screens = Grid.useBreakpoint();
  const known = stats.by_symbol || (stats.symbol || symbols.length === 1 ? [{ ...stats, symbol: stats.symbol || symbols[0] }] : []);
  const rows = symbols.map(symbol => {
    const row = known.find(item => item.symbol === symbol);
    return row ? { ...row, total_qty: row.cost_basis_current_quantity ?? row.total_qty, avg_cost: row.cost_basis_current_avg_cost ?? row.avg_cost } : { symbol, unavailable: true };
  });
  const fields = [
    ['total_qty', zh ? '持仓数量' : 'Quantity', 8], ['remaining_cost', zh ? '剩余成本' : 'Remaining cost', 2],
    ['avg_cost', zh ? '平均成本' : 'Average cost', 8], ['current_price', zh ? '当前价格' : 'Price', 8],
    ['market_value', zh ? '当前市值' : 'Market value', 2], ['unrealized_pnl', zh ? '浮动盈亏' : 'Unrealized PnL', 2],
    ['return_pct', zh ? '持仓收益率' : 'Holding return', 2],
  ];
  const value = (row, key, digits) => {
    const amount = ['avg_cost', 'return_pct'].includes(key) && row.total_qty === 0 ? null : row[key];
    return <span className={['unrealized_pnl', 'return_pct'].includes(key) ? pnlClass(amount) : ''}>{historyNumber(amount, digits)}{key === 'return_pct' && amount != null ? '%' : ''}</span>;
  };
  const detail = row => <div className="spot-holding-detail">
    <span>{zh ? '实际账户数量' : 'Account quantity'}: {historyNumber(row.actual_balance, 8)}</span>
    {row.cost_basis?.updated_at && <span><Tag color="gold">{zh ? '人工校准' : 'Calibrated'}</Tag>v{row.cost_basis_revision} · {historyDate(row.cost_basis.updated_at, zh)}</span>}
    {(row.unavailable || row.fees_complete === false) && <span>{zh ? '部分数据或手续费不完整；收益以已记录成本为依据。' : 'Some data or fees are incomplete; valuation uses recorded costs.'}</span>}
    {authenticated && <SpotCostBasisEditor configId={configId} symbol={row.symbol} stats={row} />}
  </div>;
  return <div className="spot-holdings">
    <div className="spot-holding-totals"><span>{zh ? '当前市值' : 'Market value'}<strong>{historyNumber(stats.market_value)} <small>{stats.quote_asset || symbols[0]?.split('/')[1]}</small></strong></span><span>{zh ? '剩余持仓成本' : 'Remaining cost'}<strong>{historyNumber(stats.remaining_cost)}</strong></span><span>{zh ? '当前浮动盈亏' : 'Unrealized PnL'}<strong className={pnlClass(stats.unrealized_pnl)}>{historyNumber(stats.unrealized_pnl)}</strong></span></div>
    {(stats.sync_status === 'partial' || stats.missing_symbols?.length > 0) && <Alert type="warning" showIcon title={zh ? '部分账户或行情数据未能核验，未知金额显示为 —' : 'Some account or market data is unverified; unknown values show —'} description={[...(stats.errors || []), ...(stats.missing_symbols || [])].join(' · ')} />}
    {screens.lg ? <Table className="spot-holdings-table" size="small" rowKey="symbol" pagination={false} scroll={{ x: 850 }} dataSource={rows} columns={[{ title: zh ? '标的' : 'Symbol', dataIndex: 'symbol', fixed: 'left' }, ...fields.map(([key, label, digits]) => ({ title: label, key, align: 'right', render: (_, row) => value(row, key, digits) }))]} expandable={{ expandedRowRender: detail }} /> : <div className="spot-holding-cards">{rows.map(row => <article key={row.symbol}><h3>{row.symbol}</h3><dl>{fields.map(([key, label, digits]) => <div key={key}><dt>{label}</dt><dd>{value(row, key, digits)}</dd></div>)}</dl>{detail(row)}</article>)}</div>}
    <p className="history-footnote">{zh ? '当前持仓 · 收益率 =（市值 − 剩余成本）÷ 剩余成本。人工校准不计入成交与累计投入。' : 'Current holdings · Return = (market value − remaining cost) / remaining cost. Calibration is not an execution or investment.'}{stats.last_sync && <> · {zh ? '更新于 ' : 'Updated '}{stats.last_sync}</>}</p>
  </div>;
}
