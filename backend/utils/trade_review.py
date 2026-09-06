"""Local execution evidence for daily reviews; never infer fills from intentions."""
import json

from backend.database import get_db_conn


def daily_exchange_evidence(config: dict, date_str: str) -> str:
    """Read actual fills for the requested day, even if no dashboard has been opened."""
    if str(config.get('mode') or '').upper() not in {'REAL', 'SPOT_DCA'}:
        return ''
    from datetime import datetime, timedelta
    from backend.database import TZ_CN
    from backend.utils.market_data import MarketTool
    start = TZ_CN.localize(datetime.strptime(date_str, '%Y-%m-%d'))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    try:
        ex = MarketTool(config_id=config['config_id']).exchange
        trades = ex.fetch_my_trades(config['symbol'], since=start_ms, limit=None,
                                   params={'until': end_ms - 1, 'paginate': True, 'paginationCalls': 5})
        unique = {}
        for trade in trades:
            if not start_ms <= int(trade.get('timestamp') or 0) < end_ms:
                continue
            info = trade.get('info') or {}
            item = {k: trade.get(k) for k in ('id', 'order', 'timestamp', 'symbol', 'side', 'price', 'amount', 'cost', 'fee')}
            item['realized_pnl'] = trade.get('realizedPnl', info.get('realizedPnl', info.get('fillPnl')))
            unique[str(trade.get('id'))] = item
        return '交易所当日成交（优先于本地历史；属于该账户该品种，可能包含手工/其他策略，不得全部归因本Agent；最多5页，完整性未证明；缺失盈亏不填0）：\n' + json.dumps({
            'returned_fill_count': len(unique), 'omitted_detail_count': max(0, len(unique) - 200),
            'fills': sorted(unique.values(), key=lambda item: item['timestamp'])[-200:],
        }, ensure_ascii=False, default=str)
    except Exception as exc:
        return f'当日交易所成交读取失败，仅能复盘本地已保存证据，不能声称完整收益：{exc}'


def daily_execution_evidence(config_id: str, date_str: str) -> str:
    sections = {}
    with get_db_conn() as conn:
        existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, date_column in (("orders", "timestamp"), ("trade_history", "timestamp"),
                                   ("position_history", "closed_at"), ("mock_orders", "close_time")):
            if table not in existing_tables:
                continue
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE config_id = ? AND date({date_column}) = ? ORDER BY {date_column}",
                (config_id, date_str),
            ).fetchall()
            # Raw exchange payloads can be very large and are not needed for a review.
            allowed = {
                "order_id", "timestamp", "trade_mode", "side", "entry_price", "amount",
                "take_profit", "stop_loss", "reason", "status", "filled_amount",
                "avg_fill_price", "filled_at", "event_type", "realized_pnl",
                "opened_at", "closed_at", "close_price", "source", "position_key",
                "trade_id", "price", "cost", "fee", "fee_currency", "close_time", "is_filled",
            }
            sections[table] = [{k: v for k, v in dict(row).items() if k in allowed} for row in rows[-200:]]
            if len(rows) > 200:
                sections[table + '_omitted_count'] = len(rows) - 200
        if 'trade_history' in existing_tables:
            stats = conn.execute(
                "SELECT COUNT(*) AS fill_count, SUM(realized_pnl) AS realized_pnl_before_fees "
                "FROM trade_history WHERE config_id=? AND date(timestamp)=?", (config_id, date_str)
            ).fetchone()
            if stats['fill_count']:
                sections['real_fill_totals'] = dict(stats)
                sections['fees_by_currency'] = [dict(row) for row in conn.execute(
                    "SELECT fee_currency, SUM(fee) AS fee FROM trade_history WHERE config_id=? AND date(timestamp)=? GROUP BY fee_currency",
                    (config_id, date_str)).fetchall()]
        if 'real_protection_events' in existing_tables:
            sections['protection_changes'] = [
                {'timestamp': row['timestamp'], 'plan': json.loads(row['payload'])}
                for row in conn.execute('SELECT timestamp,payload FROM real_protection_events WHERE config_id=? AND date(timestamp)=? ORDER BY id DESC LIMIT 200',
                                        (config_id, date_str)).fetchall()
            ][::-1]
    if not any(sections.values()):
        return ""
    return (
        "以下为本地执行证据。订单创建/撤单不等于成交，CLOSED 状态也须核对成交字段和来源；"
        "缺少成交或费用证据时标记未知。订单与持仓记录可能描述同一交易，不得重复计盈亏。\n"
        "trade_history可能仅已同步部分成交，历史realized_pnl=0也可能是来源缺失的默认值；不能把记录总计当完整账户净收益。费用按币种列示，不跨币种相加。\n"
        + json.dumps(sections, ensure_ascii=False, default=str)
    )
