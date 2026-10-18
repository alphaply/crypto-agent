"""Spot exchange observation and valuation, separate from dashboard assembly.

Public dashboard wrappers provide dependencies so existing callers keep their
cache/refresh behavior. An observed order and its cumulative fill commit together.
"""
from __future__ import annotations

import math
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from backend import storage
from backend.utils.spot_portfolio import get_config_symbols
from backend.app.services.common import TZ_CN, logger


@dataclass(frozen=True)
class SpotStatisticsDependencies:
    connection_factory: Callable
    market_factory: Callable
    fill_writer: Callable
    trade_writer: Callable
    snapshot_writer: Callable
    cache: dict
    cache_ttl: float


def _synchronize_orders(config_id: str, symbol: str, mt, sync_errors: list[str],
                        dependencies: SpotStatisticsDependencies) -> None:
    try:
        with dependencies.connection_factory() as conn:
            open_rows = conn.execute(
                """
                SELECT order_id
                FROM orders
                WHERE config_id = %s
                  AND symbol = %s
                  AND trade_mode = 'SPOT_DCA'
                  AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'
                  AND UPPER(side) IN ('BUY','BUY_LIMIT')
                  AND status IN ('OPEN', 'PARTIAL')
                GROUP BY order_id
                ORDER BY MAX(id) DESC
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

                with dependencies.connection_factory() as conn:
                    conn.execute('''UPDATE orders SET status=%s, filled_amount=%s, filled_cost=%s,
                        avg_fill_price=%s, filled_at=%s WHERE order_id=%s AND config_id=%s AND symbol=%s
                        AND trade_mode='SPOT_DCA' AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''',
                        (local_status, filled_qty, filled_cost, avg_price, filled_at, order_id, config_id, symbol))
                    dependencies.fill_writer(order_id, config_id, symbol, local_status,
                                             filled_qty, filled_cost, avg_price, filled_at,
                                             connection=conn)
                    conn.commit()
            except storage.Error:
                raise
            except Exception as one_error:
                sync_errors.append(f'{symbol} 订单 {order_id} 同步失败')
                logger.debug(f"Skip spot order sync: {order_id} => {one_error}")
    except storage.Error:
        raise
    except Exception as sync_error:
        sync_errors.append(f'{symbol} 委托同步失败')
        logger.warning(f"DCA order sync failed for {config_id}: {sync_error}")


def _synchronize_trades(config_id: str, symbol: str, mt, sync_errors: list[str],
                        dependencies: SpotStatisticsDependencies) -> None:
    try:
        trades = mt.exchange.fetch_my_trades(symbol, limit=1000)
        if trades:
            with dependencies.connection_factory() as conn:
                owned_ids = {str(row[0]) for row in conn.execute('''SELECT order_id FROM orders
                    WHERE config_id=%s AND symbol=%s AND trade_mode='SPOT_DCA'
                    AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''', (config_id, symbol))}
            dependencies.trade_writer([trade for trade in trades if str(trade.get('order') or trade.get('order_id') or '') in owned_ids], config_id=config_id)
    except storage.Error:
        raise
    except Exception as trade_error:
        sync_errors.append(f'{symbol} 成交历史同步失败')
        logger.warning(f"Fetch my_trades failed for {symbol}: {trade_error}")


def calculate_spot_symbol_statistics(
    config_id: str, cfg: dict, symbol: str, force_sync: bool = False,
    *, dependencies: SpotStatisticsDependencies,
) -> dict | None:
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
            cached = dependencies.cache.get(cache_key)
            if cached and cached.get("signature") == cache_signature and now_ts - cached["timestamp"] < dependencies.cache_ttl:
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
        mt = dependencies.market_factory(config_id=config_id)
        from backend.utils.spot_execution import reconcile_spot_reservations
        reconcile_spot_reservations(config_id, cfg, lambda: mt)

        _synchronize_orders(config_id, symbol, mt, sync_errors, dependencies)
        _synchronize_trades(config_id, symbol, mt, sync_errors, dependencies)

        with dependencies.connection_factory() as conn:
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
                    WHERE symbol=%s AND (config_id=%s OR config_id IS NULL OR config_id='')
                      AND LOWER(side)='buy' GROUP BY order_id) t ON t.order_id=o.order_id
                WHERE o.config_id=%s AND o.symbol=%s AND o.trade_mode='SPOT_DCA'
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
                WHERE config_id = %s
                  AND symbol = %s
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
        except storage.Error:
            raise
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
        except storage.Error:
            raise
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
            dependencies.snapshot_writer(config_id, symbol, result)
        dependencies.cache[cache_key] = {"timestamp": now_ts, "signature": cache_signature, "data": result}
        return result
    except storage.Error:
        raise
    except Exception as exc:
        logger.error(f"Error calculating CCXT DCA stats for {config_id}: {exc}\n{traceback.format_exc()}")
        return None
