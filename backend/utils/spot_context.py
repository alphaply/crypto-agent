"""Explicit task ownership for the spot account snapshot shown to the agent."""

from backend import database
from backend.utils.exit_orders import parse_order_reference


def scope_spot_accounts(config_id: str, accounts: dict[str, dict]) -> dict[str, dict]:
    with database.get_db_conn() as conn:
        owned = {(str(row['symbol']), str(row['order_id'])) for row in conn.execute(
            "SELECT symbol, order_id FROM orders WHERE config_id=%s AND trade_mode='SPOT_DCA' "
            "AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'", (config_id,))}
    scoped = {}
    for symbol, account in accounts.items():
        own_orders, external_orders = [], []
        for order in account.get('real_open_orders') or []:
            item = {**order, 'symbol': order.get('symbol') or symbol}
            channel, native_id = parse_order_reference(item.get('order_id') or item.get('id') or '')
            channel = channel or item.get('exchange_channel') or 'regular'
            # Creation records store native IDs; snapshots carry regular:<id>.
            # Match only regular spot orders and keep the full reference for cancellation.
            key = (item['symbol'], native_id)
            (own_orders if channel == 'regular' and key in owned else external_orders).append(item)
        scoped[symbol] = {**account, 'real_open_orders': own_orders,
                          'external_open_orders': external_orders}
    return scoped
