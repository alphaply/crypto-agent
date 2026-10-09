"""Scoped gateway to existing market and durable trading services."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from pydantic import BaseModel

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
        if requested in profile['symbols']:
            return requested
        if profile['market_type'] == 'swap':
            # A legacy BASE/QUOTE whitelist entry authorizes its linear
            # BASE/QUOTE:QUOTE contract, never a different settlement asset.
            for allowed in profile['symbols']:
                pair = allowed.split(':')[0]
                canonical = f'{pair}:{pair.split("/")[-1]}'
                if allowed in {pair, canonical} and requested in {pair, canonical}:
                    return canonical
        raise ValueError('Symbol is outside this MCP profile')
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


def list_symbols(profile, exchange, *, keyword='', quote='', limit=50, offset=0):
    """Page canonical instruments only after market and authorization filtering."""
    if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 200 or offset < 0:
        raise ValueError('limit must be 1–200 and offset must be nonnegative')
    if len(str(keyword)) > 100 or len(str(quote)) > 30:
        raise ValueError('Search keyword or quote currency is too long')
    markets = getattr(exchange, 'markets', None) or exchange.load_markets()
    records = {}
    needle = str(keyword or '').strip().upper()
    wanted_quote = str(quote or '').strip().upper()
    for market in markets.values():
        canonical = str(market.get('symbol') or '')
        if not canonical or market.get('active') is not True:
            continue
        if profile['market_type'] == 'spot':
            supported = market.get('spot') is True and not market.get('contract')
        else:
            supported = market.get('swap') is True and market.get('linear') is True
        if not supported:
            continue
        try:
            normalize_symbol(profile, canonical, exchange)
        except ValueError:
            continue
        base = str(market.get('base') or canonical.split('/')[0])
        currency = str(market.get('quote') or canonical.split('/')[-1].split(':')[0])
        if (needle and needle not in canonical.upper() and needle not in base.upper()) or (wanted_quote and wanted_quote != currency.upper()):
            continue
        records[canonical] = {'symbol': canonical, 'base': base, 'quote': currency,
                              'market_type': profile['market_type'], 'linear': market.get('linear') is True}
    ordered = sorted(records.values(), key=lambda item: item['symbol'])
    return {'profile_id': profile['profile_id'], 'exchange_profile_id': profile['exchange_profile_id'],
            'market_type': profile['market_type'], 'symbol_scope': profile.get('symbol_scope', 'selected'),
            'symbols': ordered[offset:offset + limit], 'total': len(ordered), 'limit': limit, 'offset': offset,
            'has_more': offset + limit < len(ordered)}


def query(caller, resource, profile_id=None, symbol=None, timeframe='1h', limit=120, keyword='', quote='', offset=0):
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
                if resource == 'symbols':
                    result = list_symbols(profile, exchange, keyword=keyword, quote=quote, limit=limit, offset=offset)
                elif resource == 'spot_inventory':
                    if profile['market_type'] != 'spot' or not symbol:
                        raise ValueError('Spot inventory requires a spot profile and explicit symbol')
                    from .spot_orders import inventory
                    result = inventory('mcp:' + profile_id, symbol)
                elif resource == 'balance':
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
    result = [{'name': tool.name, 'description': tool.description, 'inputSchema': tool.args_schema.model_json_schema()}
              for tool in get_trade_tools_for_mode(cfg['mode'])]
    if cfg['market_type'] == 'spot':
        from .spot_orders import tool_schemas
        result.extend(tool_schemas())
    return result


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result and not _same_value(result[key], value):
            raise ValueError(f'Conflicting duplicate JSON field: {key}')
        result[key] = value
    return result


def _same_value(left, right):
    return left == right and isinstance(left, bool) == isinstance(right, bool)


def _decode_json(value, field):
    for _ in range(3):
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value, object_pairs_hook=_json_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f'{field} must contain valid JSON') from exc
    if isinstance(value, str):
        raise ValueError(f'{field} has too many JSON string layers')
    return value


def unwrap_arguments(value, *, wrappers=('args', 'arguments'), depth=0):
    """Accept bounded, known client wrappers without silently choosing conflicts."""
    if depth > 4:
        raise ValueError('Too many nested argument wrappers')
    value = _decode_json(value, 'arguments')
    if not isinstance(value, dict):
        raise ValueError('arguments must be an object')
    result = {key: deepcopy(item) for key, item in value.items() if key not in wrappers}
    for wrapper in wrappers:
        if wrapper not in value:
            continue
        for key, item in unwrap_arguments(value[wrapper], wrappers=wrappers, depth=depth + 1).items():
            if key in result and not _same_value(result[key], item):
                raise ValueError(f'Conflicting argument field: {key}')
            result[key] = item
    return result


def normalize_trade_arguments(arguments, *, profile_id, symbol, tool_name, operation_id):
    result = unwrap_arguments(arguments)
    identities = {'profile_id': profile_id, 'config_id': 'mcp:' + profile_id,
                  'symbol': symbol, 'tool_name': tool_name, 'operation_id': operation_id}
    for key, expected in identities.items():
        if key not in result:
            continue
        actual = result.pop(key)
        matches = str(actual).strip().upper() == str(expected).strip().upper() if key == 'symbol' else actual == expected
        if not matches:
            raise ValueError(f'Conflicting gateway identity field: {key}')
    if 'cycle_id' in result:
        raise ValueError('cycle_id is controlled by the gateway')
    for key in ('orders', 'actions'):
        if key in result:
            result[key] = _decode_json(result[key], key)
            if not isinstance(result[key], list):
                raise ValueError(f'{key} must be an array')
            result[key] = [unwrap_arguments(item) for item in result[key]]
            if key == 'actions':
                for action in result[key]:
                    if 'order' in action:
                        action['order'] = unwrap_arguments(action['order'])
    return result


def _assert_no_ignored_fields(source, validated, path='arguments'):
    if isinstance(source, bool) and not isinstance(validated, bool):
        raise ValueError(f'{path} must not be a boolean')
    if isinstance(validated, BaseModel) and isinstance(source, dict):
        fields = type(validated).model_fields
        allowed = set(fields) | {field.alias for field in fields.values() if isinstance(field.alias, str)}
        unknown = set(source) - allowed
        if unknown:
            raise ValueError(f'Unsupported {path} fields: {", ".join(sorted(unknown))}. Consult get_trading_tools; no fields were ignored.')
        for name, field in fields.items():
            key = field.alias if field.alias in source else name
            if key in source:
                _assert_no_ignored_fields(source[key], getattr(validated, name), f'{path}.{key}')
    elif isinstance(source, list) and isinstance(validated, list):
        for index, (item, parsed) in enumerate(zip(source, validated)):
            _assert_no_ignored_fields(item, parsed, f'{path}[{index}]')


def validate_trade_arguments(tool_name, arguments):
    if tool_name in {'create_spot_exit', 'cancel_spot_exit'}:
        from .spot_orders import TOOL_MODELS
        model = TOOL_MODELS[tool_name]
    else:
        from backend.agent.tool_registry import _TOOL_BY_NAME
        model = _TOOL_BY_NAME[tool_name].args_schema
    parsed = model.model_validate(arguments)
    _assert_no_ignored_fields(arguments, parsed)
    return parsed.model_dump(exclude_none=True)


def trade_scope(tool_name, arguments):
    if tool_name in {'cancel_orders_spot', 'cancel_orders_real', 'cancel_spot_exit'}:
        return 'cancel'
    if tool_name == 'execute_trade_actions' and arguments.get('actions') and all(action.get('action') == 'cancel' for action in arguments['actions'] if isinstance(action, dict)):
        return 'cancel'
    return 'trade'


@serialized_spot_execution
def execute(caller, profile_id, symbol, tool_name, arguments, operation_id):
    if not isinstance(operation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
        raise ValueError('operation_id must contain 8–160 letters, numbers, dots, underscores, colons or hyphens')
    result = None
    reserved = False
    try:
        arguments = normalize_trade_arguments(arguments, profile_id=profile_id, symbol=symbol,
                                              tool_name=tool_name, operation_id=operation_id)
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
        validated = validate_trade_arguments(tool_name, arguments)
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
            if profile['market_type'] == 'spot' and tool_name not in {'create_spot_exit', 'cancel_spot_exit'}:
                from backend.utils.spot_execution import ensure_spot_budget_cycle
                cycle_id = stable_id
                ensure_spot_budget_cycle(cfg_id, get_runtime_profile(cfg_id), cycle_id, origin='mcp')
            # The gateway has already authenticated and validated this symbol.
            # Pass it explicitly to the shared multi-symbol spot dispatcher;
            # user-provided identity fields remain forbidden above.
            dispatch_args = {**validated, 'symbol': symbol} if profile['market_type'] == 'spot' else validated
            if tool_name in {'create_spot_exit', 'cancel_spot_exit'}:
                from .spot_orders import execute as execute_spot_exit
                raw = execute_spot_exit(tool_name, validated, cfg_id, symbol, stable_id)
            else:
                from backend.agent.tool_registry import run_trade_tool
                raw = run_trade_tool(tool_name, dispatch_args, cfg_id, symbol, operation_id=stable_id, cycle_id=cycle_id)
        from backend.utils.trade_operations import tool_result_status
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
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
