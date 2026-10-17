"""Canonical MCP facade. Legacy registrations remain available for cached clients."""
from __future__ import annotations

import json
from functools import partial
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictFloat

from . import paper, paper_batch, paper_competition, paper_market, paper_orders, service, store
from .resilience import run
from .tools import OperationId, _register
from backend.utils.spot_config_guard import serialized_spot_execution

CORE_NAMES = ('get_trading_context', 'search_symbols', 'get_market', 'get_news',
              'get_account_state', 'get_trade_records', 'execute_trade_actions')
PAPER_NAMES = ('paper_bank', 'configure_paper_account', 'get_paper_leaderboard', 'paper_transfer')

# Private exchange/raw metadata and repeated account snapshots are opt-in;
# current order evidence, quantities, fees and management facts remain visible.
_RECORD_FIELDS = {
    'id', 'order_id', 'exchange_order_id', 'client_id', 'clientOrderId', 'parent_order_id',
    'execution_order_id', 'previous_order_id', 'operation_id', 'timestamp', 'datetime',
    'created_at', 'updated_at', 'verified_at', 'observed_at', 'timestamp_source', 'kind',
    'status', 'state', 'order_status', 'symbol', 'market_type', 'exchange', 'execution_mode',
    'action', 'side', 'position_side', 'pos_side', 'type', 'order_type', 'exit_type',
    'quantity_mode', 'quantity_unit', 'quantity_units', 'amount', 'filled', 'remaining',
    'amount_units', 'contract_size', 'base_amount', 'base_filled_amount', 'base_remaining_amount',
    'filled_amount', 'remaining_amount', 'resolved_amount', 'requested_amount', 'cost',
    'price', 'entry_price', 'trigger_price', 'triggerPrice', 'stopPrice', 'average', 'average_price',
    'fee', 'fees', 'realized_pnl', 'pnl', 'margin', 'leverage', 'margin_amount',
    'close_all', 'reduceOnly', 'hedged', 'oco_group', 'cycle_id', 'contract_version',
    'reserved_cash', 'reserved_spot', 'reservation_change', 'cash_after', 'debt_after',
    'source', 'sources', 'agent_name', 'config_id', 'trade_mode', 'event_type',
    'management', 'managed', 'modifiable', 'supported_actions',
    'exchange_channel', 'identity_status', 'pending', 'missing_sources', 'error',
    'ownership', 'last_known_order_status', 'snapshot_pending', 'quantity_conflict',
    'quantity_conflicts', 'uncovered', 'execution_guaranteed',
    'error_code', 'reason', 'reason_code', 'created', 'enabled', 'markets', 'name',
    'leaderboard_visible', 'notifications_enabled', 'stop_loss', 'take_profit',
    'delivery_key', 'interest', 'borrowed', 'repaid', 'debt', 'cash', 'reset_count', 'account_epoch', 'account_status', 'interest_paid', 'sender_user_id', 'recipient_user_id',
}


def _compact_record(value):
    if not isinstance(value, dict):
        return value
    projected = {key: item for key, item in value.items() if key in _RECORD_FIELDS and item is not None}
    for key in ('payload', 'order', 'fill'):
        if isinstance(value.get(key), dict):
            projected[key] = _compact_record(value[key])
    if isinstance(value.get('orders'), list):
        projected['orders'] = [_compact_record(row) for row in value['orders']]
    return projected


def _permitted_actions(capabilities, caller):
    scopes = caller.get('scopes') or []
    if caller.get('key_type') == 'paper':
        return capabilities['actions'] if 'trade' in scopes else []
    return [action for action in capabilities['actions']
            if ('cancel' if action == 'cancel' else 'trade') in scopes]


def child_alias(profile_id, principal, parent_id, index):
    return 'mcp-step:' + store.digest(profile_id + '\0' + principal + '\0' + parent_id + '\0' + str(index))


def recover_unified(caller, profile_id, operation_id):
    with store.connection(recovery=True) as conn:
        link = conn.execute('SELECT l.* FROM mcp_operation_links l JOIN mcp_operation_requests r '
                            'USING(profile_id,operation_id) WHERE l.profile_id=%s AND l.operation_id=%s '
                            'AND l.principal=%s AND r.contract_version=3', (profile_id, operation_id, caller['id'])).fetchone()
    if not link:
        return None
    from backend.agent.unified_trading import query_unified_operation
    payload = query_unified_operation('mcp:' + profile_id, link['symbol'], link['internal_operation_id'])
    if payload is None:
        return None
    result = {'operation_id': operation_id, 'status': payload.get('status', 'unknown'),
              'result': service._external_operation_receipt(payload, link['internal_operation_id'], operation_id, profile_id, principal=caller['id'])}
    store.complete_operation(profile_id, operation_id, result)
    return result


def query_child(caller, profile_id, operation_id):
    if not operation_id.startswith('mcp-step:'):
        return None
    with store.connection(recovery=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        if 'unified_trade_children' not in tables:
            return None
        rows = conn.execute('SELECT c.action_index,l.operation_id FROM unified_trade_children c '
                            'JOIN mcp_operation_links l ON l.internal_operation_id=c.parent_operation_id '
                            'WHERE c.config_id=%s AND l.profile_id=%s AND l.principal=%s',
                            ('mcp:' + profile_id, profile_id, caller['id'])).fetchall()
    found = next((row for row in rows if child_alias(profile_id, caller['id'], row['operation_id'], row['action_index']) == operation_id), None)
    if found is None:
        return None
    parent = service.operation(caller, profile_id, found['operation_id'])
    items = (parent.get('result') or {}).get('result', {}).get('results', [])
    child = next((item for item in items if item.get('index') == found['action_index']), None)
    return {'operation_id': operation_id, 'parent_operation_id': found['operation_id'],
            'status': (child or {}).get('status', 'pending'), 'state': (child or {}).get('status', 'pending'),
            'result': child, 'found': True}


def catalog_names(caller):
    service.require(caller)
    scopes = set(caller.get('scopes') or [])
    names = [name for name in CORE_NAMES if name != 'execute_trade_actions']
    if caller.get('key_type') == 'paper':
        paper.require(caller)
        names.append('get_paper_leaderboard')
        if 'trade' in scopes:
            names.extend(('execute_trade_actions', 'paper_bank', 'configure_paper_account', 'paper_transfer'))
    elif scopes & {'trade', 'cancel'} and service.list_profiles(caller):
        names.append('execute_trade_actions')
    return set(names)


def context(caller, user_id=None):
    from backend.agent.unified_trading import capabilities_for_config
    if caller.get('key_type') == 'paper':
        namespace = paper.require(caller)
        config = paper.settings()
        capabilities = capabilities_for_config({**config, 'mode': 'PAPER', 'market_type': 'paper'}, engine='paper')
        account_capabilities = None
        if user_id is not None:
            account = paper.get_account(caller, user_id)
            account_capabilities = {key: account[key] for key in ('user_id', 'enabled', 'markets', 'leverage', 'account_status', 'account_epoch', 'reset_count')}
            capabilities = capabilities_for_config({**config, **account, 'mode': 'PAPER', 'market_type': 'paper'}, engine='paper')
            if account['account_status'] == 'bankrupt':
                capabilities = {**capabilities, 'actions': []}
        return {'execution_mode': 'paper', 'namespace': namespace, 'settings': config,
                'account_rules': {'transfer_currency': 'USDT', 'bankrupt_can_receive': True,
                                  'bankrupt_can_send': False, 'interest_settlement': '00:00 Asia/Shanghai; simple on unpaid principal',
                                  'reset': 'Only a bankrupt human explicitly requesting 下海; never automatic or for the bot'},
                'bot_user_id': config['bot_user_id'], 'contract_version': 3,
                'capabilities': capabilities, 'allowed_actions': _permitted_actions(capabilities, caller),
                'account_capabilities': account_capabilities, 'scopes': caller['scopes'],
                'execution_updates': 'Original execution identities are verified automatically in the background.'}
    if user_id is not None:
        raise ValueError('Live context does not accept user_id')
    profiles = service.list_profiles(caller)
    from .settings import get_runtime_profile
    rows = []
    for row in profiles:
        capabilities = capabilities_for_config(get_runtime_profile('mcp:' + row['profile_id']))
        rows.append({**row, 'capabilities': capabilities, 'allowed_actions': _permitted_actions(capabilities, caller)})
    return {'execution_mode': 'live', 'contract_version': 3, 'scopes': caller['scopes'],
            'profiles': rows, 'execution_updates': 'Original execution identities are verified automatically in the background.'}


def _scope(caller, profile_id, user_id):
    is_paper = caller.get('key_type') == 'paper'
    if is_paper:
        paper.require(caller)
        if profile_id is not None or user_id is None:
            raise ValueError('Paper requires user_id and does not accept profile_id')
        paper.identity(user_id)
    else:
        if user_id is not None or profile_id is None:
            raise ValueError('Live requires profile_id and does not accept user_id')
        service.require(caller, 'read', profile_id)
    return is_paper


def account_state(caller, profile_id=None, user_id=None, symbol=None, include=None):
    is_paper = _scope(caller, profile_id, user_id)
    facets = include if include is not None else ['balances', 'positions']
    if not facets or set(facets) - {'balances', 'positions', 'inventory', 'orders'}:
        raise ValueError('Unsupported account facet')
    result = {'execution_mode': 'paper' if is_paper else 'live', 'profile_id': profile_id} if not is_paper else {'execution_mode': 'paper', 'user_id': user_id}
    if is_paper:
        view = paper.get_account(caller, user_id)
        result.update(account_status=view['account_status'], account_epoch=view['account_epoch'], reset_count=view['reset_count'],
                      filter=view['filter'], completeness=view['completeness'],
                      valuation_status=view['valuation_status'], oldest_mark_at=view['oldest_mark_at'])
        if 'balances' in facets:
            result['balances'] = {key: view[key] for key in ('cash', 'available_cash', 'reserved_cash', 'debt', 'equity', 'net_equity', 'used_margin')}
        if 'positions' in facets:
            result.update(positions=[row for row in view['positions'] if not symbol or row['symbol'] == symbol],
                          spot=[row for row in view['spot'] if not symbol or row['symbol'] == symbol], oldest_mark_at=view['oldest_mark_at'])
        if 'inventory' in facets:
            result['inventory'] = [row for row in view['spot'] if not symbol or row['symbol'] == symbol]
        if 'orders' in facets:
            result['orders'] = [_compact_record(row) for row in paper_orders.get_orders(caller, user_id, 'open', 100)['orders']
                                if not symbol or row['symbol'] == symbol]
    else:
        profile = service.require(caller, 'read', profile_id)
        if 'balances' in facets:
            result['balances'] = service.query(caller, 'balance', profile_id=profile_id)
        if 'positions' in facets:
            result['positions'] = service.query(caller, 'positions', profile_id=profile_id, symbol=symbol) if profile['market_type'] == 'swap' else []
        if 'inventory' in facets:
            result['inventory'] = service.query(caller, 'spot_inventory', profile_id=profile_id, symbol=symbol)
        if 'orders' in facets:
            result['orders'] = [_compact_record(row) for row in service.query(caller, 'orders', profile_id=profile_id, symbol=symbol)]
    return result


def records(caller, record_type, profile_id=None, user_id=None, symbol=None, order_id=None, state='open', limit=50, offset=0, detail=False):
    is_paper = _scope(caller, profile_id, user_id)
    if is_paper:
        if record_type in {'orders', 'order'}:
            if record_type == 'order' and not order_id:
                raise ValueError('order_id is required')
            page = paper_orders.records_page(caller, user_id, state=state, limit=limit,
                                              offset=offset, symbol=symbol,
                                              order_id=order_id if record_type == 'order' else None)
            if record_type == 'order':
                return {'record_type': record_type, 'order': page['order'] if detail else _compact_record(page['order'])}
        else:
            page = paper.history_page(caller, user_id, limit=limit, offset=offset,
                                      record_type=record_type, symbol=symbol)
        return {**page, 'record_type': record_type,
                'items': page['items'] if detail else [_compact_record(row) for row in page['items']]}
    resource = {'orders': 'orders', 'order': 'order', 'fills': 'trade_history', 'history': 'trade_history'}[record_type]
    if record_type == 'orders' and state != 'open':
        raise ValueError('Live order lists support state=open; query an exact order_id or actual fills for ended orders')
    # Read one extra item so a full page can distinguish a next page from the
    # final page. The legacy exchange read has a bounded 200-item window.
    read_limit = min(200, offset+limit+1)
    value = service.query(caller, resource, profile_id=profile_id, symbol=symbol, order_id=order_id, limit=read_limit)
    selected = value if record_type == 'order' else value[offset:offset+limit]
    return {'record_type': record_type, 'order': selected if detail else _compact_record(selected)} if record_type == 'order' else {
        'record_type': record_type, 'items': selected if detail else [_compact_record(row) for row in selected], 'has_more': len(value) > offset+limit,
        **({'window_limit': 200, 'window_exhausted': offset+limit >= 200} if len(value) == 200 else {})}


@serialized_spot_execution
def execute_live(caller, profile_id, symbol, request, operation_id):
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    from .settings import get_runtime_profile, runtime_symbols
    from backend.agent.unified_trading import validate_for_config
    from backend.agent.tool_registry import run_trade_tool
    profile = service.require(caller, 'read', profile_id)
    if query_child(caller, profile_id, operation_id) is not None:
        raise ValueError('operation_id already belongs to a child recovery identity')
    actions = request.get('actions') or []
    if any(item.get('action') == 'cancel' for item in actions):
        service.require(caller, 'cancel', profile_id)
    if any(item.get('action') != 'cancel' for item in actions):
        service.require(caller, 'trade', profile_id)
    cfg_id = 'mcp:' + profile_id
    config = get_runtime_profile(cfg_id)
    # The tool owns its client's teardown, so it must outlive every symbol
    # validation below instead of being released as a temporary.
    mt = service.market_tool(profile_id) if profile.get('symbol_scope') == 'all' else None
    exchange = mt.exchange if mt else None
    allowed = [service.normalize_symbol(profile, value, exchange, allow_inactive=True) for value in profile['symbols']]
    normalized_request = json.loads(json.dumps(request))
    for action in normalized_request.get('actions', []):
        if action.get('symbol'):
            action['symbol'] = service.normalize_symbol(profile, action['symbol'], exchange, allow_inactive=True)
    default = service.normalize_symbol(profile, symbol, exchange, allow_inactive=True) if symbol else None
    parsed = validate_for_config(normalized_request, {**config, 'symbols': allowed, 'symbol_scope': profile.get('symbol_scope', 'selected')}, default_symbol=default,
                                allowed_symbols=allowed if profile.get('symbol_scope', 'selected') == 'selected' else None)
    requested = [action.symbol for action in parsed.actions if getattr(action, 'symbol', None)]
    symbols = [service.normalize_symbol(profile, value, exchange, allow_inactive=True) for value in requested]
    primary = symbols[0] if symbols else symbol
    normalized = parsed.model_dump(exclude_none=True)
    intent = {'symbol': primary, 'tool': 'execute_trade_actions', 'arguments': normalized, 'contract_version': 3}
    previous = store.reserve_operation(profile_id, operation_id, caller['id'], intent)
    if previous is not None:
        return previous
    stable_id = 'mcp:' + store.digest(caller['id'] + ':' + operation_id)
    try:
        with runtime_symbols(cfg_id, symbols):
            # v3 registers its parent allowance lazily at the first spot open.
            # Passing an unregistered cycle would mark it as already allocated.
            raw = run_trade_tool('execute_trade_actions', normalized, cfg_id, primary, operation_id=stable_id, project_result=False)
        payload = json.loads(raw) if isinstance(raw, str) else raw
        result = {'operation_id': operation_id, 'status': payload.get('status', 'unknown'),
                  'result': service._external_operation_receipt(payload, stable_id, operation_id, profile_id, principal=caller['id'])}
    except Exception as exc:
        result = {'operation_id': operation_id, 'status': 'unknown', 'error': service.safe_error(exc, profile_id)}
    result = service.sanitize(result, profile_id)
    try:
        store.complete_operation(profile_id, operation_id, result)
    except Exception:
        result['persistence_pending'] = True
    return result


def configure(caller, user_id, operation_id, action, name=None, leverage=None, leaderboard_visible=None, notifications_enabled=None):
    paper.require(caller, 'read')
    paper.require(caller, 'trade')
    if action == 'reset':
        if any(v is not None for v in (name, leverage, leaderboard_visible, notifications_enabled)):
            raise ValueError('Reset accepts no preference overrides')
        from .paper_lifecycle import reset
        return reset(caller, user_id, operation_id)
    if action not in {'create', 'update'}:
        raise ValueError('Unsupported account action')
    if action == 'create':
        if leverage is not None or leaderboard_visible is not None or notifications_enabled is not None:
            raise ValueError('Create accepts name only; update existing settings separately')
        return paper.create_account(caller, user_id, operation_id, name or '')
    updates = {key: value for key, value in {'name': name, 'leverage': leverage,
                'leaderboard_visible': leaderboard_visible, 'notifications_enabled': notifications_enabled}.items() if value is not None}
    if not updates:
        raise ValueError('Provide at least one setting')
    if leverage is not None and not 1 <= leverage <= paper.settings()['max_leverage']:
        raise ValueError('Leverage exceeds configured maximum')
    def change(account, conn):
        account.update(updates)
        return {'preferences': updates}
    return paper._write(caller, user_id, operation_id, {'action': 'configure', **updates}, change)


def register(server):
    async def invoke(fn, *args, write=False, **kwargs):
        return await run('execute' if write else 'read', partial(fn, service.principal(), *args, **kwargs), label=fn.__name__)

    async def get_trading_context(user_id: str | None = None) -> dict:
        """Read authorized profiles/capabilities or paper rules and your bot identity. Does not claim or create accounts."""
        return await invoke(context, user_id)

    async def search_symbols(profile_id: str | None = None, keyword: str = '', quote: str = '',
                             market_type: Literal['spot', 'swap'] | None = None,
                             exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None,
                             limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50,
                             offset: Annotated[StrictInt, Field(ge=0)] = 0) -> dict:
        """Search authorized live markets or real public paper venues. Preserve returned canonical symbol and exchange."""
        def search(caller):
            if caller.get('key_type') == 'paper':
                paper.require(caller)
                if profile_id is not None:
                    raise ValueError('Paper does not accept profile_id')
                return paper_market.search(keyword, market_type, exchange, limit, offset)
            if market_type is not None or exchange is not None:
                raise ValueError('Live venue and market_type come from profile')
            return service.query(caller, 'symbols', profile_id=profile_id, keyword=keyword, quote=quote, limit=limit, offset=offset)
        return await invoke(search)

    async def get_market(symbol: str, profile_id: str | None = None, timeframe: str = '1h', limit: int = 120,
                         market_type: Literal['spot', 'swap'] | None = None,
                         exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None,
                         include: list[Literal['ticker', 'candles', 'analysis', 'sentiment']] | None = None) -> dict:
        """Read real ticker/candles, multi-timeframe technical indicators, and derivatives sentiment. Paper only simulates account execution."""
        def query(caller):
            if caller.get('key_type') == 'paper':
                paper.require(caller)
                if profile_id is not None:
                    raise ValueError('Paper does not accept profile_id')
                return paper_market.resolve_quote(symbol, market_type or 'swap', timeframe, limit, exchange, include=include)
            if market_type is not None or exchange is not None:
                raise ValueError('Live market source comes from profile')
            return service.query(caller, 'market', profile_id=profile_id, symbol=symbol, timeframe=timeframe, limit=limit, include=include)
        return await invoke(query)

    async def get_account_state(profile_id: str | None = None, user_id: str | None = None, symbol: str | None = None,
                                include: list[Literal['balances', 'positions', 'inventory', 'orders']] | None = None) -> dict:
        """Read only requested account facets. Positions alone do not append all balances, orders or history."""
        return await invoke(account_state, profile_id, user_id, symbol, include)

    async def get_trade_records(record_type: Literal['orders', 'order', 'fills', 'history'], profile_id: str | None = None,
                                user_id: str | None = None, symbol: str | None = None, order_id: str | None = None,
                                state: Literal['open', 'filled', 'cancelled', 'rejected', 'all'] = 'open',
                                limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50,
                                offset: Annotated[StrictInt, Field(ge=0, le=10000)] = 0, detail: StrictBool = False) -> dict:
        """Inspect compact orders, one exact order, fills or history; detail=true retains full records. order_id differs from operation_id."""
        return await invoke(records, record_type, profile_id, user_id, symbol, order_id, state, limit, offset, detail)

    async def execute_trade_actions(operation_id: OperationId, contract_version: Literal[2, 3], actions: list[dict],
                                    profile_id: str | None = None, user_id: str | None = None, symbol: str | None = None,
                                    execution_policy: Literal['stop_on_error', 'independent'] | None = None,
                                    include: list[Literal['account']] | None = None) -> dict:
        """Execute 1–20 typed v3 actions in order. Return concise per-action success, failure reason or not-executed text, with structured evidence retained. A successful submission is not a fill. Necessary preflight checks remain; no post-submission polling or verification reads inside this call. Unknown submissions stop all remaining actions, including independent batches. The background verifies original identities without replaying writes or tails. v2 remains a compatibility input."""
        caller = service.principal()
        is_paper = _scope(caller, profile_id, user_id)
        request = {'contract_version': contract_version, 'actions': actions}
        if execution_policy is not None:
            request['execution_policy'] = execution_policy
        if include is not None:
            request['include'] = include
        if symbol is not None and is_paper:
            raise ValueError('Paper symbols belong to actions')
        if contract_version == 2:
            if is_paper or include is not None:
                raise ValueError('Version 2 is an independent live compatibility contract')
            from pydantic import TypeAdapter
            from backend.agent.trading_contracts import TradeAction
            # The old SDK validated each item before dispatch; envelope errors
            # (policy/dependency combinations) were structured gateway failures.
            parsed_actions = TypeAdapter(list[TradeAction]).validate_python(actions)
            request['actions'] = [item.model_dump(exclude_none=True) for item in parsed_actions]
            return await invoke(service.execute, profile_id, symbol, 'execute_trade_actions', request, operation_id, write=True)
        raw = await invoke(paper_batch.execute, user_id, request, operation_id, write=True) if is_paper else await invoke(execute_live, profile_id, symbol, request, operation_id, write=True)
        from backend.agent.unified_trading import compact_trade_receipt
        return {**compact_trade_receipt(raw), **({'persistence_pending': True} if raw.get('persistence_pending') else {})}

    async def configure_paper_account(user_id: str, operation_id: OperationId, action: Literal['create', 'update', 'reset'],
                                      name: Annotated[str, Field(min_length=1, max_length=100)] | None = None,
                                      leverage: Annotated[StrictInt, Field(ge=1, le=125)] | None = None,
                                      leaderboard_visible: StrictBool | None = None,
                                      notifications_enabled: StrictBool | None = None) -> dict:
        """Create without resetting, update preferences, or reset a bankrupt user ONLY after their own explicit 下海 request. Bot accounts cannot reset."""
        return await invoke(configure, user_id, operation_id, action, name, leverage, leaderboard_visible, notifications_enabled, write=True)

    async def paper_transfer(user_id: str, recipient_user_id: str, amount: Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)], operation_id: OperationId) -> dict:
        """Transfer positive USDT from the authorized sender to a stable recipient ID in this namespace. Borrowed free funds may be sent. Bankrupt accounts may receive but cannot send. Bot aid uses only bot_user_id funds."""
        from .paper_lifecycle import transfer
        return await invoke(transfer, user_id, recipient_user_id, amount, operation_id, write=True)

    for function in (get_trading_context, search_symbols, get_market, get_account_state, get_trade_records,
                     execute_trade_actions, configure_paper_account, paper_transfer):
        if server._tool_manager.get_tool(function.__name__):
            server._tool_manager.remove_tool(function.__name__)
        _register(server, function, scope='trade / cancel' if function.__name__ == 'execute_trade_actions' else 'trade' if function.__name__ in {'configure_paper_account', 'paper_transfer'} else 'read')
    original_list = server.list_tools
    async def filtered_tools():
        caller = service.principal()
        names = catalog_names(caller)
        listed = [tool for tool in await original_list() if tool.name in names]
        for tool in listed:
            schema = json.loads(json.dumps(tool.inputSchema))
            is_paper = caller.get('key_type') == 'paper'
            properties = schema.get('properties', {})
            if is_paper:
                properties.pop('profile_id', None)
                if 'exchange' in properties:
                    properties['exchange'] = {'anyOf': [{'type': 'string', 'const': 'binance'}, {'type': 'null'}], 'default': None}
            else:
                for key in ('user_id', 'exchange', 'market_type'):
                    properties.pop(key, None)
            if tool.name == 'execute_trade_actions':
                from backend.agent.unified_trading import unified_schema_for_config, capabilities_for_config
                from .settings import get_runtime_profile
                profiles = [] if is_paper else service.list_profiles(caller)
                configs = [get_runtime_profile('mcp:' + row['profile_id']) for row in profiles]
                if is_paper:
                    envelope = unified_schema_for_config({'mode': 'PAPER', 'markets': ['spot', 'swap']}, engine='paper')
                    for definition in envelope.get('$defs', {}).values():
                        if 'exchange' in definition.get('properties', {}):
                            definition['properties']['exchange'] = {'anyOf': [{'type': 'string', 'const': 'binance'}, {'type': 'null'}], 'default': None}
                        action = definition.get('properties', {}).get('action', {}).get('const')
                        if action in {'open', 'close', 'protect'}:
                            definition.setdefault('required', []).extend(['market_type', 'symbol'])
                        if action == 'open':
                            definition['properties']['leverage'] = {'anyOf': [{'type': 'integer', 'minimum': 1, 'maximum': paper.settings()['max_leverage']}, {'type': 'null'}]}
                        if action == 'close':
                            definition['properties']['order_type'] = {'type': 'string', 'enum': capabilities_for_config({'mode': 'PAPER'}, engine='paper')['close_order_types']}
                elif len(configs) == 1:
                    envelope = unified_schema_for_config(configs[0])
                else:
                    from backend.agent.trading_contracts import UnifiedTradeActionsSchema
                    envelope = UnifiedTradeActionsSchema.model_json_schema()
                    for definition in envelope.get('$defs', {}).values():
                        for key in ('market_type', 'exchange', 'margin_amount', 'quote_amount', 'leverage', 'valid_duration_hours'):
                            definition.get('properties', {}).pop(key, None)
                allowed = set().union(*[set(_permitted_actions(capabilities_for_config(cfg), caller)) for cfg in configs]) if not is_paper else set(_permitted_actions(capabilities_for_config({'mode': 'PAPER'}, engine='paper'), caller))
                items = envelope['properties']['actions']['items']
                mapping = items['discriminator']['mapping']
                for action in list(mapping):
                    if action not in allowed:
                        mapping.pop(action)
                items['oneOf'] = [{'$ref': ref} for ref in mapping.values()]
                used = {ref.rsplit('/', 1)[-1] for ref in mapping.values()}
                envelope['$defs'] = {name: value for name, value in envelope.get('$defs', {}).items() if name in used}
                schema.setdefault('$defs', {}).update(envelope.get('$defs', {}))
                for key in ('actions', 'contract_version', 'execution_policy', 'include'):
                    if key in envelope['properties']:
                        properties[key] = envelope['properties'][key]
            if tool.name == 'get_trade_records' and not is_paper:
                properties['state'] = {'type': 'string', 'const': 'open', 'default': 'open'}
            if properties.get('profile_id') is not None:
                schema.setdefault('required', []).append('profile_id')
            if is_paper and properties.get('user_id') is not None and tool.name in {'execute_trade_actions', 'get_account_state', 'get_trade_records'}:
                schema.setdefault('required', []).append('user_id')
            schema['required'] = list(dict.fromkeys(schema.get('required', [])))
            tool.inputSchema = schema
            if tool.name == 'execute_trade_actions':
                scopes = ['read'] + ([scope for scope in ('trade', 'cancel') if scope in caller.get('scopes', [])]
                                      if not is_paper else ['trade'])
                tool.meta = {**(tool.meta or {}), 'securitySchemes': [{'type': 'oauth2', 'scopes': scopes}]}
        return listed
    server._mcp_server.list_tools()(filtered_tools)
    server.list_tools = filtered_tools
