"""Flat QQBot paper MCP contracts, isolated from the live trade dispatcher."""
from __future__ import annotations

from functools import partial
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from . import paper, service
from .resilience import run
from .tools import Amount, OperationId, Price, _register

UserId = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_.:@-]{1,100}$', description='Stable external user ID as a STRING, e.g. the authenticated QQ sender ID. Always use the sender ID supplied by the trusted bot, never an ID invented by the model.')]
Leverage = Annotated[StrictInt, Field(ge=1, le=125)]
MarketType = Literal['spot', 'swap']

PAPER_TOOL_CATALOG = [
    {'name': name, 'scope': scope, 'description': description}
    for name, scope, description in [
        ('get_paper_config', 'read', 'Read paper defaults and supported actions'),
        ('create_paper_account', 'trade', 'Create or retrieve a user paper account without resetting funds'),
        ('get_paper_account', 'read', 'Read balances, debt, holdings and last observed positions for a user'),
        ('get_paper_market', 'read', 'Read public spot/perpetual ticker and candles without exchange credentials'),
        ('paper_trade', 'trade', 'Immediately simulate a market fill at public bid/ask for a user'),
        ('set_paper_leverage', 'trade', 'Set user default leverage for new isolated entries'),
        ('paper_bank', 'trade', 'Borrow simulated USDT or repay debt for a user'),
        ('get_paper_history', 'read', 'Read user trades, borrowing, liquidation and admin adjustment history'),
    ]
]


def register_paper_tools(server):
    async def invoke(function, *args, write=False, **kwargs):
        return await run('execute' if write else 'read', partial(function, service.principal(), *args, **kwargs), label=function.__name__)

    async def get_paper_config() -> dict:
        """Start here with a paper key. USDT-only spot and linear perpetual paper trading. User IDs are strings scoped by the key's namespace. Shared intelligence is available through get_news. The key cannot access live profiles. Bank loans have no interest; debt remains after liquidation."""
        def config(caller):
            namespace = paper.require(caller)
            return {'paper': True, 'namespace': namespace, 'currency': 'USDT', 'settings': paper.settings(),
                    'spot_actions': ['buy', 'sell'], 'swap_actions': ['open_long', 'open_short', 'close_long', 'close_short'],
                    'order_types': ['market', 'limit', 'stop_loss', 'take_profit'], 'margin_mode': 'isolated', 'amount_units': 'base',
                    'market_data_mode': 'live_public', 'market_data_is_simulated': False, 'execution_mode': 'paper',
                    'bot_user_id': paper.settings()['bot_user_id'],
                    'maintenance_interval_seconds': 15, 'interest_rate': 0}
        return await invoke(config)

    async def create_paper_account(user_id: UserId, operation_id: OperationId, name: Annotated[str, Field(max_length=100)] = '') -> dict:
        """Create the user's paper account using configured initial cash. Existing accounts are returned without resetting cash, debt or positions. Use the authenticated QQ sender ID, and a stable operation_id derived from the bot message/event ID."""
        return await invoke(paper.create_account, user_id, operation_id, name, write=True)

    async def get_paper_account(user_id: UserId) -> dict:
        """Read this user's paper cash, equity, net equity after debt, spot holdings and isolated LONG/SHORT positions. Mark prices are last observed, with oldest_mark_at. Background valuation/liquidation checks run approximately every 15s plus market/network time; stale marks must not be described as live equity."""
        return await invoke(paper.get_account, user_id)

    async def get_paper_market(symbol: str, market_type: MarketType = 'swap', timeframe: str = '1h', limit: Annotated[StrictInt, Field(ge=1, le=200)] = 120,
                               exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Read REAL public production exchange ticker and OHLCV, NOT simulated/testnet quotes. 'Paper' only describes the execution account. The source, market_data_mode=live_public and market_data_is_simulated=false make this explicit. Discover unknown stock tokens such as MUB with search_paper_symbols first; report token/venue prices accurately rather than claiming they are NASDAQ spot stock quotes. Timeframes: 1m,5m,15m,1h,4h,1d,1w. No exchange API key is needed."""
        def read(caller):
            paper.require(caller)
            from .paper_market import resolve_quote
            return resolve_quote(symbol, market_type, timeframe, limit, exchange)
        return await invoke(read)

    async def paper_trade(user_id: UserId, market_type: MarketType, symbol: str,
                          action: Literal['buy', 'sell', 'open_long', 'open_short', 'close_long', 'close_short'],
                          operation_id: OperationId, amount: Amount | None = None, leverage: Leverage | None = None,
                          quote_amount: Amount | None = None, margin_amount: Amount | None = None, close_all: bool = False,
                          stop_loss: Price | None = None, take_profit: Price | None = None,
                          exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Execute one simulated market trade at REAL public production exchange bid/ask. Exactly one sizing input: amount in BASE quantity; quote_amount in USDT notional (spot buy includes fee); margin_amount in USDT margin for perpetual entry, with fee extra; or close_all=true for exits. Spot buy/sell never has leverage. Perpetual entries may have leverage and attached stop_loss/take_profit. Prefer paper_open_position/paper_close_position for explicit LONG/SHORT and units. For LIMIT orders use paper_place_order; for OCO protection use paper_set_protection. A stable operation_id makes identical retries return the original fill without another market request. Paper refers to execution and balances, not to quote authenticity."""
        from .paper_orders import execute_market
        return await invoke(execute_market, user_id, market_type, symbol, action, operation_id, amount=amount, leverage=leverage,
                            quote_amount=quote_amount, margin_amount=margin_amount, close_all=close_all,
                            stop_loss=stop_loss, take_profit=take_profit, exchange_name=exchange, write=True)

    async def set_paper_leverage(user_id: UserId, leverage: Leverage, operation_id: OperationId) -> dict:
        """Set default leverage for subsequent paper perpetual entries. Does not resize existing isolated margin. Actual maximum is get_paper_config.settings.max_leverage."""
        return await invoke(paper.set_leverage, user_id, leverage, operation_id, write=True)

    async def paper_bank(user_id: UserId, action: Literal['borrow', 'repay'], amount: Amount, operation_id: OperationId) -> dict:
        """Borrow virtual USDT or repay debt. Borrow adds equal cash and debt; it does not increase net equity. Loans have zero interest and remain after liquidation. Repayment needs enough free cash and cannot exceed debt. Administrator can edit cash, debt and loan limit."""
        return await invoke(paper.bank, user_id, action, amount, operation_id, write=True)

    async def get_paper_history(user_id: UserId, limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50) -> list[dict]:
        """Read the user's latest paper events, including fills, loans, liquidation and admin adjustments."""
        return await invoke(paper.history, user_id, limit)

    functions = {f.__name__: f for f in [get_paper_config, create_paper_account, get_paper_account, get_paper_market,
                                        paper_trade, set_paper_leverage, paper_bank, get_paper_history]}
    for item in PAPER_TOOL_CATALOG:
        _register(server, functions[item['name']], scope=item['scope'])
    from .paper_advanced_tools import register
    register(server)
