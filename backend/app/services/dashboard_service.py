import math
import json
import sqlite3
import time
import traceback
from datetime import datetime, timedelta

from backend.agent.agent_graph import resolve_market_timeframes
from backend.agent.memory_workflow import get_review_result
from backend.config import config as global_config
from backend.database import (
    get_short_memory,
    clean_financial_data,
    delete_daily_summary as db_delete_daily_summary,
    delete_short_memories as db_delete_short_memories,
    delete_summaries_by_symbol,
    get_active_agents,
    get_daily_summaries,
    get_latest_news_snapshot,
    get_short_memories,
    list_short_memories,
    get_db_conn,
    get_dca_daily_snapshot_history,
    get_history_pnl_stats,
    get_history_pnl_stats_for_configs,
    get_all_pricing,
    list_daily_summaries,
    get_mock_account,
    get_mock_equity_history,
    get_paginated_orders,
    get_paginated_summaries,
    get_summary_count,
    save_dca_daily_snapshot,
    save_trade_history,
    update_daily_summary as db_update_daily_summary,
    update_short_memory as db_update_short_memory,
    update_order_fill_status,
    upsert_spot_order_fill,
)
from backend.utils.market_data import MarketTool
from backend.utils.run_schedule import schedule_preview, normalize_dca_freq
from backend.utils.spot_portfolio import get_config_symbols

from backend.app.services.common import TZ_CN, get_scheduler_status, get_symbol_specific_status, list_symbols, logger


DCA_STATS_CACHE = {}
DCA_STATS_CACHE_TTL = 300
DASHBOARD_VISIBLE_MODES = {"REAL", "STRATEGY", "SPOT_DCA"}


def _scheduler_timestamp_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return TZ_CN.localize(parsed).isoformat()
    except (TypeError, ValueError):
        return str(value)


def _equity_series_metadata(points: list[dict]) -> dict:
    if not points:
        return {
            "data_state": "no_data",
            "point_count": 0,
            "first_date": None,
            "last_date": None,
            "latest_equity": None,
            "return_pct": None,
        }
    first = float(points[0].get("equity") or 0)
    latest = float(points[-1].get("equity") or 0)
    return {
        "data_state": "ready",
        "point_count": len(points),
        "first_date": points[0].get("date"),
        "last_date": points[-1].get("date"),
        "latest_equity": latest,
        "return_pct": ((latest - first) / first * 100) if first else None,
    }


def _order_action_label(order: dict) -> str:
    side = str(order.get("side") or "").lower()
    status = str(order.get("status") or "").upper()
    trade_mode = str(order.get("trade_mode") or "").upper()
    # 明确撤单记录（side 包含 cancel）：优先判断方向
    if "cancel" in side:
        if "buy" in side or "long" in side:
            return "撤多单"
        if "sell" in side or "short" in side:
            return "撤空单"
        return "撤单"
    if "close" in side:
        return "平多" if "long" in side or "buy" in side else "平空"
    if "sell" in side or "short" in side:
        if status == "CANCELLED":
            return "开空(撤)"
        return "开空" if status != "CLOSED" else "平多"
    if "buy" in side or "long" in side:
        if status == "CANCELLED":
            return "开多(撤)" if trade_mode != "SPOT_DCA" else "现货买入(撤)"
        return "开多" if trade_mode != "SPOT_DCA" else "现货买入"
    # 纯 status=CANCELLED 的兜底（无方向信息）
    if status == "CANCELLED":
        return "撤单"
    return side.upper() or status or "记录"


def _event_label(event_type: str, trade_mode: str, side: str, status: str) -> str:
    event = str(event_type or "ORDER_CREATED").upper()
    mode = str(trade_mode or "").upper()
    side_up = str(side or "").upper()
    status_up = str(status or "").upper()
    labels = {
        "ENTRY_FILLED": "入场成交",
        "TP_HIT": "止盈成交",
        "SL_HIT": "止损成交",
        "AUTO_CLOSE": "自动平仓",
        "MANUAL_CLOSE": "主动平仓",
        "CANCELLED": "撤单",
        "TRADE_FILL": "交易所成交",
    }
    if event in labels:
        return labels[event]
    if status_up == "PARTIAL":
        return "部分成交"
    if status_up == "FILLED":
        return "成交"
    if status_up == "CANCELLED" or "CANCEL" in side_up:
        return "撤单"
    if "CLOSE" in side_up:
        return "平仓"
    if mode == "SPOT_DCA":
        return "现货买入挂单"
    if "SELL" in side_up or "SHORT" in side_up:
        return "开空挂单"
    if "BUY" in side_up or "LONG" in side_up:
        return "开多挂单"
    return status_up or "记录"


def _direction_label(side: str) -> str:
    side_up = str(side or "").upper()
    if "CLOSE" in side_up:
        if "SHORT" in side_up:
            return "平空"
        if "LONG" in side_up or "BUY" in side_up:
            return "平多"
        return "平仓"
    if "BUY" in side_up or "LONG" in side_up:
        return "多"
    if "SELL" in side_up or "SHORT" in side_up:
        return "空"
    return side or "-"


def _normalize_order_record(order: dict) -> dict:
    payload = dict(order)
    event_type = str(payload.get("event_type") or "ORDER_CREATED").upper()
    trade_mode = str(payload.get("trade_mode") or "").upper()
    status = str(payload.get("status") or "").upper()
    payload["activity_type"] = payload.get("activity_type") or "order"
    payload["event_type"] = event_type
    payload["event_label"] = _event_label(event_type, trade_mode, payload.get("side"), status)
    payload["action_label"] = payload["event_label"]
    payload["direction_label"] = _direction_label(payload.get("side"))
    payload["is_auto"] = bool(payload.get("is_auto"))
    payload["copy_fields"] = [
        value
        for value in (payload.get("entry_price"), payload.get("amount"), payload.get("take_profit"), payload.get("stop_loss"))
        if value not in (None, "")
    ]
    if str(payload.get("trade_mode") or "").upper() == "SPOT_DCA":
        payload["strategy_note"] = "监控自动成交"
    elif payload.get("take_profit") or payload.get("stop_loss"):
        payload["strategy_note"] = "止盈止损"
    else:
        payload["strategy_note"] = ""
    if payload.get("is_auto"):
        payload["strategy_note"] = "自动检测"
    elif trade_mode == "SPOT_DCA":
        payload["strategy_note"] = "现货定投"
    elif payload.get("take_profit") or payload.get("stop_loss"):
        payload["strategy_note"] = "止盈止损"
    return payload


def _normalize_trade_activity_record(row: dict) -> dict:
    payload = dict(row)
    side = str(payload.get("side") or "").lower()
    realized_pnl = float(payload.get("realized_pnl") or 0)
    if abs(realized_pnl) > 1e-12:
        action_label = "Close fill"
    elif "buy" in side or "long" in side:
        action_label = "Buy fill"
    elif "sell" in side or "short" in side:
        action_label = "Sell fill"
    else:
        action_label = "Fill"

    details = []
    if payload.get("order_id"):
        details.append(f"order={payload.get('order_id')}")
    if payload.get("trade_id"):
        details.append(f"trade={payload.get('trade_id')}")
    if payload.get("fee") not in (None, ""):
        currency = payload.get("fee_currency") or ""
        details.append(f"fee={payload.get('fee')} {currency}".strip())

    payload.update(
        {
            "activity_type": "trade",
            "event_type": "TRADE_FILL",
            "event_label": action_label,
            "action_label": action_label,
            "direction_label": _direction_label(payload.get("side")),
            "entry_price": payload.get("price"),
            "status": "FILLED",
            "strategy_note": f"PnL {realized_pnl:.2f}" if abs(realized_pnl) > 1e-12 else "Exchange fill",
            "reason": "; ".join(details),
            "is_auto": False,
            "copy_fields": [value for value in (payload.get("price"), payload.get("amount")) if value not in (None, "")],
        }
    )
    return payload


def _get_recent_order_activity(config_id: str, limit: int = 20) -> list[dict]:
    with get_db_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM (
                SELECT
                    id AS sort_id,
                    'order' AS activity_type,
                    id,
                    order_id,
                    NULL AS trade_id,
                    timestamp,
                    symbol,
                    agent_name,
                    config_id,
                    trade_mode,
                    side,
                    entry_price,
                    amount,
                    take_profit,
                    stop_loss,
                    reason,
                    status,
                    NULL AS price,
                    NULL AS cost,
                    NULL AS fee,
                    NULL AS fee_currency,
                    realized_pnl,
                    event_type,
                    parent_order_id,
                    is_auto
                FROM orders
                WHERE config_id = ?

                UNION ALL

                SELECT
                    rowid AS sort_id,
                    'trade' AS activity_type,
                    NULL AS id,
                    order_id,
                    trade_id,
                    timestamp,
                    symbol,
                    NULL AS agent_name,
                    config_id,
                    COALESCE((SELECT o.trade_mode FROM orders o
                        WHERE o.config_id=trade_history.config_id AND o.symbol=trade_history.symbol
                          AND o.order_id=trade_history.order_id
                        ORDER BY o.id DESC LIMIT 1), 'REAL') AS trade_mode,
                    side,
                    price AS entry_price,
                    amount,
                    NULL AS take_profit,
                    NULL AS stop_loss,
                    NULL AS reason,
                    'FILLED' AS status,
                    price,
                    cost,
                    fee,
                    fee_currency,
                    realized_pnl,
                    'TRADE_FILL' AS event_type,
                    order_id AS parent_order_id,
                    0 AS is_auto
                FROM trade_history
                WHERE config_id = ?
            )
            ORDER BY timestamp DESC, sort_id DESC
            LIMIT ?
            """,
            (config_id, config_id, int(limit or 20)),
        ).fetchall()
    return [dict(row) for row in rows]


def _collapse_recent_order_activity(rows: list[dict]) -> list[dict]:
    latest_cancel_by_order: dict[tuple, dict] = {}
    latest_cancelled_open_by_order: dict[tuple, dict] = {}

    for row in rows:
        if row.get("activity_type") != "order":
            continue
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        order_id = (row.get('config_id'), row.get('symbol'), order_id)
        side = str(row.get("side") or "").lower()
        status = str(row.get("status") or "").upper()
        if "cancel" in side and order_id not in latest_cancel_by_order:
            latest_cancel_by_order[order_id] = row
        elif "cancel" not in side and status == "CANCELLED" and order_id not in latest_cancelled_open_by_order:
            latest_cancelled_open_by_order[order_id] = row

    collapsed = []
    emitted_cancel_order_ids: set[tuple] = set()
    for row in rows:
        if row.get("activity_type") != "order":
            collapsed.append(row)
            continue

        order_id = str(row.get("order_id") or "").strip()
        order_id = (row.get('config_id'), row.get('symbol'), order_id) if order_id else None
        side = str(row.get("side") or "").lower()
        status = str(row.get("status") or "").upper()

        if order_id and "cancel" in side:
            if order_id in emitted_cancel_order_ids:
                continue
            emitted_cancel_order_ids.add(order_id)

            merged = dict(row)
            source_row = latest_cancelled_open_by_order.get(order_id)
            if source_row:
                for field in ("entry_price", "amount", "take_profit", "stop_loss"):
                    if merged.get(field) in (None, "", 0, 0.0) and source_row.get(field) not in (None, ""):
                        merged[field] = source_row.get(field)
                for field in ("symbol", "agent_name", "config_id", "trade_mode"):
                    if not merged.get(field) and source_row.get(field):
                        merged[field] = source_row.get(field)
            collapsed.append(merged)
            continue

        if order_id and status == "CANCELLED" and "cancel" not in side and order_id in latest_cancel_by_order:
            continue

        collapsed.append(row)

    return collapsed


def build_symbol_overview_metrics(symbol: str, agent_summaries: list[dict]) -> dict:
    config_ids = [item.get("config_id") for item in agent_summaries if item.get("config_id")]
    with get_db_conn() as conn:
        placeholders = ",".join(["?"] * len(config_ids))
        token_total = 0
        cost_total = 0.0
        if config_ids:
            rows = conn.execute(
                f"""
                SELECT model,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COALESCE(SUM(total_tokens), 0) AS total
                FROM token_usage
                WHERE config_id IN ({placeholders})
                GROUP BY model
                """,
                tuple(config_ids),
            ).fetchall()
            pricing = get_all_pricing()
            for row in rows:
                token_total += int(row["total"] or 0)
                price = pricing.get(row["model"], {"input_price_per_m": 0, "output_price_per_m": 0})
                cost_total += (float(row["prompt"] or 0) / 1_000_000 * price.get("input_price_per_m", 0)) + (
                    float(row["completion"] or 0) / 1_000_000 * price.get("output_price_per_m", 0)
                )

    pnl_stats = get_history_pnl_stats_for_configs(symbol, config_ids)
    win_rate = pnl_stats.get("win_rate")
    return {
        "agent_count": len(agent_summaries),
        "win_rate": round(float(win_rate), 2) if win_rate is not None else None,
        "total_pnl": round(float(pnl_stats.get("total_pnl", 0) or 0), 4),
        "total_trades": int(pnl_stats.get("total_trades", 0) or 0),
        "total_tokens": int(token_total or 0),
        "total_cost": round(cost_total, 4),
    }


def build_config_compare_rows(symbol: str, agent_summaries: list[dict]) -> list[dict]:
    rows = []
    for agent in agent_summaries:
        config_id = agent.get("config_id")
        if not config_id:
            continue
        pnl = get_history_pnl_stats(symbol, config_id=config_id)
        win_rate = pnl.get("win_rate")
        with get_db_conn() as conn:
            orders = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN LOWER(side) LIKE '%buy%' OR LOWER(side) LIKE '%long%' THEN 1 ELSE 0 END) AS long_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%sell%' OR LOWER(side) LIKE '%short%' THEN 1 ELSE 0 END) AS short_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%close%' OR status = 'CLOSED' THEN 1 ELSE 0 END) AS close_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%cancel%' OR status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancel_count
                FROM orders
                WHERE config_id = ?
                """,
                (config_id,),
            ).fetchone()
        long_count = int(orders["long_count"] or 0)
        short_count = int(orders["short_count"] or 0)
        rows.append(
            {
                "config_id": config_id,
                "display_name": agent.get("display_name") or config_id,
                "mode": agent.get("mode"),
                "model": agent.get("model"),
                "long_count": long_count,
                "short_count": short_count,
                "long_short_ratio": round(long_count / short_count, 2) if short_count else (long_count if long_count else 0),
                "close_count": int(orders["close_count"] or 0),
                "cancel_count": int(orders["cancel_count"] or 0),
                "total_orders": int(orders["total_orders"] or 0),
                "win_rate": round(float(win_rate), 2) if win_rate is not None else None,
                "total_pnl": round(float(pnl.get("total_pnl", 0) or 0), 4),
            }
        )
    return rows


def calculate_next_run(config, latest_summary=None):
    return schedule_preview(config, datetime.now(TZ_CN), scheduler_enabled=get_scheduler_status())["next_run"]


def calculate_dca_stats(config_id, force_sync=False, symbol=None):
    """Return task totals in quote currency and keep base-asset quantities separate."""
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        return None
    symbols = get_config_symbols(cfg)
    if symbol is not None:
        symbols = [symbol] if symbol in symbols else []
    if not symbols:
        return None
    results = [_calculate_dca_symbol_stats(config_id, cfg, item, force_sync) for item in symbols]
    available = [item for item in results if item is not None]
    if len(symbols) == 1:
        return available[0] if available else None
    if not available:
        return None
    totals = {key: (round(sum(float(item[key]) for item in available), 4)
                   if len(available) == len(symbols) and all(item.get(key) is not None for item in available) else None)
              for key in ('total_invested', 'market_value', 'unrealized_pnl', 'recorded_buy_cost')}
    invested = totals['total_invested']
    complete = len(available) == len(symbols) and all(item.get('sync_status') == 'synced' for item in available)
    return {
        **totals, 'is_portfolio': True, 'symbols': symbols, 'by_symbol': available,
        'quote_asset': symbols[0].split('/')[1],
        'total_qty': None, 'avg_cost': None, 'current_price': None, 'actual_balance': None,
        'manual_avg_cost': None, 'manual_qty': None, 'recorded_buy_qty': None,
        'return_pct': (round(totals['unrealized_pnl'] / invested * 100, 2) if invested else 0)
                      if invested is not None and totals['unrealized_pnl'] is not None else None,
        'buy_count': sum(item['buy_count'] for item in available),
        'pending_orders': sum(item['pending_orders'] for item in available),
        'dca_amount_per': cfg.get('dca_amount', cfg.get('dca_budget', 0)),
        'has_legacy': any(item['has_legacy'] for item in available), 'stats_source': 'portfolio',
        'first_buy': min((item['first_buy'] for item in available if item['first_buy']), default=None),
        'last_buy': max((item['last_buy'] for item in available if item['last_buy']), default=None),
        'sync_status': 'synced' if complete else 'partial',
        'errors': [error for item in available for error in item.get('errors', [])],
        'missing_symbols': [item for item, result in zip(symbols, results) if result is None],
        'last_sync': min(item['last_sync'] for item in available),
    }


def _calculate_dca_symbol_stats(config_id, cfg, symbol, force_sync=False):
    try:
        sync_errors = []
        cache_key = f'{config_id}:{symbol}'
        now_ts = time.time()

        cache_signature = (
            tuple(get_config_symbols(cfg)), symbol, cfg.get('dca_amount'), cfg.get('exchange_profile_id'),
            cfg.get("initial_cost"),
            cfg.get("initial_qty"),
            cfg.get("manual_avg_cost"),
        )
        if not force_sync:
            cached = DCA_STATS_CACHE.get(cache_key)
            if cached and cached.get("signature") == cache_signature and now_ts - cached["timestamp"] < DCA_STATS_CACHE_TTL:
                return cached["data"]

        if not symbol:
            return None

        base_asset = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
        # Legacy manual balances describe the primary symbol only.
        is_primary = symbol == cfg.get('symbol')
        initial_qty = float(cfg.get("initial_qty", 0) or 0) if is_primary else 0
        manual_avg_cost = float(cfg.get("manual_avg_cost", 0) or 0) if is_primary else 0
        if manual_avg_cost > 0 and initial_qty > 0:
            initial_cost = manual_avg_cost * initial_qty
        else:
            initial_cost = float(cfg.get("initial_cost", 0) or 0) if is_primary else 0
            manual_avg_cost = (initial_cost / initial_qty) if initial_qty > 0 and initial_cost > 0 else 0
        mt = MarketTool(config_id=config_id)
        from backend.utils.spot_execution import reconcile_spot_reservations
        reconcile_spot_reservations(config_id, cfg, lambda: mt)

        try:
            with get_db_conn() as conn:
                open_rows = conn.execute(
                    """
                    SELECT DISTINCT order_id
                    FROM orders
                    WHERE config_id = ?
                      AND symbol = ?
                      AND trade_mode = 'SPOT_DCA'
                      AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'
                      AND UPPER(side) IN ('BUY','BUY_LIMIT')
                      AND status IN ('OPEN', 'PARTIAL')
                    ORDER BY id DESC
                    LIMIT 100
                    """,
                    (config_id, symbol),
                ).fetchall()

            for row in open_rows:
                order_id = str(row["order_id"])
                try:
                    od = mt.exchange.fetch_order(order_id, symbol)
                    exch_status = str(od.get("status", "") or "").lower()
                    if exch_status not in {'open', 'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'} or od.get('filled') is None:
                        raise ValueError('交易所订单状态或成交数量尚未核验')
                    filled_qty = float(od.get("filled", 0) or 0)
                    filled_cost = float(od.get("cost", 0) or 0)
                    avg_price = float(od.get("average", 0) or 0)

                    if filled_qty > 0 and filled_cost <= 0 and avg_price > 0:
                        filled_cost = filled_qty * avg_price
                    if avg_price <= 0 and filled_qty > 0 and filled_cost > 0:
                        avg_price = filled_cost / filled_qty
                    if any(not math.isfinite(value) or value < 0 for value in (filled_qty, filled_cost, avg_price)) or (filled_qty > 0 and filled_cost <= 0):
                        raise ValueError('交易所成交成本无效')

                    fill_ts = od.get("lastTradeTimestamp") or od.get("timestamp")
                    filled_at = None
                    if fill_ts:
                        filled_at = datetime.fromtimestamp(float(fill_ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")

                    local_status = "OPEN"
                    if exch_status in ("closed", "filled"):
                        local_status = "FILLED"
                    elif exch_status in ("canceled", "cancelled", "expired", "rejected"):
                        local_status = "CANCELLED"
                    elif filled_qty > 0:
                        local_status = "PARTIAL"

                    with get_db_conn() as conn:
                        conn.execute('''UPDATE orders SET status=?, filled_amount=?, filled_cost=?,
                            avg_fill_price=?, filled_at=? WHERE order_id=? AND config_id=? AND symbol=?
                            AND trade_mode='SPOT_DCA' AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''',
                            (local_status, filled_qty, filled_cost, avg_price, filled_at, order_id, config_id, symbol))
                        conn.commit()
                    upsert_spot_order_fill(order_id, config_id, symbol, local_status, filled_qty, filled_cost, avg_price, filled_at)
                except Exception as one_error:
                    sync_errors.append(f'{symbol} 订单 {order_id} 同步失败')
                    logger.debug(f"Skip spot order sync: {order_id} => {one_error}")
        except Exception as sync_error:
            sync_errors.append(f'{symbol} 委托同步失败')
            logger.warning(f"DCA order sync failed for {config_id}: {sync_error}")

        try:
            trades = mt.exchange.fetch_my_trades(symbol, limit=1000)
            if trades:
                with get_db_conn() as conn:
                    owned_ids = {str(row[0]) for row in conn.execute('''SELECT order_id FROM orders
                        WHERE config_id=? AND symbol=? AND trade_mode='SPOT_DCA'
                        AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''', (config_id, symbol))}
                save_trade_history([trade for trade in trades if str(trade.get('order') or trade.get('order_id') or '') in owned_ids], config_id=config_id)
        except Exception as trade_error:
            sync_errors.append(f'{symbol} 成交历史同步失败')
            logger.warning(f"Fetch my_trades failed for {symbol}: {trade_error}")

        with get_db_conn() as conn:
            # Order fill totals survive exchange trade-history pagination. Use
            # the larger complete evidence per order, never add two copies of
            # the same fill and never lose older orders outside fetch_my_trades.
            rows = conn.execute('''SELECT o.*,f.filled_qty AS sync_qty,f.filled_cost AS sync_cost,
                    f.avg_fill_price AS sync_price,f.status AS sync_status,
                    t.traded_qty,t.traded_cost,t.first_buy,t.last_buy
                FROM orders o LEFT JOIN spot_order_fills f
                  ON f.config_id=o.config_id AND f.symbol=o.symbol AND f.order_id=o.order_id
                LEFT JOIN (SELECT order_id,SUM(amount) AS traded_qty,SUM(cost) AS traded_cost,
                    MIN(timestamp) AS first_buy,MAX(timestamp) AS last_buy FROM trade_history
                    WHERE symbol=? AND (config_id=? OR config_id IS NULL OR config_id='')
                      AND LOWER(side)='buy' GROUP BY order_id) t ON t.order_id=o.order_id
                WHERE o.config_id=? AND o.symbol=? AND o.trade_mode='SPOT_DCA'
                  AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
                  AND UPPER(o.side) IN ('BUY','BUY_LIMIT') ORDER BY o.id DESC''',
                (symbol, config_id, config_id, symbol)).fetchall()
            traded_cost = traded_qty = 0.0
            buy_count = 0
            buy_dates = []
            seen = set()
            for row in rows:
                if row['order_id'] in seen:
                    continue
                seen.add(row['order_id'])
                synced = row['sync_status'] is not None
                qty = float((row['sync_qty'] if synced else row['filled_amount']) or 0)
                cost = float((row['sync_cost'] if synced else row['filled_cost']) or 0)
                avg = float((row['sync_price'] if synced else row['avg_fill_price']) or row['entry_price'] or 0)
                cost = max(cost, qty * avg)
                history_qty, history_cost = float(row['traded_qty'] or 0), float(row['traded_cost'] or 0)
                if history_qty >= qty:
                    qty, cost = history_qty, max(cost, history_cost)
                if any(not math.isfinite(value) or value < 0 for value in (qty, cost)):
                    raise ValueError('现货历史成交数量或成本无效')
                traded_qty += qty
                traded_cost += cost
                if qty > 0:
                    buy_count += 1
                    buy_dates.extend(value for value in (row['first_buy'], row['last_buy'], row['filled_at'] or row['timestamp']) if value)
            agg = {'first_buy': min(buy_dates) if buy_dates else None, 'last_buy': max(buy_dates) if buy_dates else None}

            pending = conn.execute(
                """
                SELECT COUNT(DISTINCT order_id) AS pending_count
                FROM orders
                WHERE config_id = ?
                  AND symbol = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'
                  AND UPPER(side) IN ('BUY','BUY_LIMIT')
                  AND status IN ('OPEN', 'PARTIAL')
                """,
                (config_id, symbol),
            ).fetchone()

        current_qty = None
        try:
            balances = mt.exchange.fetch_balance()
            base_balance = balances.get(base_asset) or balances.get(base_asset.lower()) or {}
            current_qty = float(base_balance.get('total') or (balances.get('total') or {}).get(base_asset) or 0)
            if not math.isfinite(current_qty) or current_qty < 0:
                raise ValueError('交易所持仓余额无效')
        except Exception:
            current_qty = None
            sync_errors.append(f'{symbol} 账户余额同步失败')

        if initial_qty > 0:
            final_qty = initial_qty + traded_qty
            final_invested = initial_cost + traded_cost
            stats_source = "manual_and_trades" if traded_qty else "manual"
        else:
            final_qty = traded_qty
            final_invested = traded_cost
            stats_source = "trades"
        avg_cost = (final_invested / final_qty) if final_qty > 0 else 0
        current_price = None
        try:
            ticker = mt.exchange.fetch_ticker(symbol)
            current_price = float(ticker.get("last") or ticker.get("close") or 0)
            if not math.isfinite(current_price) or current_price <= 0:
                raise ValueError('无有效现货价格')
        except Exception as ticker_error:
            current_price = None
            sync_errors.append(f'{symbol} 价格同步失败，市值及收益暂不可用')
            logger.debug(f"Fetch ticker failed for DCA stats {config_id}: {ticker_error}")
        market_value = final_qty * current_price if current_price is not None else None
        unrealized_pnl = market_value - final_invested if market_value is not None else None
        return_pct = ((unrealized_pnl / final_invested * 100) if final_invested > 0 else 0) if unrealized_pnl is not None else None

        result = {
            'symbol': symbol, 'base_asset': base_asset, 'quote_asset': symbol.split('/')[1], 'is_portfolio': False,
            "buy_count": buy_count,
            "total_invested": round(final_invested, 2),
            "total_qty": round(final_qty, 6),
            "avg_cost": round(avg_cost, 4),
            "manual_avg_cost": round(manual_avg_cost, 4),
            "manual_qty": round(initial_qty, 6),
            "recorded_buy_qty": round(traded_qty, 6),
            "recorded_buy_cost": round(traded_cost, 2),
            "stats_source": stats_source,
            "current_price": round(current_price, 4) if current_price is not None else None,
            "market_value": round(market_value, 2) if market_value is not None else None,
            "unrealized_pnl": round(unrealized_pnl, 4) if unrealized_pnl is not None else None,
            "return_pct": round(return_pct, 2) if return_pct is not None else None,
            "dca_amount_per": cfg.get("dca_amount", cfg.get("dca_budget", 0)),
            "has_legacy": initial_qty > 0,
            "first_buy": agg["first_buy"],
            "last_buy": agg["last_buy"],
            "actual_balance": round(current_qty, 6) if current_qty is not None else None,
            "pending_orders": int(pending["pending_count"] or 0),
            "sync_status": "partial" if sync_errors else "synced",
            "errors": sync_errors,
            "last_sync": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        }

        if not sync_errors:
            save_dca_daily_snapshot(config_id, symbol, result)
        DCA_STATS_CACHE[cache_key] = {"timestamp": now_ts, "signature": cache_signature, "data": result}
        return result
    except Exception as exc:
        logger.error(f"Error calculating CCXT DCA stats for {config_id}: {exc}\n{traceback.format_exc()}")
        return None


def get_dashboard_data(symbol, page=1, per_page=10, *, config_id=None):
    try:
        with get_db_conn() as conn:
            configs = global_config.get_all_symbol_configs()
            symbol_configs = [
                conf
                for conf in configs
                if (not symbol or symbol in get_config_symbols(conf))
                and (config_id is None or conf.get("config_id") == config_id)
                and str(conf.get("mode", "STRATEGY")).upper() in DASHBOARD_VISIBLE_MODES
            ]

            agent_summaries = []
            for config in symbol_configs:
                config_id = config["config_id"]
                latest_summary_row = conn.execute(
                    "SELECT * FROM summaries WHERE config_id = ? ORDER BY id DESC LIMIT 1",
                    (config_id,),
                ).fetchone()
                try:
                    latest_execution_row = conn.execute(
                        """
                        SELECT status, phase, progress_message, reasoning_content, reasoning_tokens,
                               tool_calls_json, scheduled_at, started_at, finished_at,
                               created_at, updated_at, error
                        FROM scheduler_runs
                        WHERE config_id = ? AND job_type = 'agent'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (config_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    latest_execution_row = None

                mode = str(config.get("mode", "STRATEGY")).upper()
                model_name = config.get("model", "Unknown")
                enabled = config.get("enabled", True)

                if latest_summary_row:
                    summary_dict = dict(latest_summary_row)
                else:
                    summary_dict = {
                        "config_id": config_id,
                        "agent_name": model_name,
                        "symbol": config.get("symbol"),
                        "content": "",
                        "strategy_logic": "",
                        "timestamp": "",
                        "agent_type": None,
                        "id": -1,
                    }

                summary_dict["model"] = model_name
                summary_dict["mode"] = mode
                summary_dict["enabled"] = enabled
                summary_dict['symbols'] = get_config_symbols(config)
                summary_dict['is_portfolio'] = len(summary_dict['symbols']) > 1
                now = datetime.now(TZ_CN)
                dca_executed = False
                if mode == "SPOT_DCA":
                    from backend.app.core.scheduler import dca_was_dispatched
                    dca_executed = dca_was_dispatched(config_id, now, config=config)
                schedule = schedule_preview(config, now, scheduler_enabled=get_scheduler_status(), dca_executed=dca_executed)
                summary_dict["schedule"] = schedule
                summary_dict["next_run"] = schedule["next_run"]
                summary_dict["next_run_at"] = schedule["next_run_at"]
                summary_dict["freq"] = schedule["frequency"]

                summary_dict["leverage"] = global_config.get_leverage(config_id)
                summary_dict["market_timeframes"] = resolve_market_timeframes(config)
                summary_dict["title"] = config.get("title") or config_id
                summary_dict["display_name"] = f"{summary_dict['title']} ({mode})"
                orders, total = get_paginated_orders(config_id, page=1, per_page=10)
                summary_dict["all_orders"] = orders
                summary_dict["order_total"] = total
                summary_dict["order_page"] = 1
                from backend.utils.decision_record import read_decision_record
                summary_dict['decision'] = read_decision_record(summary_dict)
                if summary_dict['decision'] and summary_dict['decision'].get('raw_analysis') is not None:
                    summary_dict['content'] = summary_dict['decision']['raw_analysis']
                summary_dict.pop('report_json', None)
                summary_dict.pop('decision_json', None)
                summary_dict['memory_update'] = None
                if summary_dict.get('run_id'):
                    try:
                        memory_job = conn.execute(
                            'SELECT status,attempts,error FROM memory_update_jobs WHERE config_id=? AND run_id=?',
                            (config_id, summary_dict['run_id']),
                        ).fetchone()
                        summary_dict['memory_update'] = dict(memory_job) if memory_job else None
                    except sqlite3.OperationalError:
                        pass
                if latest_execution_row:
                    execution = dict(latest_execution_row)
                    try:
                        execution["tool_calls"] = json.loads(execution.pop("tool_calls_json") or "[]")
                    except (TypeError, json.JSONDecodeError):
                        execution["tool_calls"] = []
                    execution["started_at_iso"] = _scheduler_timestamp_iso(
                        execution.get("started_at") or execution.get("created_at")
                    )
                    summary_dict["execution"] = execution
                else:
                    summary_dict["execution"] = None
                agent_summaries.append(summary_dict)

        return agent_summaries
    except Exception as exc:
        logger.error(f"Failed to load dashboard data for {symbol}: {exc}")
        return []


def build_dashboard_overview(symbol: str | None = None, page: int = 1):
    symbols = list_symbols()
    current_symbol = symbol or None
    agent_summaries = get_dashboard_data(current_symbol, page)
    symbol_mode, symbol_freq, symbol_enabled = get_symbol_specific_status(current_symbol) if current_symbol else ("ALL", None, True)
    from backend.app.services.news_service import get_latest_global_snapshot
    run_counts = {"finished": 0, "failed": 0, "running": 0}
    with get_db_conn() as conn:
        try:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM scheduler_runs WHERE job_type='agent' AND created_at >= ? GROUP BY status",
                ((datetime.now(TZ_CN) - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S'),),
            ):
                status = str(row['status']).lower()
                if status in run_counts:
                    run_counts[status] = row['n']
        except sqlite3.OperationalError:
            pass
    return {
        "generated_at": datetime.now(TZ_CN).isoformat(),
        "timezone": str(TZ_CN),
        "symbols": symbols,
        "current_symbol": current_symbol,
        "agent_summaries": agent_summaries,
        "overview_metrics": {"agent_count": len(agent_summaries), "enabled_count": sum(bool(item.get('enabled')) for item in agent_summaries), "runs_24h": run_counts},
        "compare_rows": build_config_compare_rows(current_symbol, agent_summaries) if current_symbol else [],
        "symbol_mode": symbol_mode,
        "symbol_freq": symbol_freq,
        "symbol_enabled": symbol_enabled,
        "scheduler_enabled": get_scheduler_status(),
        "market_timeframes": list(getattr(global_config, "market_timeframes", None) or []),
        "news_snapshot": get_latest_global_snapshot(),
    }


def build_history_payload(symbol: str, agent_filter: str = "ALL", page: int = 1, per_page: int = 20, compare_ids: list[str] | None = None):
    compare_ids = compare_ids or []
    symbol_configs = [
        cfg for cfg in global_config.get_all_symbol_configs() if symbol in get_config_symbols(cfg) and cfg.get("config_id")
    ]
    config_map = {cfg.get("config_id"): cfg for cfg in symbol_configs}
    if agent_filter != "ALL" and agent_filter not in config_map:
        agent_filter = "ALL"

    portfolio_ids = [cfg['config_id'] for cfg in symbol_configs
                     if str(cfg.get('mode') or '').upper() == 'SPOT_DCA' and len(get_config_symbols(cfg)) > 1]
    if portfolio_ids:
        from backend.app.services.portfolio_history_service import portfolio_decision_page
        summaries, total_count, history_agents = portfolio_decision_page(symbol, portfolio_ids, agent_filter, page, per_page)
    else:
        summaries = get_paginated_summaries(symbol, page, per_page, config_id=agent_filter)
        total_count = get_summary_count(symbol, config_id=agent_filter)
        history_agents = get_active_agents(symbol)
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    active_agents = [aid for aid in history_agents if aid in config_map]
    if agent_filter == "ALL":
        pnl_stats = get_history_pnl_stats_for_configs(symbol, config_map.keys())
    else:
        pnl_stats = get_history_pnl_stats(symbol, config_id=agent_filter)

    mock_config_id = agent_filter if agent_filter != "ALL" else ""
    agent_mode = "STRATEGY"
    if agent_filter != "ALL":
        cfg = global_config.get_config_by_id(agent_filter)
        if cfg:
            agent_mode = str(cfg.get("mode", "STRATEGY")).upper()

    mock_acc = None
    mock_chart_data = []
    if agent_mode == "STRATEGY" or agent_filter == "ALL":
        mock_acc = get_mock_account(mock_config_id, symbol)
        mock_chart_data = get_mock_equity_history(
            mock_config_id,
            symbol=symbol,
            include_fallback=agent_filter != "ALL",
        )

    real_chart_data = []
    real_balance = None
    if agent_mode == "REAL" and agent_filter != "ALL":
        try:
            with get_db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT day, total_equity FROM (
                        SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                               row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                        FROM balance_history WHERE config_id = ? AND symbol = ? AND total_equity > 0
                    ) WHERE rn = 1 ORDER BY day ASC
                    """,
                    (agent_filter, symbol),
                ).fetchall()
                real_chart_data = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
        except Exception as exc:
            logger.warning(f"Failed to load real chart data: {exc}")

        try:
            mt = MarketTool(config_id=agent_filter)
            bal = mt.exchange.fetch_balance()
            real_balance = float(bal.get("USDT", {}).get("total", 0) or bal.get("total", {}).get("USDT", 0) or 0)
        except Exception as exc:
            logger.warning(f"Failed to fetch live balance for REAL mode: {exc}")

    dca_stats = None
    dca_chart_data = []
    if agent_mode == "SPOT_DCA" and agent_filter != "ALL":
        dca_stats = calculate_dca_stats(agent_filter, symbol=symbol)
        dca_chart_data = get_dca_daily_snapshot_history(agent_filter, days=30, symbol=symbol)

    history_compare_series = []
    selected_compare_ids = [cid for cid in compare_ids if cid in config_map]
    if agent_filter == "ALL" or selected_compare_ids:
        target_cfgs = [config_map[cid] for cid in selected_compare_ids] if selected_compare_ids else symbol_configs
        with get_db_conn() as conn:
            for cfg in target_cfgs:
                config_id = cfg.get("config_id")
                mode = str(cfg.get("mode", "STRATEGY")).upper()
                if not config_id:
                    continue

                data_source = None
                if mode == "REAL":
                    rows = conn.execute(
                        """
                        SELECT day, total_equity FROM (
                            SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                   row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                            FROM balance_history WHERE config_id = ? AND symbol = ? AND total_equity > 0
                        ) WHERE rn = 1 ORDER BY day ASC
                        """,
                        (config_id, symbol),
                    ).fetchall()
                    points = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
                    data_source = {
                        "table": "balance_history",
                        "field": "total_equity",
                        "scope": "config_id",
                        "config_id": config_id,
                        "symbol": symbol,
                        "label": "balance_history.total_equity",
                        "display_label": "实盘账户权益快照",
                        "kind": "real_equity",
                    }
                elif mode == "STRATEGY":
                    strategy_points = get_mock_equity_history(
                        config_id,
                        symbol=symbol,
                        include_fallback=False,
                    )
                    points = [
                        {"date": p.get("date"), "equity": p.get("equity", p.get("balance"))}
                        for p in strategy_points
                        if p.get("date") is not None and (p.get("equity") is not None or p.get("balance") is not None)
                    ]
                    data_source = {
                        "table": "mock_balance_history",
                        "field": "total_equity",
                        "fallback_field": "balance",
                        "scope": "config_id",
                        "config_id": config_id,
                        "symbol": symbol,
                        "label": "mock_balance_history.total_equity",
                        "display_label": "策略模拟权益快照",
                        "kind": "strategy_equity",
                    }
                elif mode == "SPOT_DCA":
                    rows = conn.execute(
                        """
                        SELECT snapshot_date AS day, total_invested AS equity
                        FROM dca_daily_snapshots
                        WHERE config_id = ? AND symbol = ? AND total_invested > 0
                        ORDER BY snapshot_date ASC
                        LIMIT 30
                        """,
                        (config_id, symbol),
                    ).fetchall()
                    points = [{"date": r["day"], "equity": r["equity"]} for r in rows]
                    data_source = {
                        "table": "dca_daily_snapshots",
                        "field": "total_invested",
                        "scope": "config_id",
                        "config_id": config_id,
                        "symbol": symbol,
                        "label": "dca_daily_snapshots.total_invested",
                        "display_label": "定投累计投入快照",
                        "kind": "dca_invested",
                    }

                history_compare_series.append(
                    {
                        "config_id": config_id,
                        "mode": mode,
                        "label": f"{cfg.get('title') or config_id} ({mode})",
                        "points": points,
                        "data_source": data_source,
                        **_equity_series_metadata(points),
                    }
                )

    return {
        "summaries": summaries,
        "current_symbol": symbol,
        "current_page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "active_agents": active_agents,
        "current_agent": agent_filter,
        "pnl_stats": pnl_stats,
        "mock_acc": mock_acc,
        "mock_chart_data": mock_chart_data,
        "agent_mode": agent_mode,
        "real_chart_data": real_chart_data,
        "real_balance": real_balance,
        "dca_stats": dca_stats,
        "dca_chart_data": dca_chart_data,
        "history_compare_series": history_compare_series,
        "compare_candidates": [
            {
                "config_id": cfg.get("config_id"),
                "label": cfg.get("title") or cfg.get("config_id"),
                "mode": str(cfg.get("mode", "STRATEGY")).upper(),
            }
            for cfg in symbol_configs
        ],
        "compare_ids": selected_compare_ids,
    }


def get_orders_payload(config_id: str, page: int = 1, per_page: int = 20):
    orders, total = get_paginated_orders(config_id, page, per_page)
    return {
        "orders": [_normalize_order_record(order) for order in orders],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": math.ceil(total / per_page) if per_page else 1,
    }


def get_recent_order_activity_payload(config_id: str, limit: int = 20):
    rows = _collapse_recent_order_activity(_get_recent_order_activity(config_id, limit))
    normalized = []
    for row in rows:
        if row.get("activity_type") == "trade":
            normalized.append(_normalize_trade_activity_record(row))
        else:
            normalized.append(_normalize_order_record(row))
    return {
        "orders": normalized,
        "total": len(normalized),
        "page": 1,
        "per_page": limit,
        "total_pages": 1,
        "source": "orders_and_trades",
    }


def get_daily_summaries_payload(config_id: str, days: int = 7):
    return {"daily_summaries": get_daily_summaries(config_id, days=days)}


def get_short_memories_payload(config_id: str, limit: int = 2):
    return {"short_memories": get_short_memories(config_id, limit=limit)}


def list_short_memories_payload(
    symbol: str | None = None,
    config_id: str | None = None,
    limit: int = 200,
):
    return {"short_memories": list_short_memories(symbol=symbol, config_id=config_id, limit=limit)}


def generate_short_memory_payload(config_id: str, bucket_start: str | None = None):
    from backend.agent.memory_service import generate_rolling_short_memory_for_config, MemoryReviewError
    from backend.database import get_summary_logic_between
    now = datetime.now(TZ_CN)
    target_time = now
    if bucket_start:
        try:
            parsed = datetime.fromisoformat(bucket_start)
            target_time = parsed.astimezone(TZ_CN) if parsed.tzinfo else TZ_CN.localize(parsed)
        except (ValueError, TypeError) as exc:
            raise ValueError('无效的复盘窗口时间') from exc
    if target_time > now:
        raise ValueError('只能整理已结束的时间，不能生成未来记忆')
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError('交易任务不存在')
    start, end = target_time - timedelta(hours=4), target_time
    rows = get_summary_logic_between(config_id, start.strftime('%Y-%m-%d %H:%M:%S'),
                                     (end + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'))
    failure = ''
    try:
        generated = generate_rolling_short_memory_for_config(config_id, {**cfg, 'enabled': True}, now_cn=target_time, hours=4)
    except MemoryReviewError as exc:
        generated, failure = False, str(exc)
    outcome = get_review_result(f'rolling:{config_id}:{rows[-1].get("timestamp")}') if rows else {}
    outcome = outcome or {}
    status = 'failed' if failure else outcome.get('status') or ('completed' if generated else 'unchanged' if not rows else 'failed')
    if status == 'completed' and not generated:
        status = 'unchanged'
    return {'generated': generated, 'review_status': status, 'error': failure or outcome.get('error', '')
            or ('整理未完成或已有整理正在运行，旧记忆已保留。' if status == 'failed' else ''),
            'rule_receipts': outcome.get('rule_receipts', []),
            'bucket_start': start.strftime('%Y-%m-%d %H:%M:%S'), 'bucket_end': end.strftime('%Y-%m-%d %H:%M:%S')}


def list_daily_summaries_payload(
    symbol: str | None = None,
    config_id: str | None = None,
    days: int | None = None,
    limit: int = 200,
):
    return {
        "daily_summaries": list_daily_summaries(symbol=symbol, config_id=config_id, days=days, limit=limit),
    }


def export_daily_summaries_payload(
    symbol: str | None = None,
    config_id: str | None = None,
    days: int | None = None,
):
    rows = list_daily_summaries(symbol=symbol, config_id=config_id, days=days, limit=1000)
    chunks = []
    for row in rows:
        chunks.append(
            "\n".join(
                [
                    f"# {row.get('date')} {row.get('symbol')} {row.get('config_id')}",
                    f"Created: {row.get('created_at') or '-'}",
                    "",
                    row.get("summary") or "",
                ]
            )
        )
    return "\n\n---\n\n".join(chunks)


def clean_history_payload(symbol: str):
    delete_summaries_by_symbol(symbol)
    clean_financial_data(symbol)
    return {"message": f"Reset all history and financial data for {symbol}."}


def update_daily_summary_payload(date_str: str, config_id: str, summary_content: str):
    db_update_daily_summary(date_str, config_id, summary_content)
    return {"message": "Daily summary updated."}


def update_short_memory_payload(config_id: str, bucket_start: str, market_summary: str, position_summary: str):
    updated = db_update_short_memory(config_id, bucket_start, market_summary, position_summary)
    if not updated:
        raise FileNotFoundError("Short memory not found")
    return {"message": "Short memory updated.", "updated": updated}


def delete_short_memories_payload(
    symbol: str | None = None,
    config_id: str | None = None,
    bucket_start_from: str | None = None,
    bucket_start_to: str | None = None,
    buckets: list[dict] | None = None,
):
    deleted = db_delete_short_memories(
        symbol=symbol,
        config_id=config_id,
        bucket_start_from=bucket_start_from,
        bucket_start_to=bucket_start_to,
        buckets=buckets or None,
    )
    return {"message": f"Deleted {deleted} short memories.", "deleted": deleted}


def delete_daily_summary_payload(date_str: str, config_id: str):
    deleted = db_delete_daily_summary(date_str, config_id)
    if not deleted:
        raise FileNotFoundError("Daily summary not found")
    return {"message": "Daily summary deleted.", "deleted": deleted}
