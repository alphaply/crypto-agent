"""Read-only ownership checks before cancelling a perpetual exchange order."""
from __future__ import annotations

import json


def symbol_aliases(market_tool, symbol: str) -> tuple[str, ...]:
    """Accept the configured shorthand and its exchange-canonical symbol only."""
    aliases = [str(symbol)]
    exchange = getattr(market_tool, 'exchange', None)
    if exchange is not None and hasattr(exchange, 'market'):
        market = exchange.market(symbol)
        spot = (getattr(market_tool,'market_type',None) == 'spot' or
                getattr(exchange,'options',{}).get('defaultType') == 'spot')
        if not spot and ':' not in str(symbol) and not market.get('contract'):
            market = exchange.market(f'{symbol}:USDT')
        if not spot and (market.get('contract') is False or market.get('linear') is False):
            raise ValueError('Order ownership requires a linear perpetual symbol')
        canonical = market.get('symbol')
        if canonical:
            aliases.append(str(canonical))
            if not spot and str(canonical).endswith(':USDT'):
                aliases.append(str(canonical).split(':')[0])
    return tuple(dict.fromkeys(aliases))


def assert_owned_perpetual_order(market_tool, symbol: str, order_id: str, config_id: str | None = None) -> dict:
    """Reject unknown/foreign IDs without contacting an exchange cancellation API.

    Account-scoped execution links override older local order logs. Historical
    REAL creation logs remain usable when a pre-ledger task has no newer link.
    """
    from backend import database
    from backend.utils.execution_ledger import account_scope

    config_id = str(config_id or getattr(market_tool, 'config_id', '') or '')
    if not config_id or not str(order_id).strip():
        raise ValueError('A configuration and owned order ID are required to cancel a perpetual order')
    scope = account_scope(market_tool.exchange, config_id)
    aliases = symbol_aliases(market_tool, symbol)
    placeholders = ','.join('?' for _ in aliases)
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'execution_order_links' in tables:
            links = conn.execute(f'SELECT config_id,role,payload FROM execution_order_links WHERE account_scope=? '
                                 f'AND symbol IN ({placeholders}) AND order_id=?',
                                 (scope, *aliases, str(order_id))).fetchall()
            if links:
                if any(row['config_id'] != config_id for row in links):
                    raise ValueError('Order belongs to another configuration; cancellation rejected')
                metadata = json.loads(links[0]['payload'])
                trigger = links[0]['role'] == 'stop_loss' or (links[0]['role'] == 'take_profit' and metadata.get('trigger_price') is not None)
                return {'source': 'execution_order_links', 'role': links[0]['role'], 'account_scope': scope, 'trigger': trigger}
        owned_plan = None
        if 'real_protection_plans' in tables:
            for row in conn.execute(f'SELECT config_id,payload FROM real_protection_plans WHERE symbol IN ({placeholders})', aliases):
                plan = json.loads(row['payload'])
                records = [*plan.get('entries', []), *plan.get('legs', []), *plan.get('exits', [])]
                if plan.get('exit_order'):
                    records.append(plan['exit_order'])
                match = next((record for record in records if str(order_id) in
                              {str(record.get('id')), str(record.get('execution_order_id'))}), None)
                if not match:
                    continue
                if plan.get('account_scope') and plan['account_scope'] != scope:
                    if row['config_id'] == config_id:
                        raise ValueError('Order belongs to previous exchange credentials; restore its original account')
                    continue
                if row['config_id'] != config_id:
                    raise ValueError('Order belongs to another configuration; cancellation rejected')
                owned_plan = {'source': 'real_protection_plans', 'account_scope': scope,
                              'role': match.get('exit_type') or match.get('kind') or 'entry',
                              'trigger': match.get('kind') in {'sl','tp'} or match.get('exit_type') == 'stop_market'}
        if owned_plan:
            return owned_plan
        if 'orders' in tables:
            row = conn.execute(f"SELECT id FROM orders WHERE order_id=? AND config_id=? AND symbol IN ({placeholders}) "
                               "AND trade_mode='REAL' AND COALESCE(event_type,'ORDER_CREATED') IN ('ORDER_CREATED','CLOSE_ORDER_CREATED') "
                               "AND UPPER(COALESCE(side,'')) NOT LIKE 'CANCEL%' LIMIT 1",
                               (str(order_id), config_id, *aliases)).fetchone()
            if row:
                return {'source': 'legacy_order_log', 'account_scope': scope, 'role': 'legacy'}
    raise ValueError('Unknown order ownership for this configuration and symbol; cancellation rejected')
