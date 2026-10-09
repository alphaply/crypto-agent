"""Keep MCP account and order lifecycles attached during configuration edits."""
from __future__ import annotations

import json


def owned_symbols(conn, profile):
    """Persisted task evidence, independent of its current symbol policy."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = set()
    for table in ('orders', 'spot_budget_reservations', 'real_protection_plans', 'execution_order_links', 'trade_action_runs'):
        if table in tables:
            result.update(str(row[0]) for row in conn.execute(
                f'SELECT DISTINCT symbol FROM {table} WHERE config_id=? AND symbol IS NOT NULL',
                ('mcp:' + profile['profile_id'],),
            ) if row[0])
    return sorted(result)


def active_lifecycle(conn, profile, symbols=None, *, excluded_symbols=None):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    config_id = 'mcp:' + profile['profile_id']
    if 'mcp_operations' in tables and conn.execute(
        "SELECT 1 FROM mcp_operations WHERE profile_id=? AND state IN ('started','unknown','pending') LIMIT 1",
        (profile['profile_id'],),
    ).fetchone():
        return True
    if 'trade_action_runs' in tables and conn.execute(
        "SELECT 1 FROM trade_action_runs WHERE config_id=? AND status IN ('running','unknown','pending') LIMIT 1", (config_id,),
    ).fetchone():
        return True
    def affected(symbol):
        canonical = str(symbol).split(':')[0]
        if excluded_symbols is not None and canonical in {value.split(':')[0] for value in excluded_symbols}:
            return False
        return symbols is None or canonical in {value.split(':')[0] for value in symbols}
    if 'real_protection_plans' in tables:
        for row in conn.execute('SELECT symbol,payload FROM real_protection_plans WHERE config_id=?', (config_id,)):
            if affected(row['symbol']) and json.loads(row['payload']).get('state') != 'DONE':
                return True
    if profile['market_type'] == 'spot':
        if 'orders' in tables:
            for row in conn.execute("SELECT order_id,symbol,status,filled_amount,filled_cost FROM orders WHERE config_id=? AND trade_mode='SPOT_DCA' AND UPPER(side) IN ('BUY','BUY_LIMIT')", (config_id,)):
                if not affected(row['symbol']):
                    continue
                verified_empty = None
                if 'spot_order_fills' in tables:
                    verified_empty = conn.execute('''SELECT 1 FROM spot_order_fills
                        WHERE config_id=? AND symbol=? AND order_id=?
                        AND UPPER(status) IN ('CANCELLED','CANCELED','EXPIRED','REJECTED')
                        AND COALESCE(filled_qty,0)=0 AND COALESCE(filled_cost,0)=0''',
                        (config_id, row['symbol'], row['order_id'])).fetchone()
                if not verified_empty:
                    return True
        if 'spot_budget_reservations' in tables:
            for row in conn.execute("SELECT symbol,order_id FROM spot_budget_reservations WHERE config_id=? AND status<>'released'", (config_id,)):
                if not affected(row['symbol']):
                    continue
                verified_empty = None
                if 'spot_order_fills' in tables:
                    verified_empty = conn.execute('''SELECT 1 FROM spot_order_fills
                        WHERE config_id=? AND symbol=? AND order_id=?
                        AND UPPER(status) IN ('CANCELLED','CANCELED','EXPIRED','REJECTED')
                        AND COALESCE(filled_qty,0)=0 AND COALESCE(filled_cost,0)=0''',
                        (config_id, row['symbol'], row['order_id'])).fetchone()
                if not verified_empty:
                    return True
    return False


def assert_profile_transition(conn, previous, updated):
    if not previous:
        return
    previous_all = previous.get('symbol_scope', 'selected') == 'all'
    updated_all = updated.get('symbol_scope', 'selected') == 'all'
    before_symbols = {value.split(':')[0] for value in previous.get('symbols') or []}
    after_symbols = {value.split(':')[0] for value in updated.get('symbols') or []}
    removed = before_symbols - after_symbols if not updated_all else set()
    changed_exit = previous.get('exit_mode') != updated.get('exit_mode')
    if previous_all and not updated_all and active_lifecycle(conn, previous, excluded_symbols=updated.get('symbols') or []):
        raise ValueError('MCP profile still has positions, orders or unresolved operations outside the selected symbols')
    if (removed or changed_exit) and active_lifecycle(conn, previous, None if changed_exit else removed):
        raise ValueError('MCP profile still has positions, orders or unresolved operations; cannot change exit mode or remove affected symbols')


def account_change_hook():
    from backend.config_store import load_effective_runtime_snapshot, load_runtime_snapshot
    previous = {row['profile_id']: row for row in load_effective_runtime_snapshot().get('exchange_profiles', [])}

    def validate(conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_records'").fetchone():
            return
        updated = {row['profile_id']: row for row in (load_runtime_snapshot(connection=conn) or {}).get('exchange_profiles', [])}
        for row in conn.execute("SELECT payload FROM mcp_records WHERE kind='profile'"):
            profile = json.loads(row[0])
            account_id = profile['exchange_profile_id']
            if account_id not in updated:
                raise ValueError(f'Exchange account is referenced by MCP profile {profile["name"]}; remove the unused MCP profile first')
            before, after = previous.get(account_id, {}), updated[account_id]
            if after.get('market_type', 'swap') != profile['market_type']:
                raise ValueError('Exchange account market type must match its MCP profile')
            changed = any((before.get(key) or '') != (after.get(key) or '') for key in ('exchange', 'market_type', 'api_key', 'secret', 'passphrase'))
            if changed and active_lifecycle(conn, profile):
                raise ValueError('MCP account has active exposure or unresolved operations; credentials cannot change until its lifecycle is complete')
    return validate
