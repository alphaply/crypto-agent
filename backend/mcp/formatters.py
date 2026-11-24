"""Human-readable Chinese formatters for MCP read and account-configuration responses."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))

_MARKET_LABELS = {'spot': '现货', 'swap': '永续'}
_SIDE_LABELS = {'LONG': '多', 'SHORT': '空'}
_ACTION_LABELS = {
    'buy': '买入',
    'sell': '卖出',
    'open_long': '开多',
    'open_short': '开空',
    'close_long': '平多',
    'close_short': '平空',
    'borrow': '借款',
    'repay': '还款',
    'reset': '下海重置',
    'create': '开户',
    'bot_created': 'Bot开户',
    'preferences': '修改偏好',
    'configure': '更新配置',
    'place_order': '挂单',
    'edit_order': '改单',
    'cancel_order': '撤单',
    'order_filled': '成交',
    'order_cancelled': '撤单',
    'order_rejected': '拒单',
    'liquidation': '爆仓',
    'bankruptcy': '破产',
    'bankruptcy_recovered': '恢复正常',
    'interest': '扣息',
    'transfer_in': '转入',
    'transfer_out': '转出',
    'admin_adjustment': '后台调整',
    'set_leverage': '调杠杆',
    'add_margin': '加保证金',
    'reduce_margin': '减保证金',
}
_STATE_LABELS = {
    'open': '待成交',
    'filled': '已成交',
    'closed': '已平/结束',
    'cancelled': '已撤销',
    'canceled': '已撤销',
    'rejected': '已拒绝',
    'active': '正常',
    'bankrupt': '破产冻结',
    'complete': '完整',
    'partial': '部分',
    'incomplete': '不完整',
    'processing': '处理中',
    'disabled': '未启用',
    'awaiting_first_snapshot': '等待首份快照',
}
_RECORD_TYPE_LABELS = {
    'orders': '委托列表',
    'order': '委托详情',
    'fills': '成交明细',
    'history': '历史流水',
}


def _fmt_num(value: Any, *, signed: bool = False, suffix: str = '') -> str:
    if value is None or isinstance(value, bool):
        return '—'
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return '—'
        text = f'{value:,.8f}'.rstrip('0').rstrip('.')
        if text == '-0':
            text = '0'
        if signed and value > 0:
            text = '+' + text
        return text + suffix
    text = str(value).strip()
    return (text + suffix) if text else '—'


def _fmt_pct(value: Any, *, signed: bool = False) -> str:
    if value is None or isinstance(value, bool):
        return '—'
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(num):
        return '—'
    text = f'{num:.4f}'.rstrip('0').rstrip('.')
    if signed and num > 0:
        text = '+' + text
    return text + '%'


def _fmt_time(value: Any) -> str:
    if value is None or value == '':
        return '—'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value <= 0:
            return '—'
        ts = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(ts, BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return '—'
        try:
            iso = raw[:-1] + '+00:00' if raw.endswith('Z') else raw
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            return raw
    return str(value)


def _label_market(value: Any) -> str:
    return _MARKET_LABELS.get(str(value or '').lower(), str(value or '—'))


def _label_action(value: Any) -> str:
    raw = str(value or '')
    return _ACTION_LABELS.get(raw.lower(), raw or '—')


def _label_state(value: Any) -> str:
    raw = str(value or '')
    return _STATE_LABELS.get(raw.lower(), raw or '—')


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _format_market(result: dict) -> str:
    symbol = result.get('symbol') or '—'
    exchange = result.get('exchange') or ''
    market_type = _label_market(result.get('market_type')) if result.get('market_type') else ''
    venue_parts = [p for p in (exchange, market_type) if p and p != '—']
    venue_text = f"（{' · '.join(venue_parts)}）" if venue_parts else ''
    name = result.get('name')
    underlying = result.get('underlying_symbol')
    asset_tag = ''
    if name and underlying and str(name) != str(underlying):
        asset_tag = f' [{name}]'
    status = result.get('status') or 'complete'
    obs = result.get('observed_at')
    if isinstance(obs, dict):
        obs_vals = [v for v in obs.values() if isinstance(v, (int, float))]
        obs_str = _fmt_time(max(obs_vals)) if obs_vals else '—'
    else:
        obs_str = _fmt_time(obs)
    lines = [f"【行情】{symbol}{venue_text}{asset_tag} | 状态: {_label_state(status)} | 北京时间: {obs_str}"]

    ticker = result.get('ticker') if isinstance(result.get('ticker'), dict) else {}
    last = result.get('price') if result.get('price') is not None else ticker.get('last') or ticker.get('close')
    bid = result.get('bid') if result.get('bid') is not None else ticker.get('bid')
    ask = result.get('ask') if result.get('ask') is not None else ticker.get('ask')
    pct = result.get('change_percentage_24h') if result.get('change_percentage_24h') is not None else ticker.get('percentage')
    vol = result.get('quote_volume_24h') if result.get('quote_volume_24h') is not None else ticker.get('quoteVolume')
    high, low = ticker.get('high'), ticker.get('low')
    if any(v is not None for v in (last, bid, ask, pct, vol, high, low)):
        parts = [f"现价 {_fmt_num(last)}"]
        if pct is not None:
            parts.append(f"24h涨跌 {_fmt_pct(pct, signed=True)}")
        if bid is not None or ask is not None:
            parts.append(f"买一/卖一 {_fmt_num(bid)}/{_fmt_num(ask)}")
        if low is not None and high is not None:
            parts.append(f"24h高低 {_fmt_num(low)}~{_fmt_num(high)}")
        if vol is not None:
            parts.append(f"24h成交额 {_fmt_num(vol)}")
        lines.append('报价: ' + ' | '.join(parts))

    analysis = result.get('analysis')
    if isinstance(analysis, dict) and analysis:
        lines.append('技术指标:')
        for tf, info in analysis.items():
            if not isinstance(info, dict):
                lines.append(f'- {tf}: {info}')
                continue
            pieces = []
            if info.get('price') is not None:
                pieces.append(f"收盘 {info['price']}")
            trend = info.get('trend') if isinstance(info.get('trend'), dict) else {}
            if trend.get('status'):
                pieces.append(f"趋势 {trend['status']}")
            if trend.get('adx') is not None:
                pieces.append(f"ADX {_fmt_num(trend['adx'])}")
            rsi_info = info.get('rsi_analysis') if isinstance(info.get('rsi_analysis'), dict) else {}
            rsi_val = rsi_info.get('rsi') if rsi_info else info.get('rsi')
            if rsi_val is not None:
                pieces.append(f"RSI {_fmt_num(rsi_val)}")
            macd = info.get('macd') if isinstance(info.get('macd'), dict) else {}
            if macd:
                macd_str = f"MACD({_fmt_num(macd.get('diff'))}/{_fmt_num(macd.get('dea'))}, hist {_fmt_num(macd.get('hist'))}"
                if macd.get('momentum'):
                    macd_str += f", {macd['momentum']}"
                pieces.append(macd_str + ')')
            ema = info.get('ema') if isinstance(info.get('ema'), dict) else {}
            if ema:
                ema_parts = [f"{k.replace('ema_', 'EMA')}:{v}" for k, v in ema.items() if v is not None]
                if ema_parts:
                    pieces.append(' '.join(ema_parts[:4]))
            bb = info.get('bollinger') if isinstance(info.get('bollinger'), dict) else {}
            if bb and any(bb.get(k) is not None for k in ('low', 'mid', 'up')):
                pieces.append(f"BOLL({bb.get('low', '—')}/{bb.get('mid', '—')}/{bb.get('up', '—')})")
            dc = info.get('decision_context')
            if isinstance(dc, str) and dc.strip() and not pieces:
                pieces.append(dc.strip()[:120])
            if not pieces:
                pieces.append(_safe_json(info)[:160])
            lines.append(f"- {tf}: " + ' | '.join(pieces))

    sentiment = result.get('sentiment')
    if isinstance(sentiment, dict) and sentiment:
        s_parts = []
        if sentiment.get('funding_rate') is not None:
            s_parts.append(f"资金费率 {_fmt_num(sentiment['funding_rate'])}")
        if sentiment.get('open_interest') is not None:
            s_parts.append(f"持仓量 {_fmt_num(sentiment['open_interest'])}")
        if sentiment.get('ls_ratio') not in (None, 'N/A', ''):
            s_parts.append(f"多空比 {sentiment['ls_ratio']}")
        if sentiment.get('ls_accounts') not in (None, 'N/A', ''):
            s_parts.append(f"多空账户比 {sentiment['ls_accounts']}")
        if sentiment.get('liquidations_24h') not in (None, 'N/A', ''):
            s_parts.append(f"24h爆仓 {sentiment['liquidations_24h']}")
        if s_parts:
            lines.append('衍生品情绪: ' + ' | '.join(s_parts))

    if 'candles' in result:
        tf = result.get('timeframe') or '1h'
        candles = result.get('candles')
        if isinstance(candles, list) and candles:
            highs = [row[2] for row in candles if isinstance(row, (list, tuple)) and len(row) >= 5 and row[2] is not None]
            lows = [row[3] for row in candles if isinstance(row, (list, tuple)) and len(row) >= 5 and row[3] is not None]
            last_row = candles[-1] if isinstance(candles[-1], (list, tuple)) and len(candles[-1]) >= 5 else None
            last_close = last_row[4] if last_row else None
            range_str = f" | 区间 {_fmt_num(min(lows))}~{_fmt_num(max(highs))}" if highs and lows else ''
            close_str = f" | 最新收盘 {_fmt_num(last_close)}" if last_close is not None else ''
            lines.append(f"K线({tf}): 共 {len(candles)} 根{range_str}{close_str}")
        else:
            lines.append(f"K线({tf}): 暂无数据")

    missing = result.get('missing_sources') or []
    if status == 'partial' or missing:
        lines.append('提示: 部分数据源缺失 (' + ', '.join(str(m) for m in missing) + ')')
    return '\n'.join(lines)


def _format_account_state(result: dict) -> str:
    mode = result.get('execution_mode') or 'paper'
    mode_label = '模拟' if mode == 'paper' else '实盘'
    ident = result.get('user_id') or result.get('profile_id') or '—'
    status_label = _label_state(result.get('account_status')) if result.get('account_status') else ''
    header = f"【账户状态】{ident}（{mode_label}" + (f" · {status_label}" if status_label else '') + '）'
    if result.get('account_epoch') is not None or result.get('reset_count') is not None:
        header += f" | 第 {result.get('account_epoch', 0)} 轮 / 下海 {result.get('reset_count', 0)} 次"
    lines = [header]

    completeness = result.get('completeness') if isinstance(result.get('completeness'), dict) else {}
    if completeness.get('valuation') == 'incomplete' or result.get('valuation_status') == 'incomplete':
        reasons = ', '.join(str(r) for r in (completeness.get('reason_codes') or [])) or '历史扫描或估值未完成'
        lines.append(f"估值提示: 不完整（{reasons}），缺项显示为 —")

    balances = result.get('balances')
    if isinstance(balances, dict):
        if mode == 'paper':
            lines.append(
                f"资金(USDT): 账本现金 {_fmt_num(balances.get('cash'))} | 可用 {_fmt_num(balances.get('available_cash'))} | "
                f"挂单占用 {_fmt_num(balances.get('reserved_cash'))} | 持仓保证金 {_fmt_num(balances.get('used_margin'))} | "
                f"负债 {_fmt_num(balances.get('debt'))} | 净资产 {_fmt_num(balances.get('net_equity'))}"
            )
            reserve = balances.get('interest_reserve')
            if isinstance(reserve, dict) and reserve.get('message'):
                lines.append('付息提醒: ' + reserve['message'])
        else:
            if any(k in balances for k in ('total', 'free', 'used')):
                lines.append(
                    f"资金: 总额 {_fmt_num(balances.get('total'))} | 可用 {_fmt_num(balances.get('free'))} | "
                    f"冻结 {_fmt_num(balances.get('used'))}"
                )
            else:
                non_zero = []
                for asset, item in balances.items():
                    if isinstance(item, dict):
                        tot = item.get('total') or item.get('free') or item.get('used') or 0
                        if tot:
                            non_zero.append(f"{asset}: 可用 {_fmt_num(item.get('free'))}/总 {_fmt_num(item.get('total'))}")
                lines.append('资金: ' + (' | '.join(non_zero[:12]) if non_zero else '暂无非零余额'))

    if 'positions' in result:
        positions = result.get('positions') or []
        if not positions:
            lines.append('合约持仓: 无')
        else:
            lines.append(f"合约持仓 ({len(positions)}):")
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                side = _SIDE_LABELS.get(str(pos.get('side', '')).upper(), str(pos.get('side') or '—'))
                lev = f" {_fmt_num(pos.get('leverage'))}×" if pos.get('leverage') is not None else ''
                amount = pos.get('amount') if pos.get('amount') is not None else pos.get('base_amount')
                entry = pos.get('entry_price')
                mark = pos.get('mark_price')
                upnl = pos.get('unrealized_pnl')
                margin = pos.get('margin')
                pid = pos.get('resource_id') or pos.get('position_id')
                line = (
                    f"- {pos.get('symbol', '—')} {side}{lev} | 数量 {_fmt_num(amount)} | "
                    f"开仓 {_fmt_num(entry)} / 观察价 {_fmt_num(mark)} | 浮盈 {_fmt_num(upnl, signed=True)}"
                )
                if margin is not None:
                    line += f" | 保证金 {_fmt_num(margin)}"
                if pid:
                    line += f" | position_id={pid}"
                lines.append(line)

    spot_list = result.get('spot') if 'spot' in result else result.get('inventory')
    if 'spot' in result or 'inventory' in result:
        holdings = spot_list if isinstance(spot_list, list) else ([spot_list] if isinstance(spot_list, dict) and spot_list else [])
        if holdings:
            lines.append(f"现货持仓 ({len(holdings)}):")
            for item in holdings:
                if not isinstance(item, dict):
                    continue
                amt = item.get('amount') if item.get('amount') is not None else item.get('available_amount')
                line = f"- {item.get('symbol', '—')} | 数量 {_fmt_num(amt)}"
                if item.get('entry_price') is not None:
                    line += f" | 成本 {_fmt_num(item.get('entry_price'))}"
                if item.get('mark_price') is not None:
                    line += f" | 观察价 {_fmt_num(item.get('mark_price'))}"
                if item.get('market_value') is not None:
                    line += f" | 市值 {_fmt_num(item.get('market_value'))}"
                lines.append(line)
        elif 'inventory' in result:
            lines.append('现货持仓: 无')

    if 'orders' in result:
        orders = result.get('orders') or []
        if not orders:
            lines.append('当前挂单: 无')
        else:
            lines.append(f"当前挂单 ({len(orders)}):")
            for order in orders[:20]:
                lines.append('- ' + _format_order_line(order))
            if len(orders) > 20:
                lines.append(f"... 其余 {len(orders) - 20} 笔挂单略")

    perf = result.get('performance')
    if isinstance(perf, dict):
        lines.append(f"周期统计({perf.get('period', 'current_cycle')}): {perf.get('summary') or _label_state(perf.get('status'))}")
        p = perf.get('performance') if isinstance(perf.get('performance'), dict) else {}
        c = perf.get('current') if isinstance(perf.get('current'), dict) else {}
        lines.append(
            f"已实现净盈亏: {_fmt_num(p.get('realized_net'), signed=True)} | 扣息后: {_fmt_num(p.get('realized_net_after_interest'), signed=True)} | "
            f"手续费: {_fmt_num(p.get('fees'))} | 已付利息: {_fmt_num(p.get('interest_paid'))} | 当前净资产: {_fmt_num(c.get('net_equity'))}"
        )

    return '\n'.join(lines)


def _format_trading_context(result: dict) -> str:
    mode = result.get('execution_mode') or 'paper'
    if mode == 'paper':
        cfg = result.get('settings') if isinstance(result.get('settings'), dict) else {}
        lines = [
            f"【交易上下文】模式: 模拟交易 | 命名空间: {result.get('namespace', '—')} | bot_user_id: {result.get('bot_user_id', '—')}",
            f"规则参数: 初始资金 {_fmt_num(cfg.get('initial_balance'))} USDT | 最大杠杆 {_fmt_num(cfg.get('max_leverage'))}× | "
            f"手续费率 {_fmt_num(cfg.get('fee_rate'))} | 维持保证金率 {_fmt_num(cfg.get('maintenance_margin_rate'))}",
            f"银行借贷: {'开启' if cfg.get('bank_enabled') else '关闭'} | 额度上限 {_fmt_num(cfg.get('loan_limit'))} USDT | "
            f"日利率 {_fmt_num(cfg.get('daily_interest_rate'))} (每日北京时间00:00计息)",
            '支持动作: ' + ', '.join(str(a) for a in (result.get('allowed_actions') or [])),
        ]
        acct_caps = result.get('account_capabilities')
        if isinstance(acct_caps, dict):
            lines.append(
                f"指定账户({acct_caps.get('user_id', '—')}): 状态 {_label_state(acct_caps.get('account_status'))} | "
                f"默认杠杆 {_fmt_num(acct_caps.get('leverage'))}× | 市场 {','.join(acct_caps.get('markets') or [])} | "
                f"第 {acct_caps.get('account_epoch', 0)} 轮 / 下海 {acct_caps.get('reset_count', 0)} 次"
            )
        return '\n'.join(lines)

    profiles = result.get('profiles') or []
    lines = [f"【交易上下文】模式: 实盘 | 权限: {','.join(result.get('scopes') or [])} | 可用Profile ({len(profiles)}):"]
    for row in profiles:
        if not isinstance(row, dict):
            continue
        caps = row.get('capabilities') if isinstance(row.get('capabilities'), dict) else {}
        actions = ','.join(str(a) for a in (row.get('allowed_actions') or []))
        lines.append(
            f"- profile_id={row.get('profile_id')} | {row.get('exchange', '—')} · {_label_market(row.get('market_type'))} | "
            f"模式 {caps.get('mode', row.get('mode', '—'))} | 允许动作: {actions or '只读'}"
        )
    return '\n'.join(lines)


def _format_search_symbols(result: dict) -> str:
    symbols = result.get('symbols') or result.get('items') or []
    total = result.get('total')
    total_text = str(total) if total is not None else str(len(symbols))
    lines = [
        f"【标的搜索】命中 {total_text} 个，本页返回 {len(symbols)} 个"
        + ('（仍有更多，可增大 offset 翻页）' if result.get('has_more') else '')
    ]
    for item in symbols[:30]:
        if not isinstance(item, dict):
            lines.append(f'- {item}')
            continue
        mtype = _label_market(item.get('market_type'))
        name = item.get('name')
        extra = f" | {name}" if name and name != item.get('base') else ''
        lines.append(f"- {item.get('symbol', '—')} ({item.get('exchange', '—')} · {mtype}{extra})")
    if len(symbols) > 30:
        lines.append(f"... 本页还有 {len(symbols) - 30} 个标的未展开")
    hints = result.get('asset_hints') or []
    if hints:
        hint_str = ', '.join(f"{h.get('base')}({h.get('name', '')})" for h in hints[:5] if isinstance(h, dict))
        if hint_str:
            lines.append('相关资产线索(非可交易证明): ' + hint_str)
    if result.get('status') == 'partial':
        lines.append('提示: 部分交易所目录查询失败，空结果不代表标的全球不存在')
    return '\n'.join(lines)


def _format_news(result: dict) -> str:
    updated = _fmt_time(result.get('summary_as_of') or result.get('as_of'))
    status = _label_state(result.get('status') or ('complete' if result.get('available', True) else 'disabled'))
    stale_text = '（已过期）' if result.get('stale') else ''
    digest = (result.get('digest') or result.get('summary') or result.get('text') or '').strip()
    lines = [f"【市场情报】更新时间: {updated} | 状态: {status}{stale_text}"]
    if digest:
        lines.append(digest)
    else:
        lines.append('暂无可用市场情报摘要。')
    return '\n'.join(lines)


def _format_order_line(order: Any) -> str:
    if not isinstance(order, dict):
        return str(order)
    oid = order.get('order_id') or order.get('id') or '—'
    sym = order.get('symbol') or '—'
    action = _label_action(order.get('action') or order.get('side'))
    otype = order.get('order_type') or order.get('type') or ''
    state = _label_state(order.get('order_status') or order.get('state') or order.get('status'))
    if order.get('close_all') or order.get('quantity_mode') == 'all':
        qty_str = '全部'
    else:
        qty_str = _fmt_num(order.get('requested_amount') if order.get('requested_amount') is not None else order.get('amount'))
    price = order.get('price')
    trig = order.get('trigger_price')
    price_parts = []
    if price is not None:
        price_parts.append(f"限价 {_fmt_num(price)}")
    if trig is not None:
        price_parts.append(f"触发 {_fmt_num(trig)}")
    filled = order.get('filled_amount') if order.get('filled_amount') is not None else order.get('filled')
    fill_str = f" | 已成交 {_fmt_num(filled)}" if filled not in (None, 0) else ''
    return f"{sym} {action} {otype} | 状态 {state} | 数量 {qty_str}" + (f" | {' '.join(price_parts)}" if price_parts else '') + f"{fill_str} | order_id={oid}"


def _format_trade_records(result: dict) -> str:
    rtype = result.get('record_type') or 'orders'
    label = _RECORD_TYPE_LABELS.get(rtype, rtype)
    if rtype == 'order' and 'order' in result:
        return f"【{label}】" + _format_order_line(result.get('order'))
    items = result.get('items') or []
    lines = [
        f"【{label}】本页 {len(items)} 条"
        + ('（仍有更多，可用 offset 翻页）' if result.get('has_more') else '')
    ]
    if not items:
        lines.append('暂无记录。')
        return '\n'.join(lines)
    for row in items[:30]:
        if not isinstance(row, dict):
            lines.append(f'- {row}')
            continue
        if 'order_id' in row and 'kind' not in row:
            lines.append('- ' + _format_order_line(row))
            continue
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else row
        fill = payload.get('fill') if isinstance(payload.get('fill'), dict) else {}
        ts = _fmt_time(row.get('timestamp') or row.get('created_at') or row.get('observed_at'))
        kind = _label_action(row.get('kind') or fill.get('action') or row.get('action') or row.get('side'))
        sym = fill.get('symbol') or payload.get('symbol') or row.get('symbol') or ''
        qty = fill.get('amount') if fill.get('amount') is not None else payload.get('amount', row.get('amount'))
        px = fill.get('price') if fill.get('price') is not None else payload.get('price', row.get('price'))
        rpnl = fill.get('realized_pnl') if fill.get('realized_pnl') is not None else payload.get('realized_pnl', row.get('realized_pnl'))
        oid = payload.get('order_id') or row.get('order_id')
        parts = [ts, kind]
        if sym:
            parts.append(sym)
        if qty is not None:
            parts.append(f"数量 {_fmt_num(qty)}")
        if px is not None:
            parts.append(f"价格 {_fmt_num(px)}")
        if rpnl is not None:
            parts.append(f"实现盈亏 {_fmt_num(rpnl, signed=True)}")
        if payload.get('cash_after') is not None:
            parts.append(f"现金后 {_fmt_num(payload['cash_after'])}")
        if payload.get('reason'):
            parts.append(str(payload['reason'])[:60])
        if oid:
            parts.append(f"order_id={oid}")
        lines.append('- ' + ' | '.join(parts))
    if len(items) > 30:
        lines.append(f"... 本页还有 {len(items) - 30} 条未展开")
    return '\n'.join(lines)


def _format_leaderboard_entry(item: dict, metric: str) -> str:
    tag = '[Bot]' if item.get('account_type') == 'bot' else ''
    name = item.get('name') or item.get('user_id') or '—'
    rank = item.get('rank', '—')
    if metric == 'reset_count':
        return f"#{rank} {name}{tag} | 下海 {_fmt_num(item.get('reset_count'))} 次"
    return f"#{rank} {name}{tag} | 收益率 {_fmt_pct(item.get('return_pct'), signed=True)} | 盈亏 {_fmt_num(item.get('pnl'), signed=True)} USDT"


def _format_leaderboard(result: dict) -> str:
    metric = result.get('metric') or 'return_pct'
    title = '下海榜' if metric == 'reset_count' else '收益率榜'
    items = result.get('items') or []
    header = f"【模拟交易{title}】共 {_fmt_num(result.get('total'))} 名上榜"
    if result.get('unranked_count'):
        header += f"（未上榜 {result['unranked_count']}）"
    lines = [header]
    if result.get('status') == 'partial' and not items:
        lines.append(f"榜单正在分段扫描构建，请稍后再试 (next_cursor={result.get('next_cursor')})")
        return '\n'.join(lines)
    for row in items[:20]:
        if isinstance(row, dict):
            lines.append(_format_leaderboard_entry(row, metric))
    if isinstance(result.get('self'), dict):
        lines.append('你的排名: ' + _format_leaderboard_entry(result['self'], metric))
    if isinstance(result.get('bot'), dict):
        lines.append('Bot排名: ' + _format_leaderboard_entry(result['bot'], metric))
    return '\n'.join(lines)


def _format_configure_paper_account(result: dict) -> str:
    account = result.get('account') if isinstance(result.get('account'), dict) else {}
    uid = result.get('user_id') or account.get('user_id') or '—'
    cash = result.get('cash') if result.get('cash') is not None else account.get('cash')
    action = result.get('action')
    if 'created' in result:
        if result['created']:
            return f"【开户成功】账户 {uid} 已创建，初始可用资金 {_fmt_num(cash)} USDT。"
        return f"【账户已存在】账户 {uid} 已存在，原有资金与仓位保持不变。"
    if action == 'reset' or (result.get('reset_count') is not None and 'account_epoch' in result and 'preferences' not in result):
        return (
            f"【下海重置成功】账户 {uid} 已开启第 {result.get('account_epoch', '—')} 轮"
            f"（累计下海 {result.get('reset_count', '—')} 次），当前资金 {_fmt_num(cash)} USDT。"
        )
    prefs = result.get('preferences') if isinstance(result.get('preferences'), dict) else {}
    if prefs:
        desc = ', '.join(f"{k}={v}" for k, v in prefs.items())
        return f"【账户配置已更新】账户 {uid}：{desc}。"
    return f"【账户操作完成】账户 {uid}，状态 {_label_state(result.get('status') or 'completed')}。"


_FORMATTERS = {
    'get_market': _format_market,
    'get_account_state': _format_account_state,
    'get_trading_context': _format_trading_context,
    'search_symbols': _format_search_symbols,
    'get_news': _format_news,
    'get_trade_records': _format_trade_records,
    'get_paper_leaderboard': _format_leaderboard,
    'configure_paper_account': _format_configure_paper_account,
}


def render_read_tool_result(tool_name: str, result: Any) -> str:
    """Format a structured MCP tool dictionary into concise Chinese text."""
    if not isinstance(result, dict):
        return str(result) if isinstance(result, str) else _safe_json(result)
    formatter = _FORMATTERS.get(tool_name)
    if formatter is None:
        return _safe_json(result)
    try:
        return formatter(result)
    except Exception:
        return _safe_json(result)
