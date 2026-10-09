"""Scoped gateway to existing market and durable trading services."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from mcp.server.auth.middleware.auth_context import get_access_token

from . import store
from .settings import profiles, get_runtime_profile, runtime_symbols, settings
from .redaction import sanitize, safe_error
from backend.utils.spot_config_guard import serialized_spot_execution


def principal():
    token = get_access_token()
    if token is None:
        raise PermissionError('MCP authentication required')
    return {'id': (token.claims or {}).get('principal') or token.client_id,
            'scopes': token.scopes, 'profile_ids': (token.claims or {}).get('profile_ids', [])}


def require(caller, scope='read', profile_id=None):
    if not settings()['enabled']:
        raise PermissionError('MCP service is disabled')
    if scope not in caller.get('scopes', []):
        raise PermissionError(f'Missing MCP scope: {scope}')
    if profile_id is None:
        return None
    if profile_id not in caller.get('profile_ids', []):
        raise PermissionError('This token is not authorized for the requested MCP profile')
    row = store.get('profile', profile_id)
    if not row or not row.get('enabled'):
        raise PermissionError('MCP profile is disabled or missing')
    return row


def list_profiles(caller):
    require(caller)
    return [row for row in profiles() if row['profile_id'] in caller['profile_ids'] and row.get('enabled')]


def normalize_symbol(profile, symbol, exchange=None, *, allow_inactive=False):
    requested = str(symbol or '').strip().upper()
    if profile.get('symbol_scope', 'selected') != 'all':
        if requested not in profile['symbols']:
            raise ValueError('Symbol is outside this MCP profile')
        return requested
    if not requested or '/' not in requested or len(requested) > 50:
        raise ValueError('Provide a valid exchange symbol')
    if exchange is None:
        raise ValueError('Exchange market validation is required for unrestricted symbols')
    try:
        if profile['market_type'] == 'swap':
            from .guard import perpetual_symbol
            requested = perpetual_symbol(exchange, requested)
        market = exchange.market(requested)
    except Exception as exc:
        raise ValueError('Symbol is not a valid market on this exchange') from exc
    if profile['market_type'] == 'spot':
        valid = market.get('spot') is True and not market.get('contract')
    else:
        valid = market.get('swap') is True and market.get('linear') is True
    if not valid:
        raise ValueError('Symbol market type does not match this MCP profile')
    if market.get('active') is False and not allow_inactive:
        raise ValueError('Symbol is not active on this exchange')
    if not market.get('symbol'):
        raise ValueError('Exchange did not identify a canonical market symbol')
    return str(market['symbol'])


def market_tool(profile_id):
    from backend.utils.market_data import MarketTool
    return MarketTool(config_id='mcp:' + profile_id)


def query(caller, resource, profile_id=None, symbol=None, timeframe='1h', limit=120):
    try:
        profile = require(caller, 'read', profile_id)
        if resource == 'news':
            from backend.app.services.news_service import get_latest_news
            result = get_latest_news()
        elif resource == 'profiles':
            result = list_profiles(caller)
        else:
            if profile is None:
                raise ValueError('profile_id is required')
            if resource == 'tools':
                result = trading_tools(profile_id)
            else:
                exchange = market_tool(profile_id).exchange
                symbol = normalize_symbol(profile, symbol, exchange, allow_inactive=True) if symbol else None
                if profile['market_type'] == 'swap':
                    from .guard import perpetual_symbol
                    symbol = perpetual_symbol(exchange, symbol) if symbol else None
                if resource == 'balance':
                    balance = exchange.fetch_balance()
                    result = {key: balance.get(key) for key in ('total', 'free', 'used', 'timestamp', 'datetime')}
                elif resource == 'positions':
                    if profile['market_type'] == 'spot':
                        raise ValueError('Use balance for spot holdings')
                    unrestricted = profile.get('symbol_scope', 'selected') == 'all'
                    selected = [symbol] if symbol else (None if unrestricted else [perpetual_symbol(exchange, value) for value in profile['symbols']])
                    rows = exchange.fetch_positions() if selected is None else exchange.fetch_positions(selected)
                    result = []
                    for row in rows:
                        if unrestricted:
                            try:
                                normalize_symbol(profile, row.get('symbol'), exchange, allow_inactive=True)
                            except ValueError:
                                continue  # Account-wide responses may also contain inverse futures/options.
                        result.append({k: v for k, v in row.items() if k != 'info'})
                elif resource == 'orders':
                    if not symbol:
                        raise ValueError('symbol is required for orders')
                    result = [{k: v for k, v in row.items() if k != 'info'} for row in exchange.fetch_open_orders(symbol, limit=max(1, min(int(limit), 200)))]
                elif resource == 'market':
                    if not symbol or timeframe not in {'15m', '1h', '4h', '1d', '1w'}:
                        raise ValueError('Provide a configured symbol and supported timeframe')
                    result = {'symbol': symbol, 'timeframe': timeframe,
                              'ticker': {k: v for k, v in exchange.fetch_ticker(symbol).items() if k != 'info'},
                              'candles': exchange.fetch_ohlcv(symbol, timeframe, limit=max(1, min(int(limit), 500)))}
                else:
                    raise ValueError('Unknown query resource')
        store.audit(caller['id'], 'query_' + resource, 'completed', profile_id=profile_id)
        return sanitize(result, profile_id)
    except Exception as exc:
        error = safe_error(exc, profile_id)
        store.audit(caller['id'], 'query_' + resource, 'failed', profile_id=profile_id, result={'error': error})
        raise ValueError(error) from None


def trading_tools(profile_id):
    from backend.agent.tool_registry import get_trade_tools_for_mode
    cfg = get_runtime_profile('mcp:' + profile_id)
    if not cfg:
        raise ValueError('Unknown MCP profile')
    return [{'name': tool.name, 'description': tool.description, 'inputSchema': tool.args_schema.model_json_schema()}
            for tool in get_trade_tools_for_mode(cfg['mode'])]


def trade_scope(tool_name, arguments):
    if tool_name in {'cancel_orders_spot', 'cancel_orders_real'}:
        return 'cancel'
    if tool_name == 'execute_trade_actions' and arguments.get('actions') and all(action.get('action') == 'cancel' for action in arguments['actions'] if isinstance(action, dict)):
        return 'cancel'
    return 'trade'


@serialized_spot_execution
def execute(caller, profile_id, symbol, tool_name, arguments, operation_id):
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object')
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
        raise ValueError('operation_id must contain 8–160 letters, numbers, dots, underscores, colons or hyphens')
    result = None
    reserved = False
    try:
        profile = require(caller, trade_scope(tool_name, arguments), profile_id)
        if tool_name == 'execute_trade_actions' and any(
            isinstance(action, dict) and action.get('action') == 'cancel'
            for action in arguments.get('actions', [])
        ):
            require(caller, 'cancel', profile_id)
        unrestricted = profile.get('symbol_scope', 'selected') == 'all'
        exchange = market_tool(profile_id).exchange if unrestricted else None
        increasing = tool_name in {'open_position_spot_dca', 'open_position_real', 'update_entry_order_real'} or (
            tool_name == 'execute_trade_actions' and any(
                isinstance(action, dict) and action.get('action') in {'open', 'amend_entry'}
                for action in arguments.get('actions', [])
            )
        )
        symbol = normalize_symbol(profile, symbol, exchange, allow_inactive=not increasing)
        allowed = {tool['name']: tool for tool in trading_tools(profile_id)}
        if tool_name not in allowed:
            raise PermissionError('Tool is not available for this MCP trading profile')
        if any(key in arguments for key in ('config_id', 'symbol', 'operation_id', 'cycle_id')):
            raise ValueError('Trading identity and symbol must be supplied through gateway fields')
        arguments = deepcopy(arguments)
        call_symbols = [symbol]
        # Per-order spot symbols may vary, but remain within this profile and
        # share one quote-denominated allowance for the entire operation.
        for order in arguments.get('orders', []) if isinstance(arguments.get('orders', []), list) else []:
            if isinstance(order, dict) and order.get('symbol'):
                if profile['market_type'] != 'spot':
                    raise ValueError('Perpetual orders use the gateway symbol; per-order symbols are unsupported')
                order['symbol'] = normalize_symbol(profile, order['symbol'], exchange, allow_inactive=not increasing)
                call_symbols.append(order['symbol'])
        if profile['market_type'] == 'spot':
            from backend.utils.spot_portfolio import normalize_spot_symbols
            call_symbols = normalize_spot_symbols(call_symbols)
        from backend.agent.tool_registry import _TOOL_BY_NAME, run_trade_tool
        validated = _TOOL_BY_NAME[tool_name].args_schema.model_validate(arguments).model_dump(exclude_none=True)
        request = {'symbol': symbol, 'tool': tool_name, 'arguments': validated}
        previous = store.reserve_operation(profile_id, operation_id, caller['id'], request)
        if previous is not None:
            store.audit(caller['id'], tool_name, 'replayed', profile_id=profile_id, operation_id=operation_id)
            return sanitize(previous, profile_id)
        reserved = True
        cfg_id = 'mcp:' + profile_id
        stable_id = 'mcp:' + store.digest(caller['id'] + ':' + operation_id)
        cycle_id = None
        with runtime_symbols(cfg_id, call_symbols):
            if profile['market_type'] == 'spot':
                from backend.utils.spot_execution import ensure_spot_budget_cycle
                cycle_id = stable_id
                ensure_spot_budget_cycle(cfg_id, get_runtime_profile(cfg_id), cycle_id, origin='mcp')
            # The gateway has already authenticated and validated this symbol.
            # Pass it explicitly to the shared multi-symbol spot dispatcher;
            # user-provided identity fields remain forbidden above.
            dispatch_args = {**validated, 'symbol': symbol} if profile['market_type'] == 'spot' else validated
            raw = run_trade_tool(tool_name, dispatch_args, cfg_id, symbol, operation_id=stable_id, cycle_id=cycle_id)
        from backend.utils.trade_operations import tool_result_status
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = raw
        result = {'operation_id': operation_id, 'status': tool_result_status(raw), 'result': payload}
    except (ValueError, PermissionError) as exc:
        result = {'operation_id': operation_id, 'status': 'failed', 'error': safe_error(exc, profile_id)}
    except Exception as exc:
        # A timeout is never an invitation to repeat a financial write.
        result = {'operation_id': operation_id, 'status': 'unknown', 'error': safe_error(exc, profile_id)}
    finally:
        if result is not None:
            result = sanitize(result, profile_id)
            if reserved:
                store.complete_operation(profile_id, operation_id, result)
            store.audit(caller['id'], tool_name, result['status'], profile_id=profile_id,
                        operation_id=operation_id, request=sanitize({'symbol': symbol, 'arguments': arguments}, profile_id), result=result)
    return result


def operation(caller, profile_id, operation_id):
    require(caller, 'read', profile_id)
    with store.connection() as conn:
        row = conn.execute('SELECT * FROM mcp_operations WHERE profile_id=? AND operation_id=? AND principal=?',
                           (profile_id, operation_id, caller['id'])).fetchone()
    if not row:
        raise ValueError('Operation not found for this caller')
    return sanitize({'operation_id': operation_id, 'state': row['state'],
            'result': json.loads(row['result']) if row['result'] else None,
            'created_at': row['created_at'], 'updated_at': row['updated_at']}, profile_id)
