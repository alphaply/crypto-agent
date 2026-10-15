from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
import json
from backend import storage


def _recovery_retained_rows(conn, config_id, tables):
    """Keep original execution facts referenced by explicitly accepted handoffs."""
    if 'management_recovery_grants' not in tables:
        return {}
    grants = list(conn.execute('SELECT operation_id,payload FROM management_recovery_grants WHERE source_owner=%s',
                               (config_id,)))
    if not grants:
        return {}
    operations = {row[0] for row in grants}
    scopes = [json.loads(row[1]) for row in grants]

    def rows(table):
        if table not in tables:
            return []
        cursor = conn.execute(f'SELECT storage_id AS cleanup_rowid,* FROM {table} WHERE config_id=%s', (config_id,))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, tuple(row))) for row in cursor]

    def payload(row):
        try:
            return json.loads(row.get('payload') or '{}')
        except (TypeError, ValueError):
            return {}

    def operation_refs(value):
        if isinstance(value, dict):
            direct = {str(item) for key, item in value.items() if item and key in {
                'operation_id', 'execution_operation_id', 'parent_operation_id', 'cancel_operation_id', 'last_operation_id'}}
            return direct | set().union(*(operation_refs(item) for key, item in value.items()
                                        if key not in {'account', 'account_summary'}))
        if isinstance(value, list):
            return set().union(*(operation_refs(item) for item in value))
        return set()

    runs = rows('trade_action_runs')
    mappings = rows('unified_trade_children')
    while True:
        previous = set(operations)
        for mapping in mappings:
            if mapping['parent_operation_id'] in operations or mapping['child_operation_id'] in operations:
                operations.update((mapping['parent_operation_id'], mapping['child_operation_id']))
        for row in runs:
            if row['operation_id'] in operations and row.get('result'):
                operations.update(operation_refs(json.loads(row['result'])))
        if operations == previous:
            break

    retained = {}
    episodes, order_keys = set(), set()

    def keep(table, row):
        retained.setdefault(table, set()).add(row['cleanup_rowid'])

    def in_scope(row, value):
        for scope in scopes:
            same_symbol = (str(row.get('symbol')).split(':')[0] == str(scope.get('symbol')).split(':')[0])
            if scope.get('market_type') == 'spot':
                same_symbol = str(row.get('symbol')).split('/')[0] == str(scope.get('symbol')).split('/')[0]
            side = row.get('side') or value.get('side')
            account = row.get('account_scope') or value.get('account_scope')
            if (same_symbol and (scope.get('side') == 'BOTH' or side == scope.get('side')) and
                    (not account or not scope.get('account_scope') or account == scope['account_scope'])):
                return True
        return False

    for table in ('trade_action_runs', 'trade_operation_requests', 'mock_trade_operations', 'spot_budget_reservations'):
        for row in runs if table == 'trade_action_runs' else rows(table):
            if row['operation_id'] in operations:
                keep(table, row)
    for row in rows('trade_operation_evidence'):
        if row['operation_id'] in operations:
            episodes.add(row['episode_id'])
    for row in rows('real_protection_plans'):
        value = payload(row)
        if not in_scope(row, value):
            continue
        keep('real_protection_plans', row)
        if value.get('episode_id'):
            episodes.add(value['episode_id'])
        for channel, records in (('regular', value.get('entries', []) + value.get('exits', [])),
                                 ('algo', value.get('legs', []))):
            for record in records:
                if record.get('id'):
                    identity = str(record['id'])
                    order_keys.add((value.get('account_scope'), row['symbol'],
                                    identity if identity.startswith(('regular:', 'algo:')) else f'{record.get("channel", channel)}:{identity}'))
    for row in rows('real_protection_events'):
        value = payload(row)
        if (value.get('episode_id') in episodes or operation_refs(value) & operations or
                not value.get('episode_id') and in_scope(row, value)):
            keep('real_protection_events', row)
    for row in rows('execution_order_links'):
        value = payload(row)
        identity = str(row['order_id'])
        channel = value.get('channel', 'regular')
        key = (row['account_scope'], row['symbol'], identity if identity.startswith(('regular:', 'algo:')) else f'{channel}:{identity}')
        if operation_refs(value) & operations or value.get('episode_id') in episodes or key in order_keys:
            keep('execution_order_links', row)
            order_keys.add(key)
            if value.get('episode_id'):
                episodes.add(value['episode_id'])
    regular_orders = {(scope, symbol, identity.removeprefix('regular:')) for scope, symbol, identity in order_keys
                      if identity.startswith('regular:')}
    for row in rows('execution_fills'):
        if (row['account_scope'], row['symbol'], str(row['order_id'])) in regular_orders:
            keep('execution_fills', row)
    for table in ('execution_episodes', 'execution_position_history'):
        for row in rows(table):
            value = payload(row)
            if any(identity in episodes for identity in (row.get('episode_id'), value.get('episode_id'), value.get('plan_episode_id'))):
                keep(table, row)
    for table in ('orders', 'trade_history', 'spot_order_fills'):
        for row in rows(table):
            identity = str(row.get('order_id') or '').removeprefix('regular:')
            if any(symbol == row.get('symbol') and original == identity for _, symbol, original in regular_orders):
                keep(table, row)
    return retained


class ConfigCleanupStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], now_factory: Callable[[], str]):
        self._conn_factory = conn_factory
        self._now_factory = now_factory

    def get_dependency_counts(self, config_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            tables = [
                "chat_sessions",
                "mock_accounts",
                "mock_balance_history",
                "mock_orders",
                "orders",
                "summaries",
                "token_usage",
                "daily_summaries",
                "short_memories",
                "position_history",
                "execution_fills",
                "execution_order_links",
                "execution_episodes",
                "execution_position_history",
                "real_protection_plans",
                "real_protection_events",
                "mock_positions", "mock_exit_orders", "mock_trade_operations",
                "trade_action_runs", "trade_operation_requests", "trading_rules", "trading_rule_revisions", "trading_rule_operations",
                "spot_budget_reservations", "spot_order_fills", "dca_daily_snapshots", "trade_history",
                "spot_budget_cycles", "memory_update_jobs",
            ]
            existing_tables = storage.table_names(conn)
            counts = {}
            for table in tables:
                if table not in existing_tables:
                    continue
                counts[table] = cursor.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE config_id = %s",
                    (config_id,),
                ).fetchone()[0]

            counts["open_mock_orders"] = cursor.execute(
                "SELECT COUNT(*) FROM mock_orders WHERE config_id = %s AND status = 'OPEN'",
                (config_id,),
            ).fetchone()[0]
            counts["open_orders"] = cursor.execute(
                "SELECT COUNT(*) FROM orders WHERE config_id = %s AND status = 'OPEN'",
                (config_id,),
            ).fetchone()[0]
            return counts

    def soft_delete_runtime_data(self, config_id: str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()

            deleted_chat_sessions = cursor.execute(
                "DELETE FROM chat_sessions WHERE config_id = %s",
                (config_id,),
            ).rowcount
            deleted_mock_accounts = cursor.execute(
                "DELETE FROM mock_accounts WHERE config_id = %s",
                (config_id,),
            ).rowcount
            deleted_mock_balance_history = cursor.execute(
                "DELETE FROM mock_balance_history WHERE config_id = %s",
                (config_id,),
            ).rowcount

            closed_open_mock_orders = cursor.execute(
                "UPDATE mock_orders SET status = 'CLOSED', close_time = %s WHERE config_id = %s AND status = 'OPEN'",
                (self._now_factory(), config_id),
            ).rowcount
            cancelled_open_orders = cursor.execute(
                "UPDATE orders SET status = 'CANCELLED' WHERE config_id = %s AND status = 'OPEN'",
                (config_id,),
            ).rowcount

            conn.commit()

            return {
                "chat_sessions_deleted": deleted_chat_sessions,
                "mock_accounts_deleted": deleted_mock_accounts,
                "mock_balance_history_deleted": deleted_mock_balance_history,
                "open_mock_orders_closed": closed_open_mock_orders,
                "open_orders_cancelled": cancelled_open_orders,
            }

    def purge_all_data(self, config_id: str, *, connection: storage.Connection | None = None):
        """Purge local data, joining the caller's transaction when supplied."""
        with nullcontext(connection) if connection is not None else self._conn_factory() as conn:
            cursor = conn.cursor()
            existing_tables = storage.table_names(conn)
            retained = _recovery_retained_rows(conn, config_id, existing_tables)

            def delete_rows(table):
                protected = sorted(retained.get(table, set()))
                if protected:
                    placeholders = ','.join('%s' for _ in protected)
                    return cursor.execute(f'DELETE FROM {table} WHERE config_id=%s AND storage_id NOT IN ({placeholders})',
                                          (config_id, *protected)).rowcount
                return cursor.execute(f'DELETE FROM {table} WHERE config_id=%s', (config_id,)).rowcount

            cleanup = {
                "chat_sessions_deleted": cursor.execute(
                    "DELETE FROM chat_sessions WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "mock_accounts_deleted": cursor.execute(
                    "DELETE FROM mock_accounts WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "mock_balance_history_deleted": cursor.execute(
                    "DELETE FROM mock_balance_history WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "mock_orders_deleted": cursor.execute(
                    "DELETE FROM mock_orders WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "orders_deleted": delete_rows('orders'),
                "summaries_deleted": cursor.execute(
                    "DELETE FROM summaries WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "token_usage_deleted": cursor.execute(
                    "DELETE FROM token_usage WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "daily_summaries_deleted": cursor.execute(
                    "DELETE FROM daily_summaries WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "short_memories_deleted": cursor.execute(
                    "DELETE FROM short_memories WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
                "position_history_deleted": cursor.execute(
                    "DELETE FROM position_history WHERE config_id = %s",
                    (config_id,),
                ).rowcount,
            }
            for table in (
                "execution_fills",
                "execution_order_links",
                "execution_episodes",
                "execution_position_history",
                "real_protection_plans",
                "real_protection_events",
                "mock_positions", "mock_exit_orders", "mock_trade_operations",
                "trade_action_runs", "trade_operation_requests", "trading_rules", "trading_rule_revisions", "trading_rule_operations",
                "spot_budget_reservations", "spot_order_fills", "dca_daily_snapshots", "trade_history",
                "spot_budget_cycles", "memory_update_jobs",
            ):
                if table in existing_tables:
                    cleanup[f"{table}_deleted"] = delete_rows(table)
                    if retained.get(table):
                        cleanup[f"{table}_retained_for_recovery"] = len(retained[table])
            if retained.get('orders'):
                cleanup['orders_retained_for_recovery'] = len(retained['orders'])

            if connection is None:
                conn.commit()
            return cleanup
