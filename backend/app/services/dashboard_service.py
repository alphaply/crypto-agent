import math
import sqlite3
import time
import traceback
from datetime import datetime, timedelta

from backend.agent.agent_graph import generate_manual_daily_summary, generate_short_memory_for_config, resolve_market_timeframes
from backend.config import config as global_config
from backend.database import (
    clean_financial_data,
    delete_summaries_by_symbol,
    delete_daily_summary as db_delete_daily_summary,
    get_active_agents,
    get_daily_summaries,
    get_short_memories,
    list_short_memories,
    get_db_conn,
    get_dca_daily_snapshot_history,
    get_history_pnl_stats,
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

from backend.app.services.common import TZ_CN, get_scheduler_status, get_symbol_specific_status, list_symbols, logger


DCA_STATS_CACHE = {}
DCA_STATS_CACHE_TTL = 300


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


def _normalize_order_record(order: dict) -> dict:
    payload = dict(order)
    payload["activity_type"] = payload.get("activity_type") or "order"
    payload["action_label"] = _order_action_label(payload)
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
            "action_label": action_label,
            "entry_price": payload.get("price"),
            "status": "FILLED",
            "strategy_note": f"PnL {realized_pnl:.2f}" if abs(realized_pnl) > 1e-12 else "Exchange fill",
            "reason": "; ".join(details),
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
                    NULL AS realized_pnl
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
                    'REAL' AS trade_mode,
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
                    realized_pnl
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
    latest_cancel_by_order: dict[str, dict] = {}
    latest_cancelled_open_by_order: dict[str, dict] = {}

    for row in rows:
        if row.get("activity_type") != "order":
            continue
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        side = str(row.get("side") or "").lower()
        status = str(row.get("status") or "").upper()
        if "cancel" in side and order_id not in latest_cancel_by_order:
            latest_cancel_by_order[order_id] = row
        elif "cancel" not in side and status == "CANCELLED" and order_id not in latest_cancelled_open_by_order:
            latest_cancelled_open_by_order[order_id] = row

    collapsed = []
    emitted_cancel_order_ids: set[str] = set()
    for row in rows:
        if row.get("activity_type") != "order":
            collapsed.append(row)
            continue

        order_id = str(row.get("order_id") or "").strip()
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

    pnl_stats = get_history_pnl_stats(symbol, config_id="ALL")
    return {
        "agent_count": len(agent_summaries),
        "win_rate": round(float(pnl_stats.get("win_rate", 0) or 0), 2),
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
                "win_rate": round(float(pnl.get("win_rate", 0) or 0), 2),
                "total_pnl": round(float(pnl.get("total_pnl", 0) or 0), 4),
            }
        )
    return rows


def calculate_next_run(config, latest_summary=None):
    mode = str(config.get("mode", "STRATEGY")).upper()
    now = datetime.now(TZ_CN)

    if mode in ["REAL", "STRATEGY"]:
        default_interval = 60 if mode == "STRATEGY" else 15
        interval = int(config.get("run_interval", default_interval))
        if interval < 15:
            interval = 15
        minutes_since_midnight = now.hour * 60 + now.minute
        next_total_minutes = ((minutes_since_midnight // interval) + 1) * interval
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_total_minutes)
        return next_run.strftime("%H:%M")

    if mode == "SPOT_DCA":
        dca_time_str = config.get("dca_time", "08:00")
        try:
            hour, minute = map(int, dca_time_str.split(":"))
        except Exception:
            hour, minute = 8, 0

        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if config.get("dca_freq", "1d") == "1w":
            target_weekday = int(config.get("dca_weekday", 0))
            days_ahead = target_weekday - now.weekday()
            if days_ahead < 0 or (days_ahead == 0 and now > next_run):
                days_ahead += 7
            next_run += timedelta(days=days_ahead)
        elif now > next_run:
            next_run += timedelta(days=1)
        return next_run.strftime("%m-%d %H:%M")

    return "N/A"


def calculate_dca_stats(config_id, force_sync=False):
    try:
        cache_key = str(config_id)
        now_ts = time.time()
        if not force_sync:
            cached = DCA_STATS_CACHE.get(cache_key)
            if cached and now_ts - cached["timestamp"] < DCA_STATS_CACHE_TTL:
                return cached["data"]

        cfg = global_config.get_config_by_id(config_id)
        if not cfg:
            return None

        symbol = cfg.get("symbol")
        if not symbol:
            return None

        base_asset = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
        initial_cost = float(cfg.get("initial_cost", 0) or 0)
        initial_qty = float(cfg.get("initial_qty", 0) or 0)
        mt = MarketTool(config_id=config_id)

        try:
            with get_db_conn() as conn:
                open_rows = conn.execute(
                    """
                    SELECT order_id
                    FROM orders
                    WHERE config_id = ?
                      AND trade_mode = 'SPOT_DCA'
                      AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 100
                    """,
                    (config_id,),
                ).fetchall()

            for row in open_rows:
                order_id = str(row["order_id"])
                try:
                    od = mt.exchange.fetch_order(order_id, symbol)
                    exch_status = str(od.get("status", "") or "").lower()
                    filled_qty = float(od.get("filled", 0) or 0)
                    filled_cost = float(od.get("cost", 0) or 0)
                    avg_price = float(od.get("average", 0) or 0)

                    if filled_qty > 0 and filled_cost <= 0 and avg_price > 0:
                        filled_cost = filled_qty * avg_price
                    if avg_price <= 0 and filled_qty > 0 and filled_cost > 0:
                        avg_price = filled_cost / filled_qty

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

                    update_order_fill_status(order_id, local_status, filled_qty, filled_cost, avg_price, filled_at)
                    upsert_spot_order_fill(order_id, config_id, symbol, local_status, filled_qty, filled_cost, avg_price, filled_at)
                except Exception as one_error:
                    logger.debug(f"Skip spot order sync: {order_id} => {one_error}")
        except Exception as sync_error:
            logger.warning(f"DCA order sync failed for {config_id}: {sync_error}")

        try:
            trades = mt.exchange.fetch_my_trades(symbol, limit=1000)
            if trades:
                save_trade_history(trades, config_id=config_id)
        except Exception as trade_error:
            logger.warning(f"Fetch my_trades failed for {symbol}: {trade_error}")

        with get_db_conn() as conn:
            agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(t.cost), 0) AS traded_cost,
                    COALESCE(SUM(t.amount), 0) AS traded_qty,
                    COUNT(DISTINCT t.order_id) AS buy_count,
                    MIN(t.timestamp) AS first_buy,
                    MAX(t.timestamp) AS last_buy
                FROM trade_history t
                INNER JOIN orders o ON o.order_id = t.order_id
                WHERE o.config_id = ?
                  AND o.trade_mode = 'SPOT_DCA'
                  AND LOWER(t.side) = 'buy'
                """,
                (config_id,),
            ).fetchone()

            pending = conn.execute(
                """
                SELECT COUNT(*) AS pending_count
                FROM orders
                WHERE config_id = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND status IN ('OPEN', 'PARTIAL')
                """,
                (config_id,),
            ).fetchone()

        traded_cost = float(agg["traded_cost"] or 0)
        traded_qty = float(agg["traded_qty"] or 0)
        buy_count = int(agg["buy_count"] or 0)

        balances = mt.exchange.fetch_balance()
        current_qty = 0
        if base_asset in balances:
            current_qty = float(balances[base_asset].get("total", 0) or 0)
        elif base_asset.lower() in balances:
            current_qty = float(balances[base_asset.lower()].get("total", 0) or 0)
        elif "total" in balances and base_asset in balances["total"]:
            current_qty = float(balances["total"].get(base_asset, 0) or 0)

        final_qty = traded_qty + initial_qty
        final_invested = traded_cost + initial_cost
        avg_cost = (final_invested / final_qty) if final_qty > 0 else 0

        result = {
            "buy_count": buy_count,
            "total_invested": round(final_invested, 2),
            "total_qty": round(final_qty, 6),
            "avg_cost": round(avg_cost, 4),
            "dca_amount_per": cfg.get("dca_amount", cfg.get("dca_budget", 0)),
            "has_legacy": initial_qty > 0,
            "first_buy": agg["first_buy"],
            "last_buy": agg["last_buy"],
            "actual_balance": round(current_qty, 6),
            "pending_orders": int(pending["pending_count"] or 0),
            "sync_status": "synced",
            "last_sync": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        }

        save_dca_daily_snapshot(config_id, symbol, result)
        DCA_STATS_CACHE[cache_key] = {"timestamp": now_ts, "data": result}
        return result
    except Exception as exc:
        logger.error(f"Error calculating CCXT DCA stats for {config_id}: {exc}\n{traceback.format_exc()}")
        return None


def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        with get_db_conn() as conn:
            configs = global_config.get_all_symbol_configs()
            symbol_configs = [conf for conf in configs if conf["symbol"] == symbol and conf.get("enabled", True)]

            agent_summaries = []
            for config in symbol_configs:
                config_id = config["config_id"]
                latest_summary_row = conn.execute(
                    "SELECT * FROM summaries WHERE config_id = ? ORDER BY id DESC LIMIT 1",
                    (config_id,),
                ).fetchone()

                mode = str(config.get("mode", "STRATEGY")).upper()
                model_name = config.get("model", "Unknown")
                enabled = config.get("enabled", True)

                if latest_summary_row:
                    summary_dict = dict(latest_summary_row)
                else:
                    summary_dict = {
                        "config_id": config_id,
                        "agent_name": model_name,
                        "symbol": symbol,
                        "content": "No analysis data yet.",
                        "strategy_logic": "N/A",
                        "timestamp": "N/A",
                        "agent_type": None,
                        "id": -1,
                    }

                summary_dict["model"] = model_name
                summary_dict["mode"] = mode
                summary_dict["enabled"] = enabled
                summary_dict["next_run"] = calculate_next_run(config, latest_summary_row)
                if mode == "SPOT_DCA":
                    summary_dict["freq"] = f"{config.get('dca_freq', '1d')} (DCA)"
                    summary_dict["dca_stats"] = calculate_dca_stats(config_id)
                else:
                    default_interval = 60 if mode == "STRATEGY" else 15
                    interval = config.get("run_interval", default_interval)
                    summary_dict["freq"] = f"{interval}m"

                summary_dict["leverage"] = global_config.get_leverage(config_id)
                summary_dict["market_timeframes"] = resolve_market_timeframes(config)
                summary_dict["display_name"] = f"{model_name} ({mode})"
                orders, total = get_paginated_orders(config_id, page=1, per_page=10)
                summary_dict["all_orders"] = orders
                summary_dict["order_total"] = total
                summary_dict["order_page"] = 1
                summary_dict["daily_summaries"] = get_daily_summaries(config_id, days=7)
                agent_summaries.append(summary_dict)

        return agent_summaries
    except Exception as exc:
        logger.error(f"Failed to load dashboard data for {symbol}: {exc}")
        return []


def build_dashboard_overview(symbol: str | None = None, page: int = 1):
    symbols = list_symbols()
    current_symbol = symbol or (symbols[0] if symbols else "BTC/USDT")
    agent_summaries = get_dashboard_data(current_symbol, page)
    symbol_mode, symbol_freq, symbol_enabled = get_symbol_specific_status(current_symbol)
    return {
        "symbols": symbols,
        "current_symbol": current_symbol,
        "agent_summaries": agent_summaries,
        "overview_metrics": build_symbol_overview_metrics(current_symbol, agent_summaries),
        "compare_rows": build_config_compare_rows(current_symbol, agent_summaries),
        "symbol_mode": symbol_mode,
        "symbol_freq": symbol_freq,
        "symbol_enabled": symbol_enabled,
        "scheduler_enabled": get_scheduler_status(),
        "market_timeframes": list(getattr(global_config, "market_timeframes", None) or []),
    }


def build_history_payload(symbol: str, agent_filter: str = "ALL", page: int = 1, per_page: int = 20, compare_ids: list[str] | None = None):
    compare_ids = compare_ids or []
    symbol_configs = [
        cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("symbol") == symbol and cfg.get("config_id")
    ]
    config_map = {cfg.get("config_id"): cfg for cfg in symbol_configs}
    if agent_filter != "ALL" and agent_filter not in config_map:
        agent_filter = "ALL"

    summaries = get_paginated_summaries(symbol, page, per_page, config_id=agent_filter)
    total_count = get_summary_count(symbol, config_id=agent_filter)
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    active_agents = [aid for aid in get_active_agents(symbol) if aid in config_map]
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
        mock_chart_data = get_mock_equity_history(mock_config_id)

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
                if not rows:
                    rows = conn.execute(
                        """
                        SELECT day, total_equity FROM (
                            SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                   row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                            FROM balance_history WHERE (config_id IS NULL OR config_id = '') AND symbol = ? AND total_equity > 0
                        ) WHERE rn = 1 ORDER BY day ASC
                        """,
                        (symbol,),
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
        dca_stats = calculate_dca_stats(agent_filter)
        dca_chart_data = get_dca_daily_snapshot_history(agent_filter, days=30)

    history_compare_series = []
    selected_compare_ids = [cid for cid in compare_ids if cid in config_map]
    if agent_filter == "ALL" or selected_compare_ids:
        target_cfgs = [config_map[cid] for cid in selected_compare_ids] if selected_compare_ids else symbol_configs
        with get_db_conn() as conn:
            for cfg in target_cfgs:
                config_id = cfg.get("config_id")
                mode = str(cfg.get("mode", "STRATEGY")).upper()
                if not config_id or mode == "SPOT_DCA":
                    continue

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
                    if not rows:
                        rows = conn.execute(
                            """
                            SELECT day, total_equity FROM (
                                SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                       row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                                FROM balance_history WHERE (config_id IS NULL OR config_id = '') AND symbol = ? AND total_equity > 0
                            ) WHERE rn = 1 ORDER BY day ASC
                            """,
                            (symbol,),
                        ).fetchall()
                    points = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
                else:
                    strategy_points = get_mock_equity_history(config_id)
                    points = [
                        {"date": p.get("date"), "equity": p.get("equity", p.get("balance"))}
                        for p in strategy_points
                        if p.get("date") is not None and (p.get("equity") is not None or p.get("balance") is not None)
                    ]

                if points:
                    history_compare_series.append(
                        {"config_id": config_id, "mode": mode, "label": f"{config_id} ({mode})", "points": points}
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
    target_time = None
    if bucket_start:
        try:
            target_time = TZ_CN.localize(datetime.strptime(bucket_start, "%Y-%m-%d %H:%M:%S")) + timedelta(seconds=1)
        except Exception:
            target_time = None
    return {"generated": generate_short_memory_for_config(config_id, now_cn=target_time)}


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


def generate_daily_summary_payload(config_id: str, date_str: str):
    return {"success": generate_manual_daily_summary(config_id, date_str)}


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


def delete_daily_summary_payload(date_str: str, config_id: str):
    deleted = db_delete_daily_summary(date_str, config_id)
    if not deleted:
        raise FileNotFoundError("Daily summary not found")
    return {"message": "Daily summary deleted.", "deleted": deleted}
