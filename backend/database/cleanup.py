from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
import json
from backend import storage


def _recovery_retained_rows(conn, config_id, tables, *, observed=None):
    """Keep facts needed by accepted handoffs or unfinished original operations."""
    from backend.utils.background_receipts import _pending
    from backend.utils.trade_operations import _decode_receipt

    grants = list(conn.execute('SELECT operation_id,payload FROM management_recovery_grants WHERE source_owner=%s',
                               (config_id,))) if 'management_recovery_grants' in tables else []
    operations = {row[0] for row in grants}
    scopes = [json.loads(row[1]) for row in grants]
    cached_rows = {}

    def rows(table):
        if table not in tables:
            return []
        if table in cached_rows:
            return cached_rows[table]
        cursor = conn.execute(f'SELECT storage_id AS cleanup_rowid,xmin::text AS cleanup_version,* FROM {table} WHERE config_id=%s', (config_id,))
        columns = [column[0] for column in cursor.description]
        result = [dict(zip(columns, tuple(row))) for row in cursor]
        cached_rows[table] = result
        if observed is not None:
            observed[table] = {row['cleanup_rowid']: row['cleanup_version'] for row in result}
        return result

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
    active_since = None
    if 'agent_run_queue' in tables:
        active_since = conn.execute("""SELECT EXTRACT(EPOCH FROM MIN(COALESCE(started_at, enqueued_at)))
            FROM agent_run_queue WHERE config_id=%s AND status IN ('CLAIMED','RUNNING')""", (config_id,)).fetchone()[0]
    unfinished = [row for row in runs if row['status'] == 'submitting' or _pending({'status': row['status']})
                  or _pending(_decode_receipt(row.get('result')))
                  or active_since is not None and row['updated_at'] >= float(active_since)]
    operations.update(row['operation_id'] for row in unfinished)
    if not operations and active_since is None:
        return {}
    unfinished_symbols = {str(row['symbol']).split(':')[0] for row in unfinished}
    mappings = rows('unified_trade_children')
    while True:
        previous = set(operations)
        for mapping in mappings:
            if mapping['parent_operation_id'] in operations or mapping['child_operation_id'] in operations:
                operations.update((mapping['parent_operation_id'], mapping['child_operation_id']))
        for row in runs:
            if row['operation_id'] in operations and row.get('result'):
                operations.update(operation_refs(_decode_receipt(row['result'])))
        if operations == previous:
            break

    retained = {}
    episodes, order_keys = set(), set()
    requested_orders = set()

    def order_refs(value):
        if isinstance(value, dict):
            direct = {str(item) for key, item in value.items() if item and key in {
                'id', 'order_id', 'exchange_order_id', 'execution_order_id', 'target_id'}}
            return direct | set().union(*(order_refs(item) for key, item in value.items()
                                        if key not in {'account', 'account_summary'}))
        if isinstance(value, list):
            return set().union(*(order_refs(item) for item in value))
        return set()

    requests = {row['operation_id']: row['request_json'] for row in rows('trade_operation_requests')
                if row['operation_id'] in operations}
    for row in runs:
        if row['operation_id'] not in operations:
            continue
        for raw in (row.get('result'), requests.get(row['operation_id'])):
            if raw:
                requested_orders.update((row['symbol'], identity.removeprefix('regular:').removeprefix('algo:'))
                                        for identity in order_refs(_decode_receipt(raw)))

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
        current = str(value.get('state') or '').upper() not in {'CLOSED', 'CANCELLED', 'CANCELED'}
        unfinished_scope = active_since is not None or str(row['symbol']).split(':')[0] in unfinished_symbols
        if not (in_scope(row, value) or operation_refs(value) & operations or
                value.get('episode_id') in episodes or current and unfinished_scope):
            continue
        keep('real_protection_plans', row)
        if value.get('episode_id'):
            episodes.add(value['episode_id'])
        for channel, records in (('regular', value.get('entries', []) + value.get('exits', []) +
                                 ([value['exit_order']] if value.get('exit_order') else [])),
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
        raw_identity = identity.removeprefix('regular:').removeprefix('algo:')
        if (operation_refs(value) & operations or value.get('episode_id') in episodes or key in order_keys
                or (row['symbol'], raw_identity) in requested_orders):
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
            active_order = (active_since is not None and table == 'orders' and
                            str(row.get('status') or '').upper() in {'OPEN', 'PARTIAL', 'PENDING', 'UNKNOWN'})
            if (active_order or (row.get('symbol'), identity) in requested_orders or
                    any(symbol == row.get('symbol') and original == identity for _, symbol, original in regular_orders)):
                keep(table, row)
    # Keep the current simulated balance/positions while an original action can
    # still settle. Completed historical orders remain ordinary deletion targets.
    if unfinished or active_since is not None:
        for table in ('mock_accounts', 'trading_rules', 'spot_budget_cycles'):
            for row in rows(table):
                keep(table, row)
        for table in ('mock_orders', 'mock_positions', 'mock_exit_orders', 'position_history'):
            for row in rows(table):
                if str(row.get('status') or '').upper() not in {'CLOSED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED'}:
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
                "spot_budget_cycles", "memory_update_jobs", "spot_cost_basis",
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
            observed = {}
            retained = _recovery_retained_rows(conn, config_id, existing_tables, observed=observed)

            def delete_rows(table):
                protected = sorted(retained.get(table, set()))
                if table in observed:
                    # An in-flight exchange acknowledgement can append evidence
                    # or update a receipt after retention was inspected. Delete
                    # only the exact row versions inspected, never those new facts.
                    candidates = {identity: version for identity, version in observed[table].items()
                                  if identity not in retained.get(table, set())}
                    if not candidates:
                        return 0
                    return cursor.execute(f'''DELETE FROM {table} AS target
                        USING unnest(%s::bigint[], %s::text[]) AS selected(identity, version)
                        WHERE target.config_id=%s AND target.storage_id=selected.identity
                          AND target.xmin::text=selected.version''',
                        (list(candidates), list(candidates.values()), config_id)).rowcount
                if protected:
                    placeholders = ','.join('%s' for _ in protected)
                    return cursor.execute(f'DELETE FROM {table} WHERE config_id=%s AND storage_id NOT IN ({placeholders})',
                                          (config_id, *protected)).rowcount
                return cursor.execute(f'DELETE FROM {table} WHERE config_id=%s', (config_id,)).rowcount

            cleanup = {
                f"{table}_deleted": delete_rows(table)
                for table in (
                    "chat_sessions", "mock_accounts", "mock_balance_history", "mock_orders", "orders",
                    "summaries", "token_usage", "daily_summaries", "short_memories", "position_history",
                )
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
                "spot_budget_cycles", "memory_update_jobs", "spot_cost_basis",
            ):
                if table in existing_tables:
                    cleanup[f"{table}_deleted"] = delete_rows(table)
            for table, protected in retained.items():
                if f"{table}_deleted" in cleanup:
                    cleanup[f"{table}_retained_for_recovery"] = len(protected)

            if connection is None:
                conn.commit()
            return cleanup
