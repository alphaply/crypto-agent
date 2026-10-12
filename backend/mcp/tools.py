"""MCP tool contracts with flat trade inputs and bounded client compatibility."""
from __future__ import annotations

from functools import partial
from typing import Annotated, Literal

import anyio
from mcp.types import ToolAnnotations
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import ConfigDict, Field, create_model, model_validator

from . import service
from backend.agent.trading_contracts import OrderType, PositiveNumber, TradeAction

OperationId = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_.:-]{8,160}$', description='Caller-supplied stable ID. Reuse only for the identical request; never generate a new ID to retry an unknown result.')]
Amount = Annotated[float, Field(gt=0, allow_inf_nan=False, description='Exact base-asset quantity, not quote-currency cost or contract count.')]
Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Reason = Annotated[str, Field(min_length=1, description='Why this specific order or change is requested.')]
ProfileId = Annotated[str, Field(min_length=1, description='An authorized profile_id returned by list_profiles.')]
Symbol = Annotated[str, Field(min_length=1, description='Exact canonical symbol returned by list_symbols, for example BTC/USDT or BTC/USDT:USDT.')]

TOOL_CATALOG = [
    {'name': name, 'scope': scope, 'description': description}
    for name, scope, description in [
        ('list_profiles', 'read', 'List authorized connection profiles'),
        ('list_symbols', 'read', 'Search and page canonical markets allowed by a profile'),
        ('get_news', 'read', 'Read the shared scored news summary'),
        ('get_market', 'read', 'Read current ticker and candles'),
        ('get_balance', 'read', 'Read exchange balances'),
        ('get_positions', 'read', 'Read perpetual positions'),
        ('get_orders', 'read', 'Read open exchange orders'),
        ('get_spot_inventory', 'read', 'Read MCP-owned spot quantity and pending exits'),
        ('get_trading_tools', 'read', 'Read legacy dispatch schemas'),
        ('buy_spot', 'trade', 'Place one spot limit buy with flat arguments'),
        ('create_spot_exit', 'trade', 'Sell owned spot quantity or place native TP/SL/OCO'),
        ('cancel_spot_exit', 'cancel', 'Cancel an MCP-owned spot exit'),
        ('open', 'trade', 'Independent mode: place a limit entry with canonical version 2 fields'),
        ('close', 'trade', 'Independent mode: fixed staged exits or supported dynamic close-all'),
        ('edit', 'trade', 'Independent mode: edit an owned entry/exit with remaining quantity'),
        ('cancel', 'cancel', 'Independent mode: cancel one owned regular or conditional order'),
        ('execute_trade_actions', 'trade / cancel', 'Version 2 ordered actions; optional independent policy and explicit dependencies'),
        ('open_perpetual', 'trade', 'Place one linear perpetual limit entry'),
        ('adopt_perpetual_position', 'trade', 'Explicitly adopt a user-owned manual perpetual position for management'),
        ('close_perpetual', 'trade', 'Reduce or close an owned perpetual position'),
        ('amend_perpetual_entry', 'trade', 'Change an owned entry price or total quantity'),
        ('update_perpetual_protection', 'trade', 'Change owned perpetual TP/SL protection'),
        ('cancel_order', 'cancel', 'Cancel one order owned by this profile'),
        ('execute_trade', 'trade / cancel', 'Legacy tool dispatcher with persistent operation IDs'),
        ('get_operation', 'read', 'Read a persisted operation receipt without replaying it'),
    ]
]


class GatewayFuncMetadata(FuncMetadata):
    def pre_parse_json(self, data):
        # The SDK's permissive json.loads would erase conflicting duplicate keys
        # before our gateway can reject them. Our bounded parser owns wrappers
        # and business JSON; pydantic still validates every flat field normally.
        return data


def normalize_tool_input(tool_name, value):
    # Only the conventional args envelope is flattened at the MCP boundary.
    # execute_trade.arguments remains the explicit business-argument container.
    result = service.unwrap_arguments(value, wrappers=('args',))
    numeric = {'amount', 'remaining_amount', 'expected_amount', 'entry_price', 'price', 'trigger_price', 'stop_loss', 'take_profit',
               'stop_limit_price', 'take_profit_limit_price', 'limit', 'offset'}
    for key in numeric & result.keys():
        if isinstance(result[key], bool):
            raise ValueError(f'{key} must be a number, not a boolean')
    if 'close_all' in result and result['close_all'] is not None and type(result['close_all']) is not bool:
        raise ValueError('close_all must be a boolean')
    if 'contract_version' in result and type(result['contract_version']) is not int:
        raise ValueError('contract_version must be an integer')
    return result


def _register(server, function, *, scope='read'):
    read = scope == 'read'
    annotations = ToolAnnotations(readOnlyHint=read, destructiveHint=not read,
                                  idempotentHint=not read, openWorldHint=True)
    scopes = ['read'] if read else ['read', scope] if scope != 'trade / cancel' else ['read', 'trade', 'cancel']
    server.add_tool(function, annotations=annotations, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': scopes}]})
    # FastMCP's generated argument model defaults to ignoring unknown fields.
    # Constrain that one SDK boundary so misplaced amounts/TP/SL cannot vanish
    # before the gateway sees them. Keep the published schema flat and exact.
    registered = server._tool_manager.get_tool(function.__name__)

    def normalize(cls, value):
        return normalize_tool_input(function.__name__, value)

    model = create_model(
        f'{function.__name__}Arguments',
        __base__=registered.fn_metadata.arg_model,
        __config__=ConfigDict(extra='forbid'),
        __validators__={'normalize_client_arguments': model_validator(mode='before')(normalize)},
    )
    registered.fn_metadata.arg_model = model
    registered.fn_metadata = GatewayFuncMetadata(**{
        name: getattr(registered.fn_metadata, name) for name in FuncMetadata.model_fields
    })
    registered.parameters = model.model_json_schema(by_alias=True)


def register_tools(server):
    async def query(resource, **kwargs):
        return await anyio.to_thread.run_sync(partial(service.query, service.principal(), resource, **kwargs))

    async def execute(profile_id, symbol, tool_name, arguments, operation_id):
        return await anyio.to_thread.run_sync(partial(service.execute, service.principal(), profile_id,
                                                     symbol, tool_name, arguments, operation_id))

    async def list_profiles() -> list[dict]:
        """List authorized connection profiles and selected/all symbol scope. Never exposes exchange secrets."""
        return await query('profiles')

    async def list_symbols(profile_id: ProfileId, keyword: str = '', quote: str = '',
                           limit: Annotated[int, Field(ge=1, le=200)] = 50,
                           offset: Annotated[int, Field(ge=0)] = 0) -> dict:
        """Search actual active markets within the profile scope, then page results. Swap profiles return only linear perpetuals. Use returned canonical symbols in subsequent tools. Example: {"profile_id":"spot","keyword":"ETH","quote":"USDT","limit":20,"offset":0}. Increase offset by limit while has_more is true. Read-only."""
        return await query('symbols', profile_id=profile_id, keyword=keyword, quote=quote, limit=limit, offset=offset)

    async def get_news() -> dict:
        """Read the shared scored news digest with source timestamps and freshness."""
        return await query('news')

    async def get_market(profile_id: ProfileId, symbol: Symbol,
                         timeframe: Literal['15m', '1h', '4h', '1d', '1w'] = '1h', limit: int = 120) -> dict:
        """Read ticker and candles for an authorized canonical symbol. Use list_symbols to find it first."""
        return await query('market', profile_id=profile_id, symbol=symbol, timeframe=timeframe, limit=limit)

    async def get_balance(profile_id: ProfileId) -> dict:
        """Read total/free/used exchange balances. Shared account balances are not MCP-owned inventory."""
        return await query('balance', profile_id=profile_id)

    async def get_positions(profile_id: ProfileId, symbol: str | None = None) -> list[dict]:
        """Read open perpetual positions for authorized symbols. base_amount is contracts times contractSize, in base-asset units; use it as expected_amount when the user requests manual-position adoption. Visibility alone does not establish task ownership. Omit symbol for allowed positions; use get_spot_inventory for sellable owned spot quantity."""
        return await query('positions', profile_id=profile_id, symbol=symbol)

    async def get_orders(profile_id: ProfileId, symbol: Symbol, limit: int = 100) -> list[dict]:
        """Read open orders without submitting/canceling/reconciling writes. Independent mode merges regular/algo orders with owned entry/exit facts: exact order_id, quantity_mode, order_status, coverage and quantity conflicts. ownership=external_or_untracked is read-only; visibility does not authorize modification. local_record missing from exchange lists is pending verification, not proof of a fill."""
        return await query('orders', profile_id=profile_id, symbol=symbol, limit=limit)

    async def get_spot_inventory(profile_id: ProfileId, symbol: Symbol) -> dict:
        """Read spot base quantity attributed to this MCP profile, available quantity and pending exits. Does not sell or cancel anything."""
        return await query('spot_inventory', profile_id=profile_id, symbol=symbol)

    async def get_trading_tools(profile_id: ProfileId) -> list[dict]:
        """Read this profile's supported schemas. Independent perpetual mode recommends open/close/edit/cancel and version 2 execute_trade_actions. Legacy entries are marked; spot and attached modes retain their existing tools. Unknown fields are rejected."""
        return await query('tools', profile_id=profile_id)

    async def buy_spot(profile_id: ProfileId, symbol: Symbol, amount: Amount, entry_price: Price,
                       reason: Reason, operation_id: OperationId) -> dict:
        """Place ONE spot limit buy. All fields are top-level, not under orders/arguments. amount is base quantity; amount*entry_price uses the profile's per-call quote allowance. Example: {"profile_id":"spot","symbol":"BTC/USDT","amount":0.001,"entry_price":50000,"reason":"approved entry","operation_id":"buy-btc-0001"}. A submitted order is not a fill. Unknown results require get_operation/get_orders, never a new ID."""
        return await execute(profile_id, symbol, 'open_position_spot_dca', {
            'orders': [{'action': 'BUY_LIMIT', 'amount': amount, 'entry_price': entry_price, 'reason': reason}],
        }, operation_id)

    async def create_spot_exit(profile_id: ProfileId, symbol: Symbol, amount: Amount,
                               exit_type: Literal['market', 'limit', 'stop_loss', 'take_profit', 'oco'],
                               reason: Reason, operation_id: OperationId, price: Price | None = None,
                               stop_loss: Price | None = None, take_profit: Price | None = None,
                               stop_limit_price: Price | None = None, take_profit_limit_price: Price | None = None) -> dict:
        """Sell only this profile's owned spot quantity. All fields are flat. market sells now; limit uses price; stop_loss/take_profit use their trigger; oco links both triggers natively and reserves amount once. Explicit *_limit_price may be required by the exchange; no execution price is guessed. Example OCO: {"profile_id":"spot","symbol":"BTC/USDT","amount":0.001,"exit_type":"oco","stop_loss":45000,"take_profit":60000,"reason":"approved exits","operation_id":"btc-exit-0001"}. Inspect inventory first; unsupported combinations fail before submission."""
        arguments = {'amount': amount, 'exit_type': exit_type, 'reason': reason, 'price': price,
                     'stop_loss': stop_loss, 'take_profit': take_profit, 'stop_limit_price': stop_limit_price,
                     'take_profit_limit_price': take_profit_limit_price}
        return await execute(profile_id, symbol, 'create_spot_exit', {k: v for k, v in arguments.items() if v is not None}, operation_id)

    async def cancel_spot_exit(profile_id: ProfileId, symbol: Symbol, exit_id: str,
                               reason: Reason, operation_id: OperationId) -> dict:
        """Cancel an owned spot exit by exit_id from its receipt/inventory, including its linked native OCO. Requires cancel scope. Flat fields; never guess an ID."""
        return await execute(profile_id, symbol, 'cancel_spot_exit', {'exit_id': exit_id, 'reason': reason}, operation_id)

    async def open(profile_id: ProfileId, symbol: Symbol, side: Literal['LONG', 'SHORT'],
                   amount: PositiveNumber, price: PositiveNumber, reason: Reason, operation_id: OperationId) -> dict:
        """Independent mode limit entry. LONG buys; SHORT sells; amount is base quantity. Multiple calls may stage entries; both directions require Hedge Mode. No attached TP/SL: after confirmed fills, close may install exits now or in a later round. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","side":"LONG","amount":0.001,"price":50000,"reason":"first stage","operation_id":"entry-stage-0001"}. submitted is not filled; inspect an unknown operation before retrying."""
        return await execute(profile_id, symbol, 'open', dict(side=side, amount=amount, price=price, reason=reason), operation_id)

    async def close(profile_id: ProfileId, symbol: Symbol, side: Literal['LONG', 'SHORT'], order_type: OrderType,
                    reason: Reason, operation_id: OperationId, amount: PositiveNumber | None = None,
                    close_all: bool | None = None, price: PositiveNumber | None = None,
                    trigger_price: PositiveNumber | None = None) -> dict:
        """Independent mode exits for an already filled LONG/SHORT side. Choose amount>0 in base units OR close_all=true. market: no prices; limit: price; stop_market/take_profit_market: trigger_price; stop_limit/take_profit_limit: both. all is supported only for market (verified submission-time quantity) or conditional market (exchange closes the actual remaining side at trigger, including later additions). Fixed TP/SL stages retain their requested quantities. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","side":"LONG","order_type":"take_profit_market","close_all":true,"trigger_price":60000,"reason":"final target","operation_id":"last-target-0001"}. status is the operation; order_status is execution evidence, never assume submitted means filled."""
        arguments = dict(side=side, order_type=order_type, reason=reason, amount=amount, close_all=close_all,
                         price=price, trigger_price=trigger_price)
        return await execute(profile_id, symbol, 'close', {k: v for k, v in arguments.items() if v is not None}, operation_id)

    async def edit(profile_id: ProfileId, symbol: Symbol, order_id: str, reason: Reason, operation_id: OperationId,
                   remaining_amount: PositiveNumber | None = None, price: PositiveNumber | None = None,
                   trigger_price: PositiveNumber | None = None) -> dict:
        """Independent mode: edit an owned live order. At least one change; remaining_amount means unfilled base quantity for BOTH entries and exits. Omitted fields stay unchanged; all cannot accept quantity. Regular LIMIT accepts price/remaining_amount, no trigger; market cannot be edited. Native edit or persisted cancel_replace is selected by capability; inspect current/previous order_id and edit_method. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","order_id":"regular:123","remaining_amount":0.001,"price":51000,"reason":"revised level","operation_id":"edit-entry-0001"}. Unknown results must be reconciled, not replaced with another operation ID."""
        arguments = dict(order_id=order_id, reason=reason, remaining_amount=remaining_amount, price=price, trigger_price=trigger_price)
        return await execute(profile_id, symbol, 'edit', {k: v for k, v in arguments.items() if v is not None}, operation_id)

    async def cancel(profile_id: ProfileId, symbol: Symbol, order_id: str, reason: Reason, operation_id: OperationId) -> dict:
        """Independent mode: cancel an owned entry/exit by its exact returned order_id. Uses regular/algo identity and follows a verified triggered child to cancel its unfilled portion. Requires cancel permission. It does not close a position. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","order_id":"algo:456","reason":"invalidated condition","operation_id":"cancel-exit-0001"}. Report only the verified receipt."""
        return await execute(profile_id, symbol, 'cancel', dict(order_id=order_id, reason=reason), operation_id)

    async def execute_trade_actions(profile_id: ProfileId, symbol: Symbol, operation_id: OperationId,
                                    contract_version: Literal[2], actions: Annotated[list[TradeAction], Field(min_length=1, max_length=20)],
                                    execution_policy: Literal['stop_on_error', 'independent'] | None = None) -> dict:
        """Independent mode ordered open/close/edit/cancel actions. Requires contract_version=2 and 1–20 flat actions. Default stop_on_error stops after failed/pending/unknown. execution_policy=independent assesses each action separately; depends_on lists unique earlier indices that must be completed (submitted is not enough). Each action refreshes its evidence internally; no preliminary query or unlock. Mixed cancel/trade requires both permissions before the first write. No rollback or replay of skipped items. open does not wait for fills."""
        arguments = {
            'contract_version': contract_version,
            'actions': [item.model_dump(exclude_none=True) if hasattr(item, 'model_dump') else item for item in actions],
        }
        if execution_policy is not None:
            arguments['execution_policy'] = execution_policy
        return await execute(profile_id, symbol, 'execute_trade_actions', arguments, operation_id)

    async def open_perpetual(profile_id: ProfileId, symbol: Symbol, side: Literal['LONG', 'SHORT'],
                             amount: Amount, entry_price: Price, reason: Reason, operation_id: OperationId,
                             stop_loss: Price | None = None, take_profit: Price | None = None) -> dict:
        """Place ONE linear perpetual limit entry with flat fields. LONG buys, SHORT sells. amount is base-asset quantity. Configured exit policy may require stop_loss/take_profit; actual exchange leverage and ownership are checked. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","side":"LONG","amount":0.001,"entry_price":50000,"stop_loss":45000,"take_profit":60000,"reason":"approved entry","operation_id":"open-btc-0001"}."""
        order = {'action': 'BUY_LIMIT' if side == 'LONG' else 'SELL_LIMIT', 'amount': amount,
                 'entry_price': entry_price, 'reason': reason, 'stop_loss': stop_loss, 'take_profit': take_profit}
        return await execute(profile_id, symbol, 'open_position_real', {'orders': [{k: v for k, v in order.items() if v is not None}]}, operation_id)

    async def close_perpetual(profile_id: ProfileId, symbol: Symbol, pos_side: Literal['LONG', 'SHORT'],
                              amount: Annotated[float, Field(ge=0, allow_inf_nan=False, description='Base quantity; 0 explicitly closes the entire owned side.')],
                              exit_type: Literal['market', 'take_profit_limit', 'stop_market'],
                              reason: Reason, operation_id: OperationId, price: Price | None = None,
                              trigger_price: Price | None = None) -> dict:
        """Reduce/close an owned perpetual side. Flat fields. market accepts no price; take_profit_limit requires price; stop_market requires trigger_price. Example: {"profile_id":"swap","symbol":"BTC/USDT:USDT","pos_side":"LONG","amount":0.001,"exit_type":"market","reason":"approved reduction","operation_id":"close-btc-0001"}. Independent conditional exits require the profile's independent exit mode."""
        order = {'action': 'CLOSE', 'pos_side': pos_side, 'amount': amount, 'exit_type': exit_type,
                 'reason': reason, 'price': price, 'trigger_price': trigger_price}
        return await execute(profile_id, symbol, 'close_position_real', {'orders': [{k: v for k, v in order.items() if v is not None}]}, operation_id)

    async def adopt_perpetual_position(profile_id: ProfileId, symbol: Symbol, pos_side: Literal['LONG', 'SHORT'],
                                       expected_amount: Amount, reason: Reason, operation_id: OperationId) -> dict:
        """After the user requests management of a manually opened position, explicitly adopt its entire current side into this profile's independent exit cycle. Read get_positions first: expected_amount must equal its current base-asset quantity, not contract count. Checks account ownership, position and outstanding orders; rejects other tasks' positions. This ONLY saves management records: it does not place/cancel orders or create TP/SL. After success, use close_perpetual for exits or protection. Example: {"profile_id":"swap","symbol":"ETH/USDT:USDT","pos_side":"SHORT","expected_amount":0.37,"reason":"user requested management of their manual position","operation_id":"adopt-eth-short-0001"}."""
        return await execute(profile_id, symbol, 'adopt_position_real', {
            'pos_side': pos_side, 'expected_amount': expected_amount, 'reason': reason,
        }, operation_id)

    async def amend_perpetual_entry(profile_id: ProfileId, symbol: Symbol, order_id: str,
                                    reason: Reason, operation_id: OperationId, entry_price: Price | None = None,
                                    amount: Amount | None = None, pos_side: Literal['LONG', 'SHORT'] | None = None) -> dict:
        """Amend one owned unfilled/partially filled entry using flat fields. amount is the new TOTAL base quantity including filled quantity. Provide entry_price and/or amount; omitted fields are retained. Never cancels/reopens to bypass unknown results."""
        arguments = {'order_id': order_id, 'reason': reason, 'entry_price': entry_price, 'amount': amount, 'pos_side': pos_side}
        return await execute(profile_id, symbol, 'update_entry_order_real', {k: v for k, v in arguments.items() if v is not None}, operation_id)

    async def update_perpetual_protection(profile_id: ProfileId, symbol: Symbol, pos_side: Literal['LONG', 'SHORT'],
                                         reason: Reason, operation_id: OperationId, stop_loss: Price | None = None,
                                         take_profit: Price | None = None) -> dict:
        """Change whole-side owned perpetual protection. Flat fields; provide stop_loss and/or take_profit, omitted values stay unchanged. This is unavailable in independent exit mode. State ACTIVE reflects the latest verification, not a guaranteed future fill."""
        arguments = {'pos_side': pos_side, 'reason': reason, 'stop_loss': stop_loss, 'take_profit': take_profit}
        return await execute(profile_id, symbol, 'update_position_protection_real', {k: v for k, v in arguments.items() if v is not None}, operation_id)

    async def cancel_order(profile_id: ProfileId, symbol: Symbol, order_id: str,
                            reason: Reason, operation_id: OperationId) -> dict:
        """Cancel one owned entry/order. All fields are flat. Requires cancel scope. Use cancel_spot_exit for a managed spot exit/OCO; do not cancel another strategy's orders."""
        # Both existing spot and perpetual registries retain this legacy tool
        # name; the gateway/runtime select the matching scoped implementation.
        return await execute(profile_id, symbol, 'cancel_orders_real', {'order_id': order_id, 'reason': reason}, operation_id)

    async def execute_trade(profile_id: ProfileId, symbol: Symbol, tool_name: str,
                            arguments: dict | str, operation_id: OperationId) -> dict:
        """Legacy dispatcher; prefer flat trade tools. arguments must match get_trading_tools for tool_name. JSON-encoded objects/arrays and bounded args wrappers are accepted. Duplicate symbol/profile/operation fields must match the gateway exactly; conflicting or unknown fields fail rather than being dropped. Example: {"profile_id":"spot","symbol":"BTC/USDT","tool_name":"open_position_spot_dca","arguments":{"orders":[{"action":"BUY_LIMIT","amount":0.001,"entry_price":50000,"reason":"approved entry"}]},"operation_id":"buy-btc-0001"}. Every financial write keeps caller-supplied operation_id; unknown outcomes are never blindly retried."""
        return await execute(profile_id, symbol, tool_name, arguments, operation_id)

    async def get_operation(profile_id: ProfileId, operation_id: OperationId) -> dict:
        """Read this caller's persisted operation result without submitting any trade again."""
        return await anyio.to_thread.run_sync(partial(service.operation, service.principal(), profile_id, operation_id))

    functions = {function.__name__: function for function in (
        list_profiles, list_symbols, get_news, get_market, get_balance, get_positions, get_orders,
        get_spot_inventory, get_trading_tools, buy_spot, create_spot_exit, cancel_spot_exit,
        open, close, edit, cancel, execute_trade_actions,
        open_perpetual, adopt_perpetual_position, close_perpetual, amend_perpetual_entry, update_perpetual_protection,
        cancel_order, execute_trade, get_operation,
    )}
    for item in TOOL_CATALOG:
        _register(server, functions[item['name']], scope=item['scope'])
