import sqlite3

import pandas as pd

from backend.config import config as global_config
from backend.database import DB_NAME, delete_model_pricing, get_all_pricing, get_history_pnl_stats, save_trade_history, update_model_pricing
from backend.utils.indicators import calc_ema
from backend.utils.market_data import MarketTool

from backend.app.services.common import logger
from backend.app.services.dashboard_service import calculate_dca_stats


def get_token_stats_payload():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    pricing = get_all_pricing()
    daily_stats = cursor.execute(
        """
        SELECT strftime('%Y-%m-%d', timestamp) as day,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as completion,
               SUM(total_tokens) as total
        FROM token_usage
        GROUP BY day
        ORDER BY day DESC LIMIT 14
        """
    ).fetchall()

    daily_costs = {}
    all_usages = cursor.execute("SELECT timestamp, model, prompt_tokens, completion_tokens FROM token_usage").fetchall()
    for usage in all_usages:
        day = usage["timestamp"][:10]
        m_price = pricing.get(usage["model"], {"input_price_per_m": 0, "output_price_per_m": 0})
        cost = (usage["prompt_tokens"] / 1_000_000 * m_price["input_price_per_m"]) + (
            usage["completion_tokens"] / 1_000_000 * m_price["output_price_per_m"]
        )
        daily_costs[day] = daily_costs.get(day, 0) + cost

    model_stats = cursor.execute(
        """
        SELECT model,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as completion,
               SUM(total_tokens) as total
        FROM token_usage
        GROUP BY model
        """
    ).fetchall()

    model_stats_list = []
    for model_row in model_stats:
        row = dict(model_row)
        m_price = pricing.get(row["model"], {"input_price_per_m": 0, "output_price_per_m": 0})
        row["cost"] = (row["prompt"] / 1_000_000 * m_price["input_price_per_m"]) + (
            row["completion"] / 1_000_000 * m_price["output_price_per_m"]
        )
        model_stats_list.append(row)

    agent_stats = cursor.execute(
        """
        SELECT config_id, symbol,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as completion,
               SUM(total_tokens) as total
        FROM token_usage
        GROUP BY config_id
        """
    ).fetchall()

    conn.close()
    daily_formatted = []
    for row in daily_stats:
        formatted = dict(row)
        formatted["cost"] = round(daily_costs.get(formatted["day"], 0), 4)
        daily_formatted.append(formatted)

    return {
        "daily": daily_formatted,
        "models": model_stats_list,
        "agents": [dict(row) for row in agent_stats],
        "pricing": pricing,
    }


def save_pricing_payload(model: str, input_price: float, output_price: float, currency: str = "USD"):
    update_model_pricing(model, input_price, output_price, currency or "USD")
    return {"message": "Pricing updated."}


def list_pricing_payload():
    pricing = get_all_pricing()
    items = []
    for model, row in pricing.items():
        items.append(
            {
                "model": model,
                "input_price_per_m": row.get("input_price_per_m", 0),
                "output_price_per_m": row.get("output_price_per_m", 0),
                "currency": row.get("currency", "USD"),
            }
        )
    items.sort(key=lambda item: item["model"])
    return {"pricing": items}


def delete_pricing_payload(model: str):
    deleted = delete_model_pricing(model)
    if not deleted:
        raise FileNotFoundError("Pricing model not found")
    return {"message": "Pricing deleted."}


def get_financial_stats_payload(symbol: str):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    balance_history = cursor.execute(
        "SELECT timestamp, total_equity, total_balance FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT 200",
        (symbol,),
    ).fetchall()

    daily_equity = cursor.execute(
        """
        SELECT day, total_equity FROM (
            SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                   row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
            FROM balance_history WHERE symbol = ?
        ) WHERE rn = 1 ORDER BY day ASC
        """,
        (symbol,),
    ).fetchall()

    trades = cursor.execute("SELECT realized_pnl FROM trade_history WHERE symbol = ?", (symbol,)).fetchall()
    total_pnl = sum(trade["realized_pnl"] for trade in trades)
    win_trades = [trade for trade in trades if trade["realized_pnl"] > 0]
    lose_trades = [trade for trade in trades if trade["realized_pnl"] < 0]
    win_rate = (len(win_trades) / len(trades) * 100) if trades else 0

    latest_equity = balance_history[-1]["total_equity"] if balance_history else 0
    latest_balance = balance_history[-1]["total_balance"] if balance_history else 0
    conn.close()

    return {
        "balance_history": [dict(row) for row in balance_history],
        "daily_equity": [dict(row) for row in daily_equity],
        "summary": {
            "total_trades": len(trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 2),
            "win_count": len(win_trades),
            "lose_count": len(lose_trades),
            "latest_equity": round(latest_equity, 2),
            "latest_balance": round(latest_balance, 2),
        },
    }


def get_agent_stats_payload(config_id: str):
    from backend.database import get_agent_trade_stats

    stats = get_agent_trade_stats(config_id)
    cfg = global_config.get_config_by_id(config_id)
    if cfg and cfg.get("mode") == "SPOT_DCA":
        stats["dca_stats"] = calculate_dca_stats(config_id)
        stats["mode"] = "SPOT_DCA"
    else:
        stats["mode"] = cfg.get("mode", "STRATEGY") if cfg else "STRATEGY"
    return {"stats": stats}


def _calculate_win_rate(trade_summary):
    total_decided = trade_summary["win_count"] + trade_summary["lose_count"]
    trade_summary["win_rate"] = round(trade_summary["win_count"] / total_decided * 100, 1) if total_decided > 0 else 0
    trade_summary["realized_pnl"] = round(trade_summary["realized_pnl"], 4)
    return trade_summary


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _resolve_leverage(position, cfg, fallback_leverage):
    info = position.get("info", {}) or {}
    candidates = [
        position.get("leverage"),
        position.get("info", {}).get("leverage") if isinstance(position.get("info"), dict) else None,
        info.get("leverage"),
        info.get("bracketLeverage"),
    ]
    for item in candidates:
        val = _safe_float(item, 0)
        if val > 0:
            return val

    notional = abs(_safe_float(position.get("notional"), 0))
    initial_margin = abs(_safe_float(position.get("initialMargin"), 0))
    if notional > 0 and initial_margin > 0:
        inferred = notional / initial_margin
        if inferred > 0:
            return inferred

    cfg_lev = _safe_float(cfg.get("leverage") if isinstance(cfg, dict) else None, 0)
    if cfg_lev > 0:
        return cfg_lev
    return _safe_float(fallback_leverage, 1) or 1


def _fetch_real_position_data(mt, symbol, cfg):
    positions = []
    balance = 0
    recent_trades = []
    fetch_errors = []
    trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0, "lose_count": 0, "win_rate": 0}
    fallback_leverage = global_config.get_leverage(cfg.get("config_id") if isinstance(cfg, dict) else None)

    try:
        all_positions = mt.exchange.fetch_positions([symbol])
        for position in all_positions:
            contracts = float(position.get("contracts", 0))
            if contracts <= 0:
                continue
            entry = float(position.get("entryPrice", 0))
            unrealized = float(position.get("unrealizedPnl", 0))
            notional = float(position.get("notional", 0)) or (entry * contracts)
            leverage = _resolve_leverage(position, cfg, fallback_leverage)
            pnl_pct = (unrealized / abs(notional) * 100) if notional != 0 else 0
            roi_pct = pnl_pct * leverage
            margin_used = abs(notional) / leverage if leverage > 0 else abs(notional)
            positions.append(
                {
                    "symbol": position.get("symbol", symbol),
                    "side": str(position.get("side", "")).upper(),
                    "contracts": contracts,
                    "qty": contracts,
                    "entry_price": entry,
                    "mark_price": float(position.get("markPrice", 0)),
                    "unrealized_pnl": round(unrealized, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "roi_pct": round(roi_pct, 2),
                    "leverage": leverage,
                    "notional": round(abs(notional), 2),
                    "margin_used": round(margin_used, 2),
                }
            )
    except Exception as exc:
        fetch_errors.append(f"fetch_positions_failed({type(exc).__name__}): {exc}")
        logger.error(f"REAL positions fetch failed config_id={cfg.get('config_id')} symbol={symbol}: {exc}", exc_info=True)

    try:
        balance_data = mt.exchange.fetch_balance()
        balance = float(
            balance_data.get("USDT", {}).get("total", 0)
            or balance_data.get("USDT", {}).get("free", 0)
            or balance_data.get("total", {}).get("USDT", 0)
            or 0
        )
        info = balance_data.get("info", {}) or {}
        for key in ("totalMarginBalance", "marginBalance", "totalWalletBalance"):
            if info.get(key) not in (None, ""):
                balance = float(info.get(key) or 0)
                break
    except Exception as exc:
        fetch_errors.append(f"fetch_balance_failed({type(exc).__name__}): {exc}")
        logger.error(f"REAL balance fetch failed config_id={cfg.get('config_id')} symbol={symbol}: {exc}", exc_info=True)

    try:
        raw_trades = mt.exchange.fetch_my_trades(symbol, limit=100)
        if raw_trades:
            save_trade_history(raw_trades, config_id=cfg.get("config_id"))
            aggregated = {}
            for trade in raw_trades:
                pnl = float(trade.get("info", {}).get("realizedPnl", 0) or 0)
                if pnl == 0:
                    continue
                order_id = str(trade.get("order", trade.get("order_id", "unknown")))
                if order_id not in aggregated:
                    aggregated[order_id] = {
                        "time": trade.get("datetime", ""),
                        "side": trade.get("side", ""),
                        "price": float(trade.get("price", 0)),
                        "amount": float(trade.get("amount", 0)),
                        "pnl": pnl,
                    }
                else:
                    old = aggregated[order_id]
                    new_total_amount = old["amount"] + float(trade.get("amount", 0))
                    if new_total_amount > 0:
                        old["price"] = (
                            old["price"] * old["amount"] + float(trade.get("price", 0)) * float(trade.get("amount", 0))
                        ) / new_total_amount
                    old["amount"] = new_total_amount
                    old["pnl"] += pnl
                    old["time"] = trade.get("datetime", old["time"])

            def _approximate_entry(side, price, amount, pnl):
                if amount <= 0:
                    return 0
                if str(side).lower() == "sell":
                    return round(price - (pnl / amount), 4)
                return round(price + (pnl / amount), 4)

            display_trades = []
            for _, data in aggregated.items():
                side = data["side"].upper()
                label_side = "LONG (Closed)" if side == "SELL" else "SHORT (Closed)"
                display_trades.append(
                    {
                        "time": data["time"],
                        "side": label_side,
                        "price": round(data["price"], 4),
                        "amount": round(data["amount"], 4),
                        "pnl": round(data["pnl"], 4),
                        "entry_price": _approximate_entry(data["side"], data["price"], data["amount"], data["pnl"]),
                    }
                )
            recent_trades = sorted(display_trades, key=lambda item: item["time"], reverse=True)[:5]
    except Exception as exc:
        logger.warning(f"Fetch trades error config_id={cfg.get('config_id')} symbol={symbol}: {exc}")

    try:
        pnl_stats = get_history_pnl_stats(symbol, cfg.get("config_id"))
        if pnl_stats:
            trade_summary["win_count"] = pnl_stats.get("win_count", 0)
            trade_summary["lose_count"] = pnl_stats.get("lose_count", 0)
            trade_summary["total_trades"] = pnl_stats.get("total_trades", 0)
            trade_summary["realized_pnl"] = pnl_stats.get("total_pnl", 0)
    except Exception as exc:
        logger.warning(f"Failed to load full PnL stats from DB: {exc}")

    return positions, balance, recent_trades, _calculate_win_rate(trade_summary), fetch_errors


def _fetch_strategy_position_data(mt, config_id, symbol, cfg):
    from backend.database import get_db_conn, get_mock_account

    positions = []
    recent_trades = []
    trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0, "lose_count": 0, "win_rate": 0}
    fallback_leverage = global_config.get_leverage(config_id)
    current_price = 0
    try:
        ticker = mt.exchange.fetch_ticker(symbol)
        current_price = float(ticker.get("last", 0))
    except Exception as exc:
        logger.warning(f"Fetch ticker error in STRATEGY mode: {exc}")

    mock_account = get_mock_account(config_id, symbol)
    balance = mock_account.get("balance", 10000.0)

    with get_db_conn() as conn:
        cursor = conn.cursor()
        open_mocks = cursor.execute(
            "SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN'",
            (config_id, symbol),
        ).fetchall()
        for order in open_mocks:
            if not int(order["is_filled"] or 0):
                continue
            entry = float(order["price"])
            amount = float(order["amount"])
            side = str(order["side"]).upper()
            unrealized = 0
            if current_price > 0:
                if "BUY" in side:
                    unrealized = (current_price - entry) * amount
                else:
                    unrealized = (entry - current_price) * amount
            notional = entry * amount
            leverage = _safe_float(cfg.get("leverage"), 0) or _safe_float(fallback_leverage, 1) or 1
            pnl_pct = (unrealized / notional * 100) if notional > 0 else 0
            roi_pct = pnl_pct * leverage
            margin_used = notional / leverage if leverage > 0 else notional
            positions.append(
                {
                    "symbol": symbol,
                    "side": "LONG" if "BUY" in side else "SHORT",
                    "contracts": amount,
                    "qty": amount,
                    "entry_price": entry,
                    "mark_price": current_price,
                    "unrealized_pnl": round(unrealized, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "roi_pct": round(roi_pct, 2),
                    "leverage": leverage,
                    "notional": round(notional, 2),
                    "margin_used": round(margin_used, 2),
                }
            )

        closed_mocks = cursor.execute(
            "SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='CLOSED' AND realized_pnl IS NOT NULL",
            (config_id, symbol),
        ).fetchall()
        for order in closed_mocks:
            pnl = float(order["realized_pnl"] or 0)
            trade_summary["realized_pnl"] += pnl
            if pnl > 0:
                trade_summary["win_count"] += 1
            elif pnl < 0:
                trade_summary["lose_count"] += 1
        trade_summary["total_trades"] = len(closed_mocks)
        _calculate_win_rate(trade_summary)
        recent_trades = [
            {
                "time": trade["close_time"] or trade["timestamp"],
                "side": trade["side"],
                "entry_price": float(trade["price"] or 0),
                "price": float(trade["close_price"] or 0),
                "amount": float(trade["amount"]),
                "pnl": float(trade["realized_pnl"] or 0),
            }
            for trade in closed_mocks[-5:]
        ]

    return positions, balance, recent_trades, trade_summary


def get_position_stats_payload(config_id: str):
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")

    mode = str(cfg.get("mode", "STRATEGY")).upper()
    symbol = cfg.get("symbol")
    if not symbol:
        raise ValueError(f"Config {config_id} is missing symbol")
    if mode not in ["REAL", "STRATEGY"]:
        return {"mode": mode, "positions": [], "summary": None, "message": "Only REAL/STRATEGY support live positions"}

    mt = MarketTool(config_id=config_id)
    if mode == "REAL":
        positions, balance, recent_trades, trade_summary, fetch_errors = _fetch_real_position_data(mt, symbol, cfg)
    else:
        positions, balance, recent_trades, trade_summary = _fetch_strategy_position_data(mt, config_id, symbol, cfg)
        fetch_errors = []

    unrealized_total = sum(float(item.get("unrealized_pnl", 0) or 0) for item in positions)
    margin_balance = balance if mode == "REAL" else balance + unrealized_total

    return {
        "mode": mode,
        "positions": positions,
        "balance": round(balance, 2),
        "margin_balance": round(margin_balance, 2),
        "unrealized_pnl": round(unrealized_total, 4),
        "recent_trades": recent_trades,
        "summary": trade_summary,
        "errors": fetch_errors,
    }


def get_equity_compare_payload(symbol: str, config_ids: str = ""):
    configs = [cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("symbol") == symbol and cfg.get("enabled", True)]
    if config_ids:
        wanted = {item.strip() for item in config_ids.split(",") if item.strip()}
        configs = [cfg for cfg in configs if cfg.get("config_id") in wanted]
    configs = configs[:12]

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    series = []
    for cfg in configs:
        config_id = cfg.get("config_id")
        mode = str(cfg.get("mode") or "STRATEGY").upper()
        label = f"{config_id} ({mode})"
        points = []
        if mode == "REAL":
            # 优先按 config_id 过滤，旧数据无 config_id 时回退为按 symbol
            rows = cursor.execute(
                """
                SELECT day, total_equity FROM (
                    SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                           row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                    FROM balance_history WHERE config_id = ? AND symbol = ?
                ) WHERE rn = 1 ORDER BY day ASC
                """,
                (config_id, symbol),
            ).fetchall()
            if not rows:
                rows = cursor.execute(
                    """
                    SELECT day, total_equity FROM (
                        SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                               row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                        FROM balance_history WHERE (config_id IS NULL OR config_id = '') AND symbol = ?
                    ) WHERE rn = 1 ORDER BY day ASC
                    """,
                    (symbol,),
                ).fetchall()
            points = [{"date": row["day"], "equity": row["total_equity"]} for row in rows]
        else:
            # STRATEGY 模式：优先取 total_equity，旧数据回退 balance
            rows = cursor.execute(
                """
                SELECT h.day as day, COALESCE(h.total_equity, h.balance) as equity FROM (
                    SELECT date(timestamp) as day,
                           total_equity, balance, timestamp,
                           row_number() OVER (PARTITION BY date(timestamp) ORDER BY id DESC) as rn
                    FROM mock_balance_history WHERE config_id = ?
                ) h WHERE h.rn = 1 ORDER BY h.day ASC
                """,
                (config_id,),
            ).fetchall()
            points = [{"date": row["day"], "equity": row["equity"]} for row in rows]

        if points:
            series.append({"config_id": config_id, "label": label, "mode": mode, "points": points})

    conn.close()
    return {"symbol": symbol, "series": series}


_KLINE_ALLOWED_TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"}


def _normalize_real_open_order(order: dict, current_side: str, positions: list[dict]) -> dict:
    info = order.get("info", {}) or {}
    side = str(order.get("side", "")).upper()
    raw_type = str(order.get("type") or info.get("type") or "").upper()
    pos_side = str(info.get("positionSide") or order.get("pos_side") or "BOTH").upper()
    price = _safe_float(order.get("price"), 0)
    stop_price = _safe_float(order.get("stopPrice") or info.get("stopPrice") or info.get("triggerPrice"), 0)
    if price <= 0 and stop_price > 0:
        price = stop_price
    amount = _safe_float(order.get("amount") or info.get("origQty") or info.get("qty"), 0)
    if amount <= 0 and str(info.get("closePosition", "")).lower() == "true":
        for position in positions:
            if str(position.get("side", "")).upper() == pos_side:
                amount = _safe_float(position.get("amount") or position.get("qty") or position.get("contracts"), 0)
                break

    reduce_only = bool(
        order.get("reduceOnly") or order.get("reduce_only") or info.get("reduceOnly") or info.get("closePosition")
    )
    if reduce_only:
        order_type = "close_long" if side == "SELL" else "close_short"
    elif current_side == "LONG" and side == "SELL":
        order_type = "close_long"
    elif current_side == "SHORT" and side == "BUY":
        order_type = "close_short"
    else:
        order_type = "open_long" if side == "BUY" else "open_short"

    if "STOP" in raw_type:
        order_type = f"{order_type}_stop"
    elif "TAKE_PROFIT" in raw_type:
        order_type = f"{order_type}_tp"

    return {
        "price": price,
        "trigger_price": stop_price,
        "side": side,
        "pos_side": pos_side,
        "amount": amount,
        "order_id": order.get("id", ""),
        "type": order_type,
        "raw_type": raw_type,
    }


def _fetch_real_open_orders(mt: MarketTool, symbol: str, current_side: str, positions: list[dict]) -> list[dict]:
    regular_orders = mt.exchange.fetch_open_orders(symbol)
    trigger_orders = []
    try:
        trigger_orders = mt.exchange.fetch_open_orders(symbol, params={"trigger": True})
    except Exception as exc:
        logger.warning(f"Kline trigger orders fetch failed: {exc}")

    seen = set()
    pending_orders = []
    for order in [*regular_orders, *trigger_orders]:
        order_id = str(order.get("id") or "")
        dedupe_key = order_id or f"{order.get('type')}:{order.get('side')}:{order.get('price')}:{order.get('amount')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        pending_orders.append(_normalize_real_open_order(order, current_side, positions))
    return pending_orders


def get_kline_payload(config_id: str, timeframe: str = "1h"):
    if timeframe not in _KLINE_ALLOWED_TF:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")
    symbol = cfg.get("symbol")
    mode = str(cfg.get("mode") or "STRATEGY").upper()
    mt = MarketTool(config_id=config_id)
    fetch_limit = 260 if timeframe in {"1w", "1M"} else (360 if timeframe == "1d" else 500)
    raw = mt.exchange.fetch_ohlcv(symbol, timeframe, limit=fetch_limit)
    if not raw:
        return {
            "candles": [],
            "volume": [],
            "emas": {},
            "orders": [],
            "position": None,
            "positions": [],
            "pending_orders": [],
            "risk_lines": [],
        }

    candles = []
    volumes = []
    closes = []
    for row in raw:
        ts = int(row[0] / 1000)
        open_price, high, low, close, volume = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        candles.append({"time": ts, "open": open_price, "high": high, "low": low, "close": close})
        color = "rgba(38,166,154,0.5)" if close >= open_price else "rgba(239,83,80,0.5)"
        volumes.append({"time": ts, "value": volume, "color": color})
        closes.append(close)

    close_series = pd.Series(closes)
    emas = {}
    for span in (20, 50, 100, 200):
        ema_vals = calc_ema(close_series, span)
        ema_data = []
        for index, value in enumerate(ema_vals):
            if pd.notna(value) and index >= span - 1:
                ema_data.append({"time": candles[index]["time"], "value": round(float(value), 6)})
        emas[str(span)] = ema_data

    positions = []
    position = None
    risk_lines = []
    if mode == "REAL":
        try:
            all_positions = mt.exchange.fetch_positions([symbol])
            for current in all_positions:
                if float(current.get("contracts", 0)) > 0:
                    payload = {
                        "side": str(current.get("side", "")).upper(),
                        "entry_price": float(current.get("entryPrice", 0)),
                        "amount": float(current.get("contracts", 0)),
                        "mark_price": float(current.get("markPrice", 0) or 0),
                        "unrealized_pnl": round(float(current.get("unrealizedPnl", 0) or 0), 4),
                    }
                    positions.append(payload)
            if positions:
                position = positions[0]
        except Exception as exc:
            logger.warning(f"Kline positions fetch failed: {exc}")
    elif mode == "STRATEGY":
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT side, price, amount, stop_loss, take_profit, order_id FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN' AND is_filled=1 ORDER BY timestamp ASC",
            (config_id, symbol),
        ).fetchall()
        conn.close()
        for row in rows:
            payload = {
                "side": "LONG" if "BUY" in str(row["side"]).upper() else "SHORT",
                "entry_price": float(row["price"]),
                "amount": float(row["amount"]),
                "order_id": row["order_id"],
                "stop_loss": float(row["stop_loss"] or 0),
                "take_profit": float(row["take_profit"] or 0),
            }
            if candles:
                current_price = float(candles[-1]["close"])
                direction = 1 if payload["side"] == "LONG" else -1
                payload["mark_price"] = current_price
                payload["unrealized_pnl"] = round((current_price - payload["entry_price"]) * payload["amount"] * direction, 4)
            positions.append(payload)
            if float(row["take_profit"] or 0) > 0:
                risk_lines.append(
                    {
                        "price": float(row["take_profit"]),
                        "type": "take_profit",
                        "label": "TP",
                        "amount": float(row["amount"]),
                        "side": payload["side"],
                        "order_id": row["order_id"],
                    }
                )
            if float(row["stop_loss"] or 0) > 0:
                risk_lines.append(
                    {
                        "price": float(row["stop_loss"]),
                        "type": "stop_loss",
                        "label": "SL",
                        "amount": float(row["amount"]),
                        "side": payload["side"],
                        "order_id": row["order_id"],
                    }
                )
        if positions:
            position = positions[0]
    elif mode == "SPOT_DCA":
        dca = calculate_dca_stats(config_id)
        if dca and dca.get("avg_cost", 0) > 0:
            position = {"side": "LONG", "entry_price": dca["avg_cost"], "amount": dca.get("total_qty", 0)}
            positions.append(position)

    pending_orders = []
    try:
        if mode == "REAL":
            current_side = (position or {}).get("side", "").upper()
            pending_orders = _fetch_real_open_orders(mt, symbol, current_side, positions)
        elif mode == "STRATEGY":
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT order_id, side, price, amount FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN' AND is_filled=0",
                (config_id, symbol),
            ).fetchall()
            conn.close()
            for row in rows:
                side = str(row["side"]).upper()
                pending_orders.append(
                    {
                        "price": float(row["price"]),
                        "side": side,
                        "amount": float(row["amount"]),
                        "order_id": row["order_id"],
                        "type": "open_long" if "BUY" in side else "open_short",
                    }
                )
        elif mode == "SPOT_DCA":
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT o.order_id, o.side, o.entry_price, o.amount
                FROM orders o LEFT JOIN spot_order_fills f ON o.order_id = f.order_id
                WHERE o.config_id=? AND o.trade_mode='SPOT_DCA' AND o.status='OPEN'
                  AND (f.status IS NULL OR f.status NOT IN ('FILLED','CANCELED'))
                """,
                (config_id,),
            ).fetchall()
            conn.close()
            for row in rows:
                pending_orders.append(
                    {
                        "price": float(row["entry_price"] or 0),
                        "side": "BUY",
                        "amount": float(row["amount"] or 0),
                        "order_id": row["order_id"],
                        "type": "buy_spot",
                    }
                )
    except Exception as exc:
        logger.warning(f"Kline pending orders fetch failed: {exc}")

    return {
        "candles": candles,
        "volume": volumes,
        "emas": emas,
        "orders": [],
        "position": position,
        "positions": positions,
        "pending_orders": pending_orders,
        "risk_lines": risk_lines,
    }
