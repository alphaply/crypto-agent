"""Physical resource coordination; actor identity never implies exclusivity."""
import json
from contextlib import contextmanager

from backend import database
from backend.storage import lock_scope


@contextmanager
def resource_lock(mt, symbol):
    """Maintenance and foreground mutations use the same process boundary."""
    cfg = getattr(mt, 'runtime_config', {}) or {}
    from .visible_management import physical_account_scope, _active_management
    if cfg.get('mode', '').upper() != 'REAL' or _active_management.get():
        yield
        return
    from .management_lock import scope_file_lock
    with scope_file_lock(physical_account_scope(mt), 'swap', symbol):
        yield


def execution_scope(mt, fallback):
    cfg = getattr(mt, 'runtime_config', {}) or {}
    if cfg.get('mode', '').upper() not in {'REAL', 'SPOT_DCA'} or cfg.get('read_only'):
        return fallback
    from .visible_management import physical_account_scope
    physical = physical_account_scope(mt, persist=True)
    market = getattr(mt, 'market_type', cfg.get('market_type', 'swap'))
    with database.get_db_conn() as conn:
        lock_scope(conn, 'shared-account', physical, market)
        conn.execute('INSERT INTO management_shared_accounts(physical_account,market_type,legacy_scope) '
                     'VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (physical, market, fallback))
        scope = conn.execute('SELECT legacy_scope FROM management_shared_accounts WHERE physical_account=%s AND market_type=%s',
                             (physical, market)).fetchone()[0]
        conn.commit()
    return scope


def shared_plan(conn, account_scope, symbol, side):
    """One current resource plan. The stored config_id remains its source."""
    rows = conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol=%s AND side=%s', (symbol, side)).fetchall()
    plans = [{**json.loads(row[1]), '_storage_config_id': row[0]} for row in rows]
    matching = [plan for plan in plans if plan.get('account_scope') == account_scope]
    active = [p for p in matching if p.get('state') != 'DONE']
    if len(active) > 1:
        # Migration cannot choose away financial evidence or duplicate workers.
        raise ValueError('Multiple active execution plans need reconciliation before a shared write')
    return active[0] if active else max(matching, key=lambda p: p.get('updated_at', 0), default=None)
