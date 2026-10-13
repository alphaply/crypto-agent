"""Additive, versioned evidence independent of the current position cycle.

Network reads happen before these short SQLite transactions. Original requests
and receipts remain immutable; later projections are append-only revisions.
"""
from __future__ import annotations

import json
import time
import hashlib


def initialize_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_receipt_watermarks (
        config_id TEXT NOT NULL, account_scope TEXT NOT NULL, operation_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, PRIMARY KEY(config_id,account_scope,operation_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_scope_generations (
        account_scope TEXT NOT NULL, symbol TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(account_scope,symbol))''')
    fresh = not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_cycle_evidence'").fetchone()
    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_cycle_evidence (
        config_id TEXT NOT NULL, account_scope TEXT NOT NULL, symbol TEXT NOT NULL,
        episode_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(config_id,account_scope,symbol,episode_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_operation_evidence (
        config_id TEXT NOT NULL, account_scope TEXT NOT NULL, symbol TEXT NOT NULL,
        operation_id TEXT NOT NULL, episode_id TEXT NOT NULL, order_key TEXT NOT NULL,
        version INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(config_id,account_scope,symbol,operation_id,episode_id,order_key))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_receipt_revisions (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, revision INTEGER NOT NULL,
        observed_at REAL NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(config_id,operation_id,revision))''')
    if fresh:
        tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trade_action_runs' in tables:
            cursor.execute('INSERT OR IGNORE INTO trade_receipt_revisions SELECT config_id,operation_id,0,updated_at,result '
                           'FROM trade_action_runs WHERE result IS NOT NULL')
        # Events retain cycles that have already been replaced in the current row.
        for table, ordering in [('real_protection_events', ' ORDER BY id'), ('real_protection_plans', '')]:
            if table not in tables:
                continue
            for row in cursor.execute(f'SELECT config_id,payload FROM {table}{ordering}').fetchall():
                row = row if isinstance(row, dict) or hasattr(row, 'keys') else dict(zip(('config_id', 'payload'), row))
                try:
                    plan = json.loads(row['payload'])
                    if plan.get('execution_mode') == 'independent_exits' and plan.get('account_scope') and plan.get('episode_id'):
                        save_plan(cursor.connection, row['config_id'], plan)
                except (ValueError, TypeError, KeyError):
                    continue  # Incomplete historical evidence cannot establish ownership.


def scope_generation(conn, account_scope: str, symbol: str) -> int:
    row = conn.execute('SELECT generation FROM trade_scope_generations WHERE account_scope=? AND symbol=?',
                       (account_scope, symbol)).fetchone()
    return int(row[0]) if row else 0


def reserve_scope(conn, plan: dict):
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    generation = scope_generation(conn, plan['account_scope'], plan['symbol'])
    if plan.get('scope_generation', 0) != generation:
        raise ConcurrentProtectionUpdate('Account/symbol evidence changed; refresh before submission')
    conn.execute('INSERT INTO trade_scope_generations VALUES(?,?,?) ON CONFLICT(account_scope,symbol) '
                 'DO UPDATE SET generation=excluded.generation',
                 (plan['account_scope'], plan['symbol'], generation + 1))
    plan['scope_generation'] = generation + 1


def save_plan(conn, config_id: str, plan: dict, *, expected_version: int | None = None):
    """Caller owns the transaction; reject stale or regressing observations."""
    scope = (config_id, plan['account_scope'], plan['symbol'], plan['episode_id'])
    stored = conn.execute('SELECT version,payload FROM trade_cycle_evidence WHERE '
                          'config_id=? AND account_scope=? AND symbol=? AND episode_id=?', scope).fetchone()
    if stored is not None and not hasattr(stored, 'keys'):
        stored = dict(zip(('version', 'payload'), stored))
    version = int(plan.get('record_version', 0))
    if stored:
        from backend.utils.position_protection import ConcurrentProtectionUpdate
        if (expected_version is not None and stored['version'] != expected_version) or version < stored['version']:
            raise ConcurrentProtectionUpdate('Operation evidence changed; reload before updating')
        previous = json.loads(stored['payload'])
        records = {r.get('client_id') or r.get('id'): r for r in previous.get('entries', []) + previous.get('exits', [])}
        for record in plan.get('entries', []) + plan.get('exits', []):
            old = records.get(record.get('client_id') or record.get('id'), {})
            if float(record.get('filled') or 0) + 1e-12 < float(old.get('filled') or 0):
                raise ConcurrentProtectionUpdate('Cumulative evidence cannot move backwards')
            terminal = {'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'}
            if old.get('status') in terminal and record.get('status') not in terminal:
                raise ConcurrentProtectionUpdate('Terminal execution evidence cannot move backwards')
    conn.execute('INSERT INTO trade_cycle_evidence VALUES(?,?,?,?,?,?) ON CONFLICT '
                 '(config_id,account_scope,symbol,episode_id) DO UPDATE SET version=excluded.version,payload=excluded.payload',
                 (*scope, version, json.dumps(plan)))
    for record in plan.get('entries', []) + plan.get('exits', []):
        ids = {record.get(key) for key in ('operation_id', 'cancel_operation_id', 'last_operation_id')}
        ids.update((record.get(key) or {}).get('operation_id') for key in ('amendment', 'replacement'))
        for operation_id in ids - {None, ''}:
            conn.execute('INSERT INTO trade_operation_evidence VALUES(?,?,?,?,?,?,?,?) ON CONFLICT '
                         '(config_id,account_scope,symbol,operation_id,episode_id,order_key) '
                         'DO UPDATE SET version=excluded.version,payload=excluded.payload',
                         (config_id, plan['account_scope'], plan['symbol'], operation_id, plan['episode_id'],
                          record.get('client_id') or str(record.get('id')), version, json.dumps(record)))


def append_receipt(conn, config_id: str, operation_id: str, receipt):
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str)
    previous = conn.execute('SELECT revision,payload FROM trade_receipt_revisions WHERE config_id=? AND operation_id=? '
                            'ORDER BY revision DESC LIMIT 1', (config_id, operation_id)).fetchone()
    if previous and previous['payload'] == payload:
        return previous['revision']
    revision = previous['revision'] + 1 if previous else 0
    conn.execute('INSERT INTO trade_receipt_revisions VALUES(?,?,?,?,?)',
                 (config_id, operation_id, revision, time.time(), payload))
    return revision


def load_cycles(config_id: str, account_scope: str, symbol: str, operation_id: str) -> list[dict]:
    from backend.database import get_db_conn
    from backend.utils.trade_operations import _same_symbol, _operation_matches
    with get_db_conn() as conn:
        rows = conn.execute('SELECT DISTINCT c.symbol,c.payload,e.operation_id FROM trade_cycle_evidence c '
                            'JOIN trade_operation_evidence e USING(config_id,account_scope,symbol,episode_id) '
                            'WHERE c.config_id=? AND c.account_scope=?', (config_id, account_scope)).fetchall()
    plans = {}
    for row in rows:
        if _same_symbol(row['symbol'], symbol) and _operation_matches(row['operation_id'], operation_id):
            plan = json.loads(row['payload'])
            plans[plan['episode_id']] = plan
    return list(plans.values())


def configured_account_scope(config_id: str) -> str:
    """Resolve trusted credentials locally. Never construct a network client here."""
    from backend.config import config
    cfg = config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError('Task configuration unavailable')
    if str(cfg.get('mode', 'STRATEGY')).upper() != 'REAL':
        return 'mock:' + config_id
    exchange, key, _, _ = config.get_exchange_credentials(config_id=config_id)
    if not key:
        raise ValueError('Account credentials unavailable')
    exchange_id = 'binanceusdm' if exchange == 'binance' else exchange
    return hashlib.sha256(f'{exchange_id}:{key}'.encode()).hexdigest()


_VOLATILE_EVIDENCE = {'updated_at', 'verified_at', 'observed_at', 'fill_observed_at',
                      'last_attempt_at', 'next_retry_at', 'attempt_count', 'record_version',
                      'scope_generation', 'assessed_at', 'projected_at', 'queried_at'}


def evidence_fingerprint(plans: list[dict], operation_id: str) -> str:
    """Fingerprint original execution and its cleanup dependencies, not later cycles."""
    from backend.utils.trade_recovery import _records
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in _VOLATILE_EVIDENCE}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    values = []
    for plan in sorted(plans, key=lambda p: str(p.get('episode_id'))):
        values.append({
            'episode_id': plan.get('episode_id'), 'state': plan.get('state'),
            'records': [{'operation_id': op, 'record': record} for op, record in _records(plan, operation_id)],
            'dependencies': plan.get('entries', []) + plan.get('exits', []),
            **{k: plan.get(k) for k in ('position_observation', 'position_error',
                                        'unobserved_entry_fills', 'cleanup_fill_unseen', 'residual_verified')},
        })
    return hashlib.sha256(json.dumps(stable(values), sort_keys=True, default=str).encode()).hexdigest()
