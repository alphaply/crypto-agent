import math
import sqlite3
import time
import traceback
from datetime import datetime, timedelta

from backend.agent.agent_graph import generate_manual_daily_summary
from backend.config import config as global_config
from backend.database import (
    clean_financial_data,
    delete_summaries_by_symbol,
    get_active_agents,
    get_daily_summaries,
    get_db_conn,
    get_dca_daily_snapshot_history,
    get_history_pnl_stats,
    get_mock_account,
    get_mock_equity_history,
    get_paginated_orders,
    get_paginated_summaries,
    get_summary_count,
    save_dca_daily_snapshot,
    save_trade_history,
    update_daily_summary as db_update_daily_summary,
    update_order_fill_status,
    upsert_spot_order_fill,
)
from backend.utils.market_data import MarketTool

from backend.app.services.common import TZ_CN, get_scheduler_status, get_symbol_specific_status, list_symbols, logger


DCA_STATS_CACHE = {}
DCA_STATS_CACHE_TTL = 300


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
                save_trade_history(trades)
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
            symbol_configs = [conf for conf in configs if conf["symbol"] == symbol]

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
        "symbol_mode": symbol_mode,
        "symbol_freq": symbol_freq,
        "symbol_enabled": symbol_enabled,
        "scheduler_enabled": get_scheduler_status(),
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
                        FROM balance_history WHERE symbol = ? AND total_equity > 0
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
                            FROM balance_history WHERE symbol = ? AND total_equity > 0
                        ) WHERE rn = 1 ORDER BY day ASC
                        """,
                        (symbol,),
                    ).fetchall()
                    points = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
                else:
                    strategy_points = get_mock_equity_history(config_id)
                    points = [
                        {"date": p.get("date"), "equity": p.get("balance")}
                        for p in strategy_points
                        if p.get("date") is not None and p.get("balance") is not None
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
        "orders": orders,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": math.ceil(total / per_page) if per_page else 1,
    }


def get_daily_summaries_payload(config_id: str, days: int = 7):
    return {"daily_summaries": get_daily_summaries(config_id, days=days)}


def generate_daily_summary_payload(config_id: str, date_str: str):
    return {"success": generate_manual_daily_summary(config_id, date_str)}


def clean_history_payload(symbol: str):
    delete_summaries_by_symbol(symbol)
    clean_financial_data(symbol)
    return {"message": f"Reset all history and financial data for {symbol}."}


def update_daily_summary_payload(date_str: str, config_id: str, summary_content: str):
    db_update_daily_summary(date_str, config_id, summary_content)
    return {"message": "Daily summary updated."}
