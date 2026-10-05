"""Explicit task ownership for the spot account snapshot shown to the agent."""

from backend import database


def scope_spot_accounts(config_id: str, accounts: dict[str, dict]) -> dict[str, dict]:
    with database.get_db_conn() as conn:
        owned = {(str(row['symbol']), str(row['order_id'])) for row in conn.execute(
            "SELECT symbol, order_id FROM orders WHERE config_id=? AND trade_mode='SPOT_DCA' "
            "AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'", (config_id,))}
    scoped = {}
    for symbol, account in accounts.items():
        own_orders, external_orders = [], []
        for order in account.get('real_open_orders') or []:
            item = {**order, 'symbol': order.get('symbol') or symbol}
            key = (item['symbol'], str(item.get('order_id') or item.get('id') or ''))
            (own_orders if key in owned else external_orders).append(item)
        scoped[symbol] = {**account, 'real_open_orders': own_orders,
                          'external_open_orders': external_orders}
    return scoped
