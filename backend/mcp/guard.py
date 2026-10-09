"""MCP-only exposure gate, evaluated before entry/protection side effects."""
from __future__ import annotations

import math
import json


def perpetual_symbol(exchange, symbol):
    market = exchange.market(symbol)
    if not market.get('contract') and ':' not in symbol:
        market = exchange.market(f'{symbol}:USDT')
    if market.get('contract') is False or market.get('linear') is False:
        raise ValueError('MCP perpetual profile requires a linear contract')
    return market['symbol']


def assert_position_owner(market_tool, symbol):
    """Fail closed on shared or untracked perpetual exposure, using read-only evidence."""
    config_id = str(getattr(market_tool, 'config_id', '') or '')
    if not config_id.startswith('mcp:'):
        return
    from backend import database
    from backend.utils.execution_ledger import account_scope
    from backend.utils.order_ownership import symbol_aliases
    exchange = market_tool.exchange
    if getattr(market_tool, 'market_type', None) == 'spot':
        return
    aliases = symbol_aliases(market_tool, symbol)
    scope = account_scope(exchange, config_id)
    plans = []
    links = {}
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        placeholders = ','.join('?' for _ in aliases)
        if 'real_protection_plans' in tables:
            for row in conn.execute(f'SELECT config_id,payload FROM real_protection_plans WHERE symbol IN ({placeholders})', aliases):
                plan = json.loads(row['payload'])
                if plan.get('account_scope') == scope and plan.get('state') != 'DONE':
                    if row['config_id'] != config_id:
                        raise ValueError('Another strategy owns this account position; MCP cannot modify shared exposure')
                    plans.append(plan)
        if 'execution_order_links' in tables:
            for row in conn.execute(f'SELECT order_id,config_id,role,payload FROM execution_order_links WHERE account_scope=? AND symbol IN ({placeholders})', (scope, *aliases)):
                links[str(row['order_id'])] = {'config_id': row['config_id'], 'role': row['role'], **json.loads(row['payload'])}
    for plan in plans:
        for role, records in (('entry', plan.get('entries', [])), ('exit', [*plan.get('legs', []), *plan.get('exits', []), *([plan['exit_order']] if plan.get('exit_order') else [])])):
            for record in records:
                for order_id in (record.get('id'), record.get('execution_order_id')):
                    if order_id:
                        links.setdefault(str(order_id), {'config_id': config_id, 'role': role, 'side': plan['side']})
    canonical = perpetual_symbol(exchange, symbol)
    positions = [row for row in exchange.fetch_positions([canonical]) if abs(float(row.get('contracts') or 0)) > 0]
    if not positions:
        return
    owned_sides = {str(plan.get('side')).upper() for plan in plans}
    if any(str(row.get('side')).upper() not in owned_sides for row in positions):
        raise ValueError('Untracked exchange position: MCP cannot establish sole ownership')
    starts = [float(entry['created_at']) for plan in plans for entry in plan.get('entries', []) if entry.get('created_at')]
    if not starts:
        raise ValueError('Position ownership has no verifiable entry history')
    # Complete fills since the current position cycle distinguish owned quantity
    # from manual or another strategy's additions to the same exchange position.
    trades = exchange.fetch_my_trades(canonical, since=int(min(starts) * 1000), limit=1000)
    if len(trades) >= 1000:
        raise ValueError('Position history exceeds one verifiable page; reconcile ownership before trading')
    amounts = {}
    seen = set()
    for trade in trades:
        if trade.get('id') is None:
            raise ValueError('Exchange fill has no stable identity')
        if str(trade['id']) in seen:
            continue
        seen.add(str(trade['id']))
        link = links.get(str(trade.get('order')))
        if not link or link.get('config_id') != config_id:
            raise ValueError('Untracked or foreign fills share this position; MCP modification denied')
        side = str(link.get('side') or '').upper()
        if side not in {'LONG', 'SHORT'}:
            raise ValueError('Fill ownership direction is unresolved')
        amount = float(trade.get('amount') or 0)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError('Invalid exchange fill quantity')
        amounts[side] = amounts.get(side, 0) + (amount if link['role'] == 'entry' else -amount)
    actual = {str(row['side']).upper(): float(row['contracts']) for row in positions}
    if any(not math.isclose(amounts.get(side, 0), actual.get(side, 0), rel_tol=1e-6, abs_tol=1e-10) for side in set(amounts) | set(actual)):
        raise ValueError('Exchange position does not match solely owned fills; MCP modification denied')


def assert_entry_allowed(market_tool, symbol):
    config_id = str(getattr(market_tool, 'config_id', '') or '')
    if not config_id.startswith('mcp:'):
        return  # Internal agents deliberately retain their existing leverage policy.
    from .settings import get_runtime_profile, settings
    config = get_runtime_profile(config_id)
    if not config or not config.get('enabled') or not settings()['enabled']:
        raise ValueError('MCP trading profile is disabled or missing')
    if config['market_type'] == 'spot':
        return
    maximum = int(config['mcp_max_leverage'])
    if int(config.get('leverage', 1)) > maximum:
        raise ValueError('Configured leverage exceeds MCP maximum')
    exchange = market_tool.exchange
    try:
        canonical = perpetual_symbol(exchange, symbol)
        leverage = exchange.fetch_leverage(canonical)
        values = [leverage.get(key) for key in ('longLeverage', 'shortLeverage', 'leverage')]
        values = [float(value) for value in values if value is not None]
        # Also inspect open positions: OKX isolated positions may differ from the cross setting.
        positions = exchange.fetch_positions([canonical])
        for position in positions:
            if abs(float(position.get('contracts') or 0)):
                if position.get('leverage') is None:
                    raise ValueError('Existing position leverage is unavailable')
                values.append(float(position['leverage']))
        if not values or any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError('Exchange leverage is unavailable')
    except Exception as exc:
        raise ValueError('Cannot verify actual exchange leverage; MCP exposure increase denied') from exc
    if max(values) > maximum:
        raise ValueError(f'Actual exchange leverage {max(values):g}x exceeds MCP limit {maximum}x; close/cancel remains available')
    assert_position_owner(market_tool, symbol)


def preflight_tool(tool_name, args, config_id, symbol):
    if not str(config_id).startswith('mcp:'):
        return
    from .settings import get_runtime_profile
    profile = get_runtime_profile(config_id)
    if not profile:
        raise ValueError('Unknown MCP profile')
    if profile['market_type'] == 'spot':
        return
    risky = tool_name in {'open_position_real', 'update_entry_order_real'}
    if tool_name == 'execute_trade_actions':
        risky = any(action.get('action') in {'open', 'amend_entry'} for action in args.get('actions', []) if isinstance(action, dict))
    if risky:
        from backend.utils.market_data import MarketTool
        assert_entry_allowed(MarketTool(config_id=config_id), symbol)
    elif tool_name not in {'cancel_orders_real', 'cancel_orders_spot'}:
        if tool_name == 'execute_trade_actions' and all(action.get('action') == 'cancel' for action in args.get('actions', [])):
            return
        from backend.utils.market_data import MarketTool
        assert_position_owner(MarketTool(config_id=config_id), symbol)
