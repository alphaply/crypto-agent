from backend import storage
import json
import time

import pandas as pd

from backend.config import config as global_config
from backend.database import delete_model_pricing, get_all_pricing, get_history_pnl_stats, save_trade_history, update_model_pricing
from backend.utils.indicators import calc_ema
from backend.utils.market_data import MarketTool

from backend.app.services.common import logger
from backend.app.services.dashboard_service import calculate_dca_stats
from backend.utils.spot_portfolio import get_config_symbols


def _local_management_projection(config_id: str, symbol: str, cfg: dict, account_scope: str | None = None) -> tuple[dict, dict]:
    """Read persisted enrollment evidence; never discover, restore or trade."""
    from backend.utils.management_policy import canonical_symbol
    from backend.utils.exit_orders import order_reference
    from backend.utils.position_management import management_snapshot
    from backend.database import get_db_conn
    canonical = canonical_symbol(symbol)
    policy = cfg.get('position_management_policy', 'visible_first_claim')
    projection = {'policy': policy, 'scopes': []}
    records = {}
    try:
        projection = management_snapshot(config_id, canonical)
        projection['policy'] = policy
        from backend.utils.position_management import management_status
        current = management_status(config_id, canonical)
        if policy == 'visible_first_claim' or current.get('scopes'):
            for claim in current.get('scopes', []):
                previous = next((s for s in projection['scopes'] if claim['side'] == 'BOTH' or s['side'] == claim['side']), {})
                claim.update({key: value for key, value in previous.items() if key not in claim or key in {
                    'source', 'sync_status', 'observed_at', 'baseline_contracts', 'contracts', 'changes', 'error'}})
            projection.update(current, policy='visible_first_claim')
        with get_db_conn() as conn:
            if account_scope and projection.get('policy') != 'visible_first_claim':
                valid_sides = {row[0] for row in conn.execute('SELECT side FROM position_management_scopes WHERE owner=%s AND symbol=%s AND account_scope=%s',
                                                            (config_id, canonical, account_scope))}
                projection['scopes'] = [scope for scope in projection['scopes'] if scope['side'] in valid_sides]
            if account_scope and projection.get('policy') == 'visible_first_claim':
                rows = conn.execute("SELECT payload FROM real_protection_plans WHERE payload::jsonb->>'account_scope'=%s", (account_scope,)).fetchall()
            else:
                rows = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=%s', (config_id,)).fetchall()
        for row in rows:
            plan = json.loads(row['payload'])
            if (canonical_symbol(plan['symbol']) != canonical or plan.get('state') == 'DONE'
                    or account_scope and plan.get('account_scope') != account_scope):
                continue
            for record in plan.get('entries', []) + plan.get('exits', []):
                if record.get('id'):
                    records[order_reference(record)] = (plan, record)
                if record.get('execution_order_id'):
                    records['regular:' + str(record['execution_order_id'])] = (plan, record)
    except storage.Error:
        raise
    except Exception as exc:
        projection.update(error=str(exc), complete=False)
    existing = {(scope['symbol'], scope['side']) for scope in projection['scopes']}
    if policy == 'exclusive_scope':
        for scope in cfg.get('management_scopes', []):
            if canonical_symbol(scope['symbol']) == canonical and (canonical, scope['side']) not in existing:
                projection['scopes'].append({'symbol': canonical, 'side': scope['side'], 'owner': config_id,
                    'sync_status': 'pending', 'source': None, 'observed_at': None})
    projection.setdefault('complete', all(scope.get('sync_status') == 'verified' for scope in projection['scopes']))
    projection['owner'] = config_id
    return projection, records


def _management_order_metadata(row: dict, records: dict, projection: dict) -> dict:
    """Capabilities describe supported tools, not permission or execution approval."""
    from backend.utils.exit_orders import ORDER_TYPES, canonical_order_type
    identity = row.get('qualified_order_id') or row.get('order_id')
    entry = records.get(identity)
    if entry is None and identity and ':' not in identity:
        matches = [value for key, value in records.items() if key.partition(':')[2] == str(identity)]
        entry = matches[0] if len(matches) == 1 else None
    side = str(row.get('pos_side') or row.get('side') or '').upper()
    if side in {'BOTH', 'NET'}:
        side = 'LONG' if str(row.get('side')).lower() == 'sell' else 'SHORT'
    facts = (projection.get('resource_states') or {}).get(side, {})
    if entry is None and facts:
        kind = canonical_order_type(row)
        terminal = row.get('status') in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
        supported = kind in ORDER_TYPES and kind != 'market' and not terminal
        return {**row, 'source': row.get('source') or 'exchange_snapshot',
                'management': facts, 'management_state': facts.get('state'), 'visible': facts.get('visible'),
                'eligible': facts.get('eligible'), 'can_claim': facts.get('can_claim'), 'owner': facts.get('owner'),
                'managed': facts.get('state') in {'owned', 'shared'},
                'supported_actions': (['cancel', 'edit'] if supported else ['cancel'] if identity and not terminal else []),
                'management_observed_at': None}
    if entry is None:
        return {**row, 'source': row.get('source'), 'managed': False, 'supported_actions': [],
                'management_observed_at': None}
    plan, record = entry
    management = plan.get('management') or {}
    supported = canonical_order_type(record) in ORDER_TYPES
    terminal = record.get('status') in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
    actions = ['cancel', 'edit'] if supported and not terminal and canonical_order_type(record) != 'market' else []
    return {**row, 'source': record.get('source', 'agent'), 'managed': facts.get('state') in {'owned', 'shared'} if facts else True,
            'management': facts or management, 'management_state': facts.get('state'),
            'visible': facts.get('visible'), 'eligible': facts.get('eligible'), 'can_claim': facts.get('can_claim'),
            'owner': facts.get('owner') if facts else management.get('owner') or projection.get('owner'), 'supported_actions': actions,
            'management_observed_at': management.get('observed_at'),
            'observed_at': row.get('observed_at') or record.get('fill_observed_at') or management.get('observed_at'),
            'sync_status': management.get('sync_status'),
            'change_source': record.get('last_change_source'),
            'pending': bool(record.get('fill_pending') or record.get('replacement') or record.get('amendment', {}).get('state') == 'pending')}


def _management_position_metadata(positions: list[dict], projection: dict) -> list[dict]:
    from backend.utils.trade_operations import _same_symbol
    result = []
    for position in positions:
        scope = next((scope for scope in projection.get('scopes', [])
                      if scope['side'] in {str(position.get('side', '')).upper(), 'BOTH', 'BASE'}
                      and (not position.get('symbol') or _same_symbol(scope['symbol'], position['symbol']))), None)
        facts = (projection.get('resource_states') or {}).get(str(position.get('side', '')).upper(), {})
        if facts:
            result.append({**position, 'management': facts, 'visible': facts.get('visible'),
                           'management_state': facts.get('state'), 'eligible': facts.get('eligible'),
                           'can_claim': facts.get('can_claim'), 'owner': facts.get('owner'),
                           'managed': facts.get('state') in {'owned', 'shared'},
                           'source': position.get('source') or (scope or {}).get('source') or 'exchange_snapshot',
                           'observed_at': position.get('observed_at') or (scope or {}).get('observed_at'), 'supported_actions': ['close']})
        elif scope is None:
            result.append(position)
        else:
            result.append({**position, 'source': scope.get('source'), 'managed': scope.get('sync_status') == 'verified',
                           'owner': scope.get('owner'), 'observed_at': scope.get('observed_at'),
                           'supported_actions': ['close'], 'sync_status': scope.get('sync_status')})
    return result


def get_token_stats_payload():
    from backend.database.usage import token_stats
    return token_stats()


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
    from backend.database import get_financial_statistics_snapshot

    snapshot = get_financial_statistics_snapshot(symbol)
    balances = snapshot["balance_history"]
    totals = snapshot["totals"]
    total = totals["total_trades"]
    # This endpoint historically includes flat fills in the win-rate denominator.
    win_rate = totals["win_count"] / total * 100 if total else 0
    latest_equity = balances[-1]["total_equity"] if balances else 0
    latest_balance = balances[-1]["total_balance"] if balances else 0
    return {
        "balance_history": balances,
        "daily_equity": snapshot["daily_equity"],
        "summary": {
            "total_trades": total,
            "total_pnl": round(totals["total_pnl"], 2),
            "win_rate": round(win_rate, 2),
            "win_count": totals["win_count"],
            "lose_count": totals["lose_count"],
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
    if total_decided > 0:
        trade_summary["win_rate"] = round(trade_summary["win_count"] / total_decided * 100, 1)
    elif trade_summary.get("total_trades", 0) > 0:
        trade_summary["win_rate"] = None
    else:
        trade_summary["win_rate"] = 0
    trade_summary["realized_pnl"] = round(trade_summary["realized_pnl"], 4)
    return trade_summary


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except storage.Error:
        raise
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


def _owned_real_trades(mt, symbol: str, config_id: str | None, trades: list[dict]) -> list[dict]:
    """Account trade responses need order ownership before task attribution."""
    from backend.utils.order_ownership import assert_owned_perpetual_order, symbol_aliases

    if not config_id:
        return []
    aliases = set(symbol_aliases(mt, symbol))
    ownership = {}
    owned = []
    for trade in trades:
        if str(trade.get('symbol') or '') not in aliases:
            continue
        order_id = str(trade.get('order') or trade.get('order_id') or '')
        if not order_id:
            continue
        if order_id not in ownership:
            try:
                assert_owned_perpetual_order(mt, symbol, order_id, config_id)
                ownership[order_id] = True
            except ValueError:
                ownership[order_id] = False
        if ownership[order_id]:
            owned.append(trade)
    return owned


def _fetch_real_position_data(mt, symbol, cfg):
    positions = []
    balance = 0
    recent_trades = []
    fetch_errors = []
    trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0, "lose_count": 0, "win_rate": 0}
    fallback_leverage = global_config.get_leverage(cfg.get("config_id") if isinstance(cfg, dict) else None)

    active_plans = {}
    config_id = cfg.get("config_id") if isinstance(cfg, dict) else None
    if config_id:
        try:
            from backend.database import get_db_conn
            with get_db_conn() as conn:
                rows = conn.execute(
                    "SELECT payload FROM real_protection_plans WHERE config_id=%s",
                    (config_id,)
                ).fetchall()
                for r in rows:
                    p = json.loads(r["payload"])
                    if p.get("state") != "DONE" and str(p.get('symbol', '')).split(':')[0] == symbol.split(':')[0]:
                        active_plans[p.get("side")] = p
        except storage.Error:
            raise
        except Exception as exc:
            logger.warning(f"Failed to load active protection plans for {config_id}: {exc}")

    try:
        all_positions = mt.exchange.fetch_positions([symbol])
        for position in all_positions:
            contracts = float(position.get("contracts", 0))
            if contracts <= 0:
                continue
            entry = float(position.get("entryPrice", 0))
            unrealized = float(position.get("unrealizedPnl", 0))
            base_quantity = contracts * float(position.get('contractSize') or 1)
            notional = float(position.get("notional", 0)) or (entry * base_quantity)
            leverage = _resolve_leverage(position, cfg, fallback_leverage)
            pnl_pct = (unrealized / abs(notional) * 100) if notional != 0 else 0
            roi_pct = pnl_pct * leverage
            margin_used = abs(notional) / leverage if leverage > 0 else abs(notional)
            pos_side = str(position.get("side", "")).upper()
            plan = active_plans.get(pos_side) or {}
            positions.append(
                {
                    "symbol": position.get("symbol", symbol),
                    "side": pos_side,
                    "contracts": contracts,
                    "qty": base_quantity,
                    "amount": base_quantity,
                    "entry_price": entry,
                    "mark_price": float(position.get("markPrice", 0)),
                    "unrealized_pnl": round(unrealized, 4),
                    "pnl_pct": round(pnl_pct, 2),
                    "roi_pct": round(roi_pct, 2),
                    "leverage": leverage,
                    "notional": round(abs(notional), 2),
                    "margin_used": round(margin_used, 2),
                    "take_profit": plan.get("take_profit"),
                    "stop_loss": plan.get("stop_loss"),
                    "protection_state": plan.get("state"),
                    "protection_revision": plan.get("revision", 0),
                    "protection_verified_at": plan.get("verified_at"),
                    "protection_error": plan.get("error"),
                }
            )
    except storage.Error:
        raise
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
    except storage.Error:
        raise
    except Exception as exc:
        fetch_errors.append(f"fetch_balance_failed({type(exc).__name__}): {exc}")
        logger.error(f"REAL balance fetch failed config_id={cfg.get('config_id')} symbol={symbol}: {exc}", exc_info=True)

    try:
        raw_trades = mt.exchange.fetch_my_trades(symbol, limit=100)
        raw_trades = _owned_real_trades(mt, symbol, config_id, raw_trades)
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
    except storage.Error:
        raise
    except Exception as exc:
        logger.warning(f"Fetch trades error config_id={cfg.get('config_id')} symbol={symbol}: {exc}")

    try:
        pnl_stats = get_history_pnl_stats(symbol, cfg.get("config_id"))
        if pnl_stats:
            trade_summary["win_count"] = pnl_stats.get("win_count", 0)
            trade_summary["lose_count"] = pnl_stats.get("lose_count", 0)
            trade_summary["total_trades"] = pnl_stats.get("total_trades", 0)
            trade_summary["realized_pnl"] = pnl_stats.get("total_pnl", 0)
    except storage.Error:
        raise
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
    except storage.Error:
        raise
    except Exception as exc:
        logger.warning(f"Fetch ticker error in STRATEGY mode: {exc}")

    mock_account = get_mock_account(config_id, symbol)
    balance = mock_account.get("balance", 10000.0)

    with get_db_conn() as conn:
        cursor = conn.cursor()
        open_mocks = cursor.execute(
            "SELECT * FROM mock_orders WHERE config_id=%s AND symbol=%s AND status='OPEN'",
            (config_id, symbol),
        ).fetchall()
        from backend.utils.exit_policy import effective_exit_mode
        if effective_exit_mode(cfg) == 'independent_exits':
            from backend.database.independent import MockIndependentTrading
            open_mocks = [{**row, 'stop_loss': None, 'take_profit': None}
                          for row in MockIndependentTrading(config_id, symbol).snapshot()['positions']]
        for order in open_mocks:
            if not int(order["is_filled"] or 0):
                continue
            entry = float(order["price"])
            amount = float(order["amount"])
            side = str(order["side"]).upper()
            unrealized = 0
            if current_price > 0:
                if "BUY" in side or side == "LONG":
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
                    "side": "LONG" if "BUY" in side or side == "LONG" else "SHORT",
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
                    "take_profit": float(order["take_profit"]) if order["take_profit"] is not None else None,
                    "stop_loss": float(order["stop_loss"]) if order["stop_loss"] is not None else None,
                    "protection_state": "ACTIVE" if (order["take_profit"] is not None or order["stop_loss"] is not None) else None,
                }
            )

        closed_mocks = cursor.execute(
            "SELECT * FROM mock_orders WHERE config_id=%s AND symbol=%s AND status='CLOSED' AND realized_pnl IS NOT NULL",
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
        if effective_exit_mode(cfg) == 'independent_exits':
            history = cursor.execute("SELECT * FROM position_history WHERE config_id=%s AND symbol=%s AND source='mock_aggregate'", (config_id, symbol)).fetchall()
            trade_summary['realized_pnl'] += sum(float(r['realized_pnl'] or 0) for r in history)
            closed = [r for r in history if r['status'] == 'CLOSED']
            trade_summary['total_trades'] += len(closed)
            trade_summary['win_count'] += sum(float(r['realized_pnl'] or 0) > 0 for r in closed)
            trade_summary['lose_count'] += sum(float(r['realized_pnl'] or 0) < 0 for r in closed)
            _calculate_win_rate(trade_summary)

    return positions, balance, recent_trades, trade_summary


def get_position_stats_payload(config_id: str):
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")

    mode = str(cfg.get("mode", "STRATEGY")).upper()
    symbol = cfg.get("symbol")
    if not symbol:
        raise ValueError(f"Config {config_id} is missing symbol")
    if mode == "SPOT_DCA":
        dca_stats = calculate_dca_stats(config_id)
        positions = []
        symbol_stats = (dca_stats.get('by_symbol') or [dca_stats]) if dca_stats else []
        for item in symbol_stats:
            if float(item.get('total_qty') or 0) <= 0:
                continue
            positions.append(
                {
                    'symbol': item.get('symbol', symbol),
                    "side": "LONG",
                    "entry_price": item.get("avg_cost", 0),
                    "mark_price": item.get("current_price", 0),
                    "amount": item.get("total_qty", 0),
                    "qty": item.get("total_qty", 0),
                    "unrealized_pnl": item.get("unrealized_pnl", 0),
                    "roi_pct": item.get("return_pct", 0),
                }
            )
        return {
            "mode": mode,
            "positions": positions,
            "balance": dca_stats.get("market_value") if dca_stats else None,
            "margin_balance": dca_stats.get("market_value") if dca_stats else None,
            "unrealized_pnl": dca_stats.get("unrealized_pnl") if dca_stats else None,
            "summary": None,
            "dca_stats": dca_stats,
            "errors": (dca_stats.get('errors', []) +
                       [f'{item} 统计暂不可用' for item in dca_stats.get('missing_symbols', [])])
                      if dca_stats else ['现货组合统计暂不可用'],
        }

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
    balance_complete = not any('fetch_balance_failed' in str(error) for error in fetch_errors)
    position_complete = not any('fetch_positions_failed' in str(error) for error in fetch_errors)
    from backend.utils.exit_policy import effective_exit_mode
    exit_management = {'mode': effective_exit_mode(cfg), 'exits': [], 'uncovered': {'LONG': 0, 'SHORT': 0}, 'pending': False}
    if exit_management['mode'] == 'independent_exits':
        try:
            if mode == 'REAL':
                from backend.utils.independent_exits import IndependentExits
                exit_management = IndependentExits(mt).snapshot(symbol)
            else:
                from backend.database.independent import MockIndependentTrading
                exit_management = MockIndependentTrading(config_id, symbol).snapshot()
                exit_management['mode'] = 'independent_exits'
        except storage.Error:
            raise
        except Exception as exc:
            exit_management.update(pending=True, error=str(exc), uncovered={'LONG': None, 'SHORT': None})

    management = {'policy': 'strict_owned', 'scopes': []}
    if mode == 'REAL':
        from backend.utils.execution_ledger import account_scope
        from backend.utils.position_management import resource_management_status
        management, managed_records = _local_management_projection(config_id, symbol, cfg, account_scope(mt.exchange, config_id))
        try:
            management['resource_states'] = {side: resource_management_status(mt, symbol, side) for side in ('LONG', 'SHORT')}
        except storage.Error:
            raise
        except Exception as exc:
            management.update(complete=False, error=str(exc))
        positions = _management_position_metadata(positions, management)
        for collection in ('entries', 'exits'):
            exit_management[collection] = [_management_order_metadata(row, managed_records, management)
                                           for row in exit_management.get(collection, [])]

    return {
        "mode": mode,
        "positions": positions,
        "balance": round(balance, 2) if balance_complete else None,
        "margin_balance": round(margin_balance, 2) if balance_complete else None,
        "unrealized_pnl": round(unrealized_total, 4) if position_complete else None,
        "position_complete": position_complete,
        "balance_complete": balance_complete,
        "recent_trades": recent_trades,
        "summary": trade_summary,
        "errors": fetch_errors,
        "exit_management": exit_management,
        "management": management,
    }


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
    first = float(points[0]["equity"] or 0)
    latest = float(points[-1]["equity"] or 0)
    return {
        "data_state": "ready",
        "point_count": len(points),
        "first_date": points[0]["date"],
        "last_date": points[-1]["date"],
        "latest_equity": latest,
        "return_pct": ((latest - first) / first * 100) if first else None,
    }


def get_equity_compare_payload(symbol: str | None = None, config_ids: str = ""):
    configs = [
        cfg
        for cfg in global_config.get_all_symbol_configs()
        if (not symbol or symbol in get_config_symbols(cfg))
        and cfg.get("enabled", True)
        and str(cfg.get("mode") or "STRATEGY").upper() in {"REAL", "STRATEGY", "SPOT_DCA"}
    ]
    if config_ids:
        wanted = {item.strip() for item in config_ids.split(",") if item.strip()}
        configs = [cfg for cfg in configs if cfg.get("config_id") in wanted]
    import hashlib
    grouped_configs = []
    real_groups = {}
    for cfg in configs:
        if not symbol and str(cfg.get('mode') or '').upper() == 'REAL':
            exchange = cfg.get('exchange') or 'binance'
            key = cfg.get('api_key') or cfg.get('binance_api_key') or cfg.get('okx_api_key')
            try:
                exchange, key, _, _ = global_config.get_exchange_credentials(config_id=cfg['config_id'])
            except (AttributeError, KeyError):
                pass
            identity = hashlib.sha256(f"{exchange}:{cfg.get('market_type') or 'swap'}:{key or cfg['config_id']}".encode()).hexdigest()
            if identity in real_groups:
                real_groups[identity]['_config_ids'].append(cfg['config_id'])
                continue
            item = {**cfg, '_config_ids': [cfg['config_id']], '_account_scope': identity}
            real_groups[identity] = item
        else:
            item = {**cfg, '_config_ids': [cfg['config_id']]}
        grouped_configs.append(item)

    with storage.get_connection() as conn:

        cursor = conn.cursor()
        series = []
        for cfg in grouped_configs:
            config_id = cfg.get("config_id")
            mode = str(cfg.get("mode") or "STRATEGY").upper()
            display_name = cfg.get("title") or cfg.get("display_name") or config_id
            label = f"{display_name} ({mode})"
            points = []
            data_source = None
            selected_symbol = symbol or cfg.get('symbol') or next(iter(get_config_symbols(cfg)), '')
            if mode == "REAL":
                ids = cfg['_config_ids']
                id_placeholders = ','.join('%s' for _ in ids)
                symbol_clause = ' AND symbol = %s' if symbol else ''
                rows = cursor.execute(
                    f"""
                    SELECT day, total_equity FROM (
                        SELECT substring(timestamp from 1 for 10) as day, total_equity,
                               row_number() OVER (PARTITION BY substring(timestamp from 1 for 10) ORDER BY timestamp DESC NULLS LAST) as rn
                        FROM balance_history
                        WHERE config_id IN ({id_placeholders}){symbol_clause} AND total_equity > 0
                    ) WHERE rn = 1 ORDER BY day ASC
                    """,
                    [*ids, *([symbol] if symbol else [])],
                ).fetchall()
                points = [{"date": row["day"], "equity": row["total_equity"]} for row in rows]
                data_source = {
                    "table": "balance_history",
                    "field": "total_equity",
                    "scope": "account" if not symbol else "config_id",
                    "config_id": config_id,
                    "symbol": symbol,
                    "label": "balance_history.total_equity",
                    "display_label": "实盘账户权益快照",
                    "kind": "real_equity",
                }
            elif mode == "STRATEGY":
                rows = cursor.execute(
                    """
                    SELECT h.day as day, COALESCE(h.total_equity, h.balance) as equity FROM (
                        SELECT substring(timestamp from 1 for 10) as day,
                               total_equity, balance, timestamp,
                               row_number() OVER (PARTITION BY substring(timestamp from 1 for 10) ORDER BY id DESC) as rn
                        FROM mock_balance_history
                        WHERE config_id = %s AND symbol = %s AND COALESCE(total_equity, balance) > 0
                    ) h WHERE h.rn = 1 ORDER BY h.day ASC
                    """,
                    (config_id, selected_symbol),
                ).fetchall()
                points = [{"date": row["day"], "equity": row["equity"]} for row in rows]
                data_source = {
                    "table": "mock_balance_history",
                    "field": "total_equity",
                    "fallback_field": "balance",
                    "scope": "config_id",
                    "config_id": config_id,
                    "symbol": selected_symbol,
                    "label": "mock_balance_history.total_equity",
                    "display_label": "策略模拟权益快照",
                    "kind": "strategy_equity",
                }
            elif mode == "SPOT_DCA":
                target_symbols = [symbol] if symbol else get_config_symbols(cfg)
                placeholders = ','.join('%s' for _ in target_symbols)
                rows = cursor.execute(
                    f"""
                    SELECT snapshot_date AS day, SUM(total_invested) AS equity
                    FROM dca_daily_snapshots
                    WHERE config_id = %s AND symbol IN ({placeholders}) AND total_invested > 0
                    GROUP BY snapshot_date
                    ORDER BY snapshot_date ASC
                    """,
                    (config_id, *target_symbols),
                ).fetchall()
                points = [{"date": row["day"], "equity": row["equity"]} for row in rows]
                data_source = {
                    "table": "dca_daily_snapshots",
                    "field": "total_invested",
                    "scope": "config_id",
                    "config_id": config_id,
                    "symbol": symbol,
                    "label": "dca_daily_snapshots.total_invested",
                    "display_label": "定投累计投入快照",
                    "kind": "dca_invested",
                    "symbols": target_symbols,
                }

            series.append(
                {
                    "config_id": config_id,
                    "config_ids": cfg['_config_ids'],
                    "account_scope": cfg.get('_account_scope'),
                    "symbol": selected_symbol if mode != 'SPOT_DCA' else symbol,
                    "label": label,
                    "mode": mode,
                    "points": points,
                    "data_source": data_source,
                    **_equity_series_metadata(points),
                }
            )

    return {"symbol": symbol, "series": series, "groups": [
        {'mode': mode, 'series': [item for item in series if item['mode'] == mode]}
        for mode in ('REAL', 'STRATEGY', 'SPOT_DCA')
    ]}


_KLINE_ALLOWED_TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"}


def _load_order_reasons(config_id: str, order_ids: list[str]) -> dict[str, str]:
    unique_ids = [str(order_id) for order_id in dict.fromkeys(order_ids) if str(order_id or "").strip()]
    if not config_id or not unique_ids:
        return {}

    placeholders = ",".join("%s" for _ in unique_ids)
    with storage.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT order_id, reason
            FROM orders
            WHERE config_id = %s
              AND order_id IN ({placeholders})
              AND COALESCE(reason, '') != ''
            ORDER BY id DESC
            """,
            (config_id, *unique_ids),
        ).fetchall()

    reasons: dict[str, str] = {}
    for row in rows:
        order_id = str(row["order_id"] or "")
        if order_id and order_id not in reasons:
            reasons[order_id] = str(row["reason"] or "")
    return reasons


def _normalize_real_open_order(order: dict, current_side: str, positions: list[dict], reason: str = "") -> dict:
    info = order.get("info", {}) or {}
    side = str(order.get("side", "")).upper()
    raw_type = str(order.get("type") or info.get("type") or "").upper()
    pos_side = str(info.get("positionSide") or info.get("posSide") or order.get("pos_side") or "BOTH").upper()
    close_all = str(info.get('closePosition', '')).lower() == 'true' or str(info.get('closeFraction', '')) == '1'
    native_reduce = any(str(value).lower() == 'true' for value in (order.get('reduceOnly'), info.get('reduceOnly')))
    if pos_side in {'BOTH', 'NET'} and (close_all or native_reduce):
        pos_side = 'LONG' if side == 'SELL' else 'SHORT'
    price = _safe_float(order.get("price"), 0)
    stop_price = _safe_float(order.get("triggerPrice") or order.get("stopPrice") or info.get("stopPrice") or info.get("triggerPrice"), 0)
    if price <= 0 and stop_price > 0:
        price = stop_price
    amount = _safe_float(order.get("amount") or info.get("origQty") or info.get("qty"), 0)
    if amount <= 0 and close_all:
        for position in positions:
            if str(position.get("side", "")).upper() == pos_side:
                amount = _safe_float(position.get("amount") or position.get("qty") or position.get("contracts"), 0)
                break

    reduce_only = any(str(value).lower() == 'true' for value in (
        order.get('reduceOnly'), order.get('reduce_only'), info.get('reduceOnly'), info.get('closePosition')))
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
        "status": str(order.get("status") or info.get("status") or "OPEN").upper(),
        "reason": reason or "",
        "quantity_mode": 'all' if close_all else 'fixed',
    }


def _fetch_real_open_orders(mt: MarketTool, symbol: str, current_side: str, positions: list[dict], config_id: str,
                            *, errors: list | None = None, qualify_ids: bool = False) -> list[dict]:
    from backend.utils.position_adoption import OKX_ALGO_ORDER_TYPES
    channels = [('regular', {})]
    channels += ([('algo', {'trigger': True, 'ordType': kind}) for kind in OKX_ALGO_ORDER_TYPES]
                 if getattr(mt.exchange, 'id', None) == 'okx' else [('algo', {'trigger': True})])
    seen, raw_pending_orders = set(), []
    for channel, params in channels:
        try:
            rows = mt.exchange.fetch_open_orders(symbol, params=params)
            if not isinstance(rows, list):
                raise ValueError('Order channel is unavailable')
        except storage.Error:
            raise
        except Exception as exc:
            failure = f"orders:{channel}:{params.get('ordType', 'default')}:{type(exc).__name__}"
            logger.warning(f'Kline order channel unavailable: {failure}')
            if errors is not None:
                errors.append(failure)
            continue
        observed_at = time.time()
        for order in rows:
            order_id = str(order.get('id') or '')
            identity = f"{channel}:{order_id}" if order_id else f"{channel}:{order.get('type')}:{order.get('side')}:{order.get('price')}:{order.get('amount')}"
            if identity in seen:
                continue
            seen.add(identity)
            raw_pending_orders.append((order, channel, observed_at))
    reason_by_order_id = _load_order_reasons(config_id, [str(order.get('id') or '') for order, _, _ in raw_pending_orders])
    result = []
    for order, channel, observed_at in raw_pending_orders:
        row = _normalize_real_open_order(order, current_side, positions, reason_by_order_id.get(str(order.get('id') or ''), ''))
        market = mt.exchange.market(symbol) if hasattr(mt.exchange, 'market') else {}
        size = float(market.get('contractSize') or 1) if market.get('contract') else 1
        row['contracts'] = order.get('amount')
        row['filled'] = float(order['filled']) * size if order.get('filled') is not None else None
        row['remaining'] = float(order['remaining']) * size if order.get('remaining') is not None else None
        if row['quantity_mode'] == 'all':
            row['coverage_estimate'] = row['amount']
            row['amount'] = None
        else:
            row['amount'] *= size
        row.update(exchange_channel=channel, qualified_order_id=f"{channel}:{row['order_id']}", observed_at=observed_at)
        if qualify_ids:
            row['exchange_order_id'] = row['order_id']
            row['order_id'] = row['qualified_order_id']
        result.append(row)
    return result


def get_kline_payload(config_id: str, timeframe: str = "1h", symbol: str | None = None):
    if timeframe not in _KLINE_ALLOWED_TF:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")
    symbol = symbol or cfg.get("symbol")
    if symbol not in get_config_symbols(cfg):
        raise ValueError(f'Symbol {symbol} is not configured for {config_id}')
    mode = str(cfg.get("mode") or "STRATEGY").upper()
    mt = MarketTool(config_id=config_id)
    fetch_limit = 260 if timeframe in {"1w", "1M"} else (360 if timeframe == "1d" else 500)
    raw = mt.exchange.fetch_ohlcv(symbol, timeframe, limit=fetch_limit)
    if not raw:
        return {
            'symbol': symbol,
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
    position_complete = True
    order_errors = []
    risk_lines = []
    if mode == "REAL":
        active_plans = {}
        if config_id:
            try:
                from backend.database import get_db_conn
                with get_db_conn() as conn:
                    rows = conn.execute(
                        "SELECT payload FROM real_protection_plans WHERE config_id=%s",
                        (config_id,)
                    ).fetchall()
                    for r in rows:
                        p = json.loads(r["payload"])
                        if p.get("state") != "DONE" and str(p.get('symbol', '')).split(':')[0] == symbol.split(':')[0]:
                            active_plans[p.get("side")] = p
            except storage.Error:
                raise
            except Exception:
                pass
        try:
            all_positions = mt.exchange.fetch_positions([symbol])
            for current in all_positions:
                if float(current.get("contracts", 0)) > 0:
                    pos_side = str(current.get("side", "")).upper()
                    plan = active_plans.get(pos_side) or {}
                    payload = {
                        "side": pos_side,
                        "entry_price": float(current.get("entryPrice", 0)),
                        "contracts": float(current.get("contracts", 0)),
                        "amount": float(current.get("contracts", 0)) * float(current.get('contractSize') or 1),
                        "mark_price": float(current.get("markPrice", 0) or 0),
                        "unrealized_pnl": round(float(current.get("unrealizedPnl", 0) or 0), 4),
                        "take_profit": plan.get("take_profit"),
                        "stop_loss": plan.get("stop_loss"),
                        "protection_state": plan.get("state"),
                        "protection_revision": plan.get("revision", 0),
                        "protection_verified_at": plan.get("verified_at"),
                        "protection_error": plan.get("error"),
                        "symbol": current.get('symbol', symbol),
                    }
                    positions.append(payload)
            if positions:
                position = positions[0]
        except storage.Error:
            raise
        except Exception as exc:
            position_complete = False
            logger.warning(f"Kline positions fetch failed: {exc}")
    elif mode == "STRATEGY":
        with storage.get_connection() as conn:

            rows = conn.execute(
                "SELECT side, price, amount, stop_loss, take_profit, order_id FROM mock_orders WHERE config_id=%s AND symbol=%s AND status='OPEN' AND is_filled=1 ORDER BY timestamp ASC",
                (config_id, symbol),
            ).fetchall()
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
        dca = calculate_dca_stats(config_id, symbol=symbol)
        if dca and dca.get("avg_cost", 0) > 0:
            position = {
                'symbol': symbol,
                "side": "LONG",
                "entry_price": dca["avg_cost"],
                "mark_price": dca.get("current_price", 0),
                "amount": dca.get("total_qty", 0),
                "unrealized_pnl": dca.get("unrealized_pnl", 0),
                "roi_pct": dca.get("return_pct", 0),
            }
            positions.append(position)

    pending_orders = []
    try:
        if mode == "REAL":
            current_side = (position or {}).get("side", "").upper()
            pending_orders = _fetch_real_open_orders(mt, symbol, current_side, positions, config_id, errors=order_errors,
                                                    qualify_ids=cfg.get('position_management_policy') == 'exclusive_scope')
        elif mode == "STRATEGY":
            with storage.get_connection() as conn:

                rows = conn.execute(
                    "SELECT order_id, side, price, amount, stop_loss, take_profit, status FROM mock_orders WHERE config_id=%s AND symbol=%s AND status='OPEN' AND is_filled=0",
                    (config_id, symbol),
                ).fetchall()
            for row in rows:
                side = str(row["side"]).upper()
                pending_orders.append(
                    {
                        "price": float(row["price"]),
                        "side": side,
                        "amount": float(row["amount"]),
                        "order_id": row["order_id"],
                        "type": "open_long" if "BUY" in side else "open_short",
                        "take_profit": float(row["take_profit"] or 0),
                        "stop_loss": float(row["stop_loss"] or 0),
                        "status": row["status"] or "OPEN",
                    }
                )
        elif mode == "SPOT_DCA":
            with storage.get_connection() as conn:

                rows = conn.execute(
                    """
                    SELECT DISTINCT o.order_id, o.side, o.entry_price,
                        GREATEST(0, COALESCE(o.amount,0)-COALESCE(f.filled_qty,o.filled_amount,0)) AS amount,
                        COALESCE(f.status,o.status) AS status
                    FROM orders o LEFT JOIN spot_order_fills f ON o.order_id = f.order_id
                        AND f.config_id = o.config_id AND f.symbol = o.symbol
                    WHERE o.config_id=%s AND o.trade_mode='SPOT_DCA' AND o.status IN ('OPEN', 'PARTIAL')
                      AND o.symbol = %s
                      AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
                      AND UPPER(o.side) IN ('BUY','BUY_LIMIT')
                      AND (f.status IS NULL OR UPPER(f.status) NOT IN ('FILLED','CLOSED','CANCELED','CANCELLED','EXPIRED','REJECTED'))
                    """,
                    (config_id, symbol),
                ).fetchall()
            for row in rows:
                pending_orders.append(
                    {
                        "price": float(row["entry_price"] or 0),
                        "side": "BUY",
                        "amount": float(row["amount"] or 0),
                        "order_id": row["order_id"],
                        "type": "buy_spot",
                        "status": row["status"] or "OPEN",
                    }
                )
    except storage.Error:
        raise
    except Exception as exc:
        order_errors.append(f"orders:{type(exc).__name__}")
        logger.warning(f"Kline pending orders fetch failed: {exc}")

    from backend.utils.exit_policy import effective_exit_mode
    if mode in {'REAL', 'STRATEGY'} and effective_exit_mode(cfg) == 'independent_exits':
        try:
            if mode == 'STRATEGY':
                from backend.database.independent import MockIndependentTrading
                snapshot = MockIndependentTrading(config_id, symbol).snapshot()
                positions = [{**p, 'side': p['pos_side'], 'entry_price': p['price'],
                              'mark_price': float(candles[-1]['close']) if candles else 0}
                             for p in snapshot['positions']]
                position = positions[0] if positions else None
            else:
                from backend.utils.independent_exits import IndependentExits
                snapshot = IndependentExits(mt).snapshot(symbol)
            from backend.utils.exit_context import independent_exit_chart_lines
            risk_lines = independent_exit_chart_lines(snapshot)
        except storage.Error:
            raise
        except Exception as exc:
            logger.warning(f'Independent exit chart refresh failed: {exc}')

    management = {'policy': 'strict_owned', 'scopes': []}
    if mode == 'REAL':
        from backend.utils.execution_ledger import account_scope
        management, managed_records = _local_management_projection(config_id, symbol, cfg, account_scope(mt.exchange, config_id))
        from backend.utils.position_management import resource_management_status
        try:
            management['resource_states'] = {side: resource_management_status(mt, symbol, side) for side in ('LONG', 'SHORT')}
        except storage.Error:
            raise
        except Exception as exc:
            management.update(complete=False, error=str(exc))
        positions = _management_position_metadata(positions, management)
        position = positions[0] if positions else None
        pending_orders = [_management_order_metadata(row, managed_records, management) for row in pending_orders]

    return {
        'symbol': symbol,
        "management": management,
        "position_complete": position_complete,
        "orders_complete": not order_errors,
        "errors": order_errors,
        "candles": candles,
        "volume": volumes,
        "emas": emas,
        "orders": [],
        "position": position,
        "positions": positions,
        "pending_orders": pending_orders,
        "risk_lines": risk_lines,
    }


def update_position_protection_payload(
    config_id: str,
    symbol: str,
    side: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    clear_stop_loss: bool = False,
    clear_take_profit: bool = False,
    expected_revision: int | None = None,
) -> dict:
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")

    from backend.utils.exit_policy import effective_exit_mode
    if effective_exit_mode(cfg) == 'independent_exits':
        raise ValueError('独立退出模式请修改各档退出单，不使用整仓 TP/SL')

    mode = str(cfg.get("mode", "STRATEGY")).upper()
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")
    import math
    for value in (stop_loss, take_profit):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('TP/SL 必须是有限正数')
    if (clear_stop_loss and stop_loss is not None) or (clear_take_profit and take_profit is not None):
        raise ValueError('不能同时修改和取消同一项保护')

    if mode == "REAL":
        from backend.utils.market_data import MarketTool
        from backend.utils.position_protection import PositionProtection

        mt = MarketTool(config_id=config_id)
        protection = PositionProtection(mt)
        revision_args = {'expected_revision': expected_revision} if expected_revision is not None else {}
        plan = protection.adjust(
            symbol=symbol,
            side=side,
            sl=stop_loss,
            tp=take_profit,
            clear_sl=clear_stop_loss,
            clear_tp=clear_take_profit,
            **revision_args,
        )
        return {
            "config_id": config_id,
            "symbol": symbol,
            "side": side,
            "stop_loss": plan.get("stop_loss"),
            "take_profit": plan.get("take_profit"),
            "state": plan.get("state"),
            "error": plan.get("error"),
            "revision": plan.get("revision"),
            "verified_at": plan.get("verified_at"),
        }
    elif mode == "STRATEGY":
        from backend.database import get_db_conn

        target_side_pattern = "%BUY%" if side == "LONG" else "%SELL%"
        with get_db_conn() as conn:
            cursor = conn.cursor()
            open_orders = cursor.execute(
                "SELECT * FROM mock_orders WHERE config_id=%s AND symbol=%s AND status='OPEN' AND is_filled=1 AND UPPER(side) LIKE %s",
                (config_id, symbol, target_side_pattern),
            ).fetchall()
            if not open_orders:
                raise ValueError(f"No open position found for {side} {symbol}")

            for order in open_orders:
                new_sl = None if clear_stop_loss else (stop_loss if stop_loss is not None else order["stop_loss"])
                new_tp = None if clear_take_profit else (take_profit if take_profit is not None else order["take_profit"])
                cursor.execute(
                    "UPDATE mock_orders SET stop_loss=%s, take_profit=%s WHERE order_id=%s",
                    (new_sl, new_tp, order["order_id"]),
                )
            conn.commit()

        return {
            "config_id": config_id,
            "symbol": symbol,
            "side": side,
            "stop_loss": None if clear_stop_loss else stop_loss,
            "take_profit": None if clear_take_profit else take_profit,
            "state": "ACTIVE",
            "error": None,
        }
    else:
        raise ValueError(f"Mode {mode} does not support position protection adjustment")

