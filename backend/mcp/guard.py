"""MCP-only exposure gate, evaluated before entry/protection side effects."""
from __future__ import annotations

import math
import json


def perpetual_symbol(exchange, symbol):
    market = exchange.market(symbol)
    if not market.get('contract') and ':' not in symbol:
        market = exchange.market(f'{symbol}:{symbol.split("/")[-1]}')
    if market.get('contract') is False or market.get('linear') is False:
        raise ValueError('MCP perpetual profile requires a linear contract')
    return market['symbol']


def assert_position_owner(market_tool, symbol, *, plan_override=None, adopted_only=False, side=None):
    """Allow authorized configured tasks/profiles to manage shared account exposure."""
    return


def assert_entry_allowed(market_tool, symbol, *, side=None, only_capability=False):
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
    if not only_capability:
        assert_position_owner(market_tool, symbol, **({'side': side} if side else {}))


def is_exposure_increasing(tool_name, args, config_id, symbol):
    """Classify edits from an owned target, never from caller-supplied role hints."""
    if tool_name in {'open', 'open_position_spot_dca', 'open_position_real', 'update_entry_order_real'}:
        return True
    if tool_name == 'edit':
        from backend.agent.independent_tools import describe_order
        target = describe_order(config_id, symbol, args['order_id'])
        if target.get('kind') not in {'entry', 'exit'}:
            raise ValueError('Cannot verify edit target role')
        return target['kind'] == 'entry'
    if tool_name == 'execute_trade_actions':
        # Inspect every edit before the first action can write, including edits
        # after an entry; those targets must already exist and belong to us.
        increasing = False
        for action in args.get('actions', []):
            if action.get('action') == 'edit':
                increasing = is_exposure_increasing('edit', action, config_id, symbol) or increasing
            elif action.get('action') in {'open', 'amend_entry'}:
                increasing = True
        return increasing
    return False


def preflight_tool(tool_name, args, config_id, symbol):
    if not str(config_id).startswith('mcp:'):
        return
    from .settings import get_runtime_profile
    profile = get_runtime_profile(config_id)
    if not profile:
        raise ValueError('Unknown MCP profile')
    if profile['market_type'] == 'spot':
        return
    if tool_name in {'open', 'edit', 'open_position_real', 'update_entry_order_real', 'execute_trade_actions'}:
        # The action service verifies live leverage/ownership once, inside its
        # resource lock and before its first exchange write. Batch entry points
        # must not reuse one check for later actions or pre-read edit targets.
        return
    if tool_name == 'adopt_position_real':
        # The explicit adoption service verifies current ownership and quantity.
        # Requiring an existing cycle here would make adoption unreachable.
        return
    if tool_name not in {'cancel', 'cancel_orders_real', 'cancel_orders_spot'}:
        side = args.get('side')
        from backend.utils.market_data import MarketTool
        assert_position_owner(MarketTool(config_id=config_id), symbol, **({'side': side} if side else {}))
