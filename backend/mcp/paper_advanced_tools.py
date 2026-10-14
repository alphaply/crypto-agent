"""Paper-friendly position, order, competition and notification adapters."""
from functools import partial
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt

from . import paper, paper_competition, paper_market, paper_orders, service
from .paper_tools import Leverage, MarketType, UserId
from .resilience import run
from .tools import Amount, OperationId, Price, _register

PAPER_ADVANCED_CATALOG = [
    {'name': name, 'scope': scope, 'description': description}
    for name, scope, description in [
        ('search_paper_symbols', 'read', 'Discover real exchange symbols and stock-token mappings before rejecting an unknown ticker'),
        ('get_paper_positions', 'read', 'Read user spot holdings or perpetual positions with explicit source and units'),
        ('paper_open_position', 'trade', 'Open LONG/SHORT using base quantity, USDT notional or USDT margin; optional limit and TP/SL'),
        ('paper_close_position', 'trade', 'Partially or fully reduce a user perpetual side with optional limit'),
        ('paper_place_order', 'trade', 'Queue a simulated spot/perpetual limit or conditional exit'),
        ('get_paper_orders', 'read', 'Read open/filled/cancelled/rejected simulated orders'),
        ('paper_cancel_order', 'trade', 'Cancel one account-owned paper order and release its reservation'),
        ('paper_edit_order', 'trade', 'Change remaining paper order price, trigger or base quantity'),
        ('paper_set_protection', 'trade', 'Set or update mutually exclusive whole/partial spot or perpetual TP/SL'),
        ('get_paper_bot_account', 'read', 'Read the bot own competition account and its stable user_id'),
        ('ensure_paper_bot_account', 'trade', 'Initialize bot own account without resetting existing capital'),
        ('get_paper_leaderboard', 'read', 'Rank public participants and compare the requesting user with the bot'),
        ('set_paper_preferences', 'trade', 'Set account display name, ranking visibility and event notifications'),
        ('scan_paper_events', 'read', 'Poll liquidation, automatic fills and rejected orders using a persistent cursor'),
    ]
]


def register(server):
    async def invoke(function, *args, write=False, **kwargs):
        return await run('execute' if write else 'read', partial(function, service.principal(), *args, **kwargs), label=function.__name__)

    async def search_paper_symbols(keyword: str = '', market_type: MarketType | None = None,
                                   exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None,
                                   limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50, offset: Annotated[StrictInt, Field(ge=0)] = 0) -> dict:
        """Query REAL public exchange catalogues. With exchange omitted, searches Binance, OKX, Bitget and Bybit; with market_type omitted, searches spot and swap. Always search unfamiliar names, MUB/MU/美光 and 腾讯/TENCENT. TENCENT/USDT:USDT can be listed on Bitget even when the default Binance venue is unavailable. Preserve the returned exchange in subsequent quote and trade calls. Static stock mappings are discovery hints, not tradability proof; only catalogue_verified listings are usable. A failed source or partial result cannot prove nonexistence. A token/perpetual quote is not an underlying stock exchange spot quote."""
        def search(caller):
            paper.require(caller)
            return paper_market.search(keyword, market_type, exchange, limit, offset)
        return await invoke(search)

    async def get_paper_positions(user_id: UserId, market_type: MarketType | None = None) -> dict:
        """Read owned positions/spot holdings with base quantity, side, entry/mark price, leverage, margin and exchange. Marks are last observed real public prices, not synthetic quotes. Use exchange and symbol from this result for close/protection; do not confuse contracts with base quantity."""
        def positions(caller):
            account = paper.get_account(caller, user_id)
            return {'user_id': user_id, 'positions': account['positions'] if market_type != 'spot' else [],
                    'spot': account['spot'] if market_type != 'swap' else [], 'available_cash': account['available_cash'],
                    'oldest_mark_at': account['oldest_mark_at'], 'execution_mode': 'paper', 'market_data_mode': 'live_public'}
        return await invoke(positions)

    async def paper_open_position(user_id: UserId, symbol: str, side: Literal['LONG', 'SHORT'], operation_id: OperationId,
                                   amount: Amount | None = None, quote_amount: Amount | None = None, margin_amount: Amount | None = None,
                                   leverage: Leverage | None = None, order_type: Literal['market', 'limit'] = 'market', price: Price | None = None,
                                   stop_loss: Price | None = None, take_profit: Price | None = None,
                                   exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Perpetual entry adapter. Supply exactly one: amount in BASE units, quote_amount as USDT NOTIONAL, or margin_amount as USDT posted MARGIN (entry fee is extra). No guessing what '100U' means. Leverage applies to this entry; defaults to user account leverage. Market entry fills immediately at real public bid/ask; limit requires price and returns an accepted pending order, not a fill. Optional stop_loss/take_profit create OCO exits on filled exposure; for limit entries they activate after fill."""
        kwargs = dict(amount=amount, quote_amount=quote_amount, margin_amount=margin_amount, leverage=leverage, exchange_name=exchange, stop_loss=stop_loss, take_profit=take_profit)
        action = 'open_' + side.lower()
        if order_type == 'limit':
            return await invoke(paper_orders.place, user_id, 'swap', symbol, action, 'limit', operation_id, price=price, write=True, **kwargs)
        if price is not None:
            raise ValueError('A market entry does not accept price')
        return await invoke(paper_orders.execute_market, user_id, 'swap', symbol, action, operation_id, write=True, **kwargs)

    async def paper_close_position(user_id: UserId, symbol: str, side: Literal['LONG', 'SHORT'], operation_id: OperationId,
                                    amount: Amount | None = None, close_all: StrictBool = False,
                                    order_type: Literal['market', 'limit'] = 'market', price: Price | None = None,
                                    exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Reduce a perpetual side. Exactly one of amount (BASE units) or close_all=true. Never opens a reverse position. Market close-all resolves current quantity atomically and cancels existing exits for that side. Limit close-all follows remaining quantity in the same position cycle; it cannot close a later reopened cycle."""
        action = 'close_' + side.lower()
        if order_type == 'limit':
            return await invoke(paper_orders.place, user_id, 'swap', symbol, action, 'limit', operation_id,
                                amount=amount, close_all=close_all, price=price, exchange_name=exchange, write=True)
        if price is not None:
            raise ValueError('A market close does not accept price')
        return await invoke(paper_orders.execute_market, user_id, 'swap', symbol, action, operation_id,
                            amount=amount, close_all=close_all, exchange_name=exchange, write=True)

    async def paper_place_order(user_id: UserId, market_type: MarketType, symbol: str,
                                action: Literal['buy', 'sell', 'open_long', 'open_short', 'close_long', 'close_short'],
                                order_type: Literal['limit', 'stop_loss', 'take_profit'], operation_id: OperationId,
                                amount: Amount | None = None, quote_amount: Amount | None = None, margin_amount: Amount | None = None,
                                close_all: StrictBool = False, leverage: Leverage | None = None, price: Price | None = None,
                                trigger_price: Price | None = None, stop_loss: Price | None = None, take_profit: Price | None = None,
                                exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Queue a paper limit entry/exit or stop_loss/take_profit exit. Limits require price; conditional exits require trigger_price and no price. Quantity is base amount; budget sizing only for entries. Entry limits reserve cash/margin+fee; spot sell limits reserve holdings. Trigger exits fill at observed bid/ask, which may differ from the trigger after a gap. Prefer paper_set_protection for an OCO TP/SL pair. Accepted does not mean filled; poll order/events."""
        return await invoke(paper_orders.place, user_id, market_type, symbol, action, order_type, operation_id,
                            amount=amount, quote_amount=quote_amount, margin_amount=margin_amount, close_all=close_all,
                            leverage=leverage, price=price, trigger_price=trigger_price, stop_loss=stop_loss, take_profit=take_profit,
                            exchange_name=exchange, write=True)

    async def get_paper_orders(user_id: UserId, state: Literal['open', 'filled', 'cancelled', 'rejected', 'all'] = 'open', limit: Annotated[StrictInt, Field(ge=1, le=200)] = 100) -> dict:
        """Read account-owned paper order state, reservation, limit/trigger, OCO group and actual fill. Order IDs are local paper IDs; never pass them to real exchange tools."""
        return await invoke(paper_orders.get_orders, user_id, state, limit)

    async def paper_cancel_order(user_id: UserId, order_id: str, operation_id: OperationId) -> dict:
        """Cancel one open paper order and release reservation; terminal orders are returned without replaying their fill. Requires paper trade scope, not live cancel scope."""
        return await invoke(paper_orders.cancel, user_id, order_id, operation_id, write=True)

    async def paper_edit_order(user_id: UserId, order_id: str, operation_id: OperationId, price: Price | None = None,
                               trigger_price: Price | None = None, amount: Amount | None = None) -> dict:
        """Edit one still-open paper order. Omitted fields remain unchanged; amount changes it to fixed base quantity. An order already filled/cancelled or changed concurrently cannot be edited. Inspect its current state instead of submitting a replacement blindly."""
        if price is None and trigger_price is None and amount is None:
            raise ValueError('Provide price, trigger_price and/or amount')
        return await invoke(paper_orders.edit, user_id, order_id, operation_id, price, trigger_price, amount, write=True)

    async def paper_set_protection(user_id: UserId, market_type: MarketType, symbol: str, side: Literal['LONG', 'SHORT'], operation_id: OperationId,
                                   stop_loss: Price | None = None, take_profit: Price | None = None, amount: Amount | None = None,
                                   close_all: StrictBool = True, exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None) -> dict:
        """Create/update OCO TP/SL for owned exposure. Spot uses side LONG. Default close_all follows the current position cycle; amount specifies a partial fixed exit. Omitted TP/SL retains that leg if already configured. To remove a leg, cancel its order_id. After one leg fills the sibling is cancelled atomically. No real exchange order is submitted."""
        return await invoke(paper_orders.protection, user_id, market_type, symbol, side, operation_id,
                            stop_loss, take_profit, amount, close_all, exchange, write=True)

    async def get_paper_bot_account() -> dict:
        """Read your OWN bot competition account. Use its returned user_id to trade for yourself, never a user's ID. The bot account is provisioned with a paper key and during upgrade; paper_open_position/paper_trade work with its user_id. It has the same fee, margin, debt and matching rules as users."""
        return await invoke(paper_competition.bot_account)

    async def ensure_paper_bot_account(operation_id: OperationId) -> dict:
        """Initialize your own configured bot account if missing. Existing funds, debt and positions are retained; it does not refill a losing bot account."""
        return await invoke(paper_competition.ensure_bot, operation_id, write=True)

    async def get_paper_leaderboard(limit: Annotated[StrictInt, Field(ge=1, le=200)] = 20,
                                    metric: Literal['return_pct', 'pnl'] = 'return_pct', user_id: UserId | None = None) -> dict:
        """Public opt-out competition ranking. Pass trusted sender user_id to get self/bot ranks for PK. Score is capital-adjusted net trading PnL or return percentage; borrowing and admin top-ups do not create profit. Other users' private holdings, cash and debt are omitted; participant IDs are opaque. Marks have timestamps and can be stale."""
        return await invoke(paper_competition.leaderboard, limit, metric, user_id)

    async def set_paper_preferences(user_id: UserId, operation_id: OperationId, leaderboard_visible: StrictBool | None = None,
                                    notifications_enabled: StrictBool | None = None, name: Annotated[str, Field(min_length=1, max_length=100)] | None = None) -> dict:
        """Set the user's public ranking display name, ranking participation or event notification preference. Does not change funds or route messages to a different user."""
        return await invoke(paper_competition.preferences, user_id, operation_id, leaderboard_visible, notifications_enabled, name, write=True)

    async def scan_paper_events(after_id: Annotated[StrictInt, Field(ge=0)] = 0, limit: Annotated[StrictInt, Field(ge=1, le=200)] = 100, user_id: UserId | None = None) -> dict:
        """QQBot scheduler polling interface for liquidation, automatic order fills and order rejection notifications. Returns events, next_cursor and has_more. Route using recipient_user_id from trusted stored accounts. Deduplicate by delivery_key; only persist next_cursor after delivery or durable queueing. An empty page may advance the cursor past non-notifiable events. This tool does NOT send messages; the bot integration owns scheduling and delivery."""
        return await invoke(paper_competition.scan, after_id, limit, user_id)

    funcs = {f.__name__: f for f in [search_paper_symbols, get_paper_positions, paper_open_position, paper_close_position,
                                    paper_place_order, get_paper_orders, paper_cancel_order, paper_edit_order, paper_set_protection,
                                    get_paper_bot_account, ensure_paper_bot_account, get_paper_leaderboard, set_paper_preferences, scan_paper_events]}
    for item in PAPER_ADVANCED_CATALOG:
        _register(server, funcs[item['name']], scope=item['scope'])
