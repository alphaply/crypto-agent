"""Diagnose missing ownership; restore only a completely evidenced original cycle."""
from backend.storage import lock_scope
import json
import time

from backend.database import get_db_conn, _current_timestamp
from backend.utils.trade_receipts import unresolved_orders
from backend.utils.trade_operations import _same_symbol
from backend.utils.trade_order_evidence import classify_saved_order


def diagnose_cycle(service, symbol, side, current=None):
    diagnosis = {'cycle_state': (current or {}).get('state', 'MISSING'), 'reason_code': 'no_history',
                 'restorable': False, 'symbol': symbol, 'side': side}
    with get_db_conn() as conn:
        all_current = conn.execute('SELECT config_id,payload FROM real_protection_plans').fetchall()
        history = conn.execute('SELECT payload FROM trade_cycle_evidence WHERE config_id=%s', (service.config_id,)).fetchall()
        links = conn.execute('SELECT order_id,config_id FROM execution_order_links WHERE account_scope=%s AND symbol=%s',
                             (service.account_scope, symbol)).fetchall()
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()").fetchall()}
        active_agents = {r[0] for r in conn.execute("SELECT config_id FROM agent_configs WHERE enabled=1").fetchall()} if 'agent_configs' in tables else set()
        mcp_profiles = {'mcp:' + r[0] for r in conn.execute("SELECT id FROM mcp_records WHERE kind='profile'").fetchall()} if 'mcp_records' in tables else set()
    has_active_registry = bool(active_agents or mcp_profiles)
    for row in all_current:
        plan = json.loads(row['payload'])
        if (not _same_symbol(plan.get('symbol'), symbol) or plan.get('account_scope') != service.account_scope
                or row['config_id'] == service.config_id or plan.get('state') in ('DONE', 'WAITING')
                or (plan.get('side') and plan.get('side') != side)):
            continue
        if has_active_registry and row['config_id'] not in (active_agents | mcp_profiles):
            continue
        diagnosis['reason_code'] = 'competing_ownership'
        diagnosis['competing_config_id'] = row['config_id']
        diagnosis['competing_side'] = plan.get('side')
        return diagnosis, None
    candidates = [json.loads(row['payload']) for row in history]
    candidates = [p for p in candidates if _same_symbol(p.get('symbol'), symbol) and p.get('side') == side]
    matching = [p for p in candidates if p.get('account_scope') == service.account_scope]
    if not matching:
        diagnosis['reason_code'] = 'account_scope_mismatch' if candidates else 'no_history'
        return diagnosis, None
    candidate = max(matching, key=lambda p: (p.get('created_at', 0), p.get('updated_at', 0)))
    diagnosis['episode_id'] = candidate['episode_id']
    if current and current.get('episode_id') != candidate['episode_id']:
        diagnosis['reason_code'] = 'successor_cycle_exists'
        return diagnosis, None
    from backend.utils.trade_order_evidence import recover_legacy_rejections
    recover_legacy_rejections(service.config_id, candidate)
    records = candidate.get('entries', []) + candidate.get('exits', [])
    records = [r for r in records if classify_saved_order(r)['state'] != 'not_accepted']
    if candidate.get('history_complete') is False or not records or any(not r.get('id') for r in records):
        diagnosis['reason_code'] = 'incomplete_history'
        return diagnosis, None
    owners = {r['order_id']: r['config_id'] for r in links}
    from backend.utils.exit_orders import is_conditional, order_reference
    for record in records:
        key = order_reference(record, current=False) if record.get('exit_type') and is_conditional(record) else str(record['id'])
        if owners.get(key) != service.config_id:
            diagnosis['reason_code'] = 'incomplete_ownership_evidence'
            return diagnosis, None
    # Terminal orders are queried too: restoration requires current, complete
    # cumulative order evidence, not only an old local flat observation.
    try:
        for record in records:
            if service._query(record, symbol) is None:
                raise RuntimeError('Historical order cannot be verified')
            record.pop('query_error', None)
            record.pop('next_retry_at', None)
        position = service._position(candidate)
    except Exception as exc:
        diagnosis.update(reason_code='history_read_unverified', error=str(exc))
        return diagnosis, None
    if unresolved_orders(candidate) or any(r.get('status') not in {'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
                                            for r in candidate.get('entries', [])):
        diagnosis['reason_code'] = 'pending_order_race'
        return diagnosis, None
    incoming = sum(float(r.get('filled') or 0) for r in candidate.get('entries', []))
    incoming += float((candidate.get('adoption') or {}).get('contracts') or 0)
    outgoing = sum(float(r.get('filled') or 0) for r in candidate.get('exits', []))
    quantity = float((position or {}).get('contracts') or 0)
    if quantity <= 0 or abs(incoming - outgoing - quantity) > max(1e-10, quantity * 1e-8):
        diagnosis.update(reason_code='quantity_mismatch', evidenced_contracts=incoming-outgoing,
                         observed_contracts=quantity)
        return diagnosis, None
    diagnosis.update(reason_code='same_task_history_verified', restorable=True,
                     observed_contracts=quantity)
    return diagnosis, candidate


def restore_cycle(service, symbol, side, current=None):
    from backend.utils.trade_evidence import scope_generation, reserve_scope
    with get_db_conn() as conn:
        generation = scope_generation(conn, service.account_scope, symbol)
    diagnosis, candidate = diagnose_cycle(service, symbol, side, current)
    if not candidate:
        return None, diagnosis
    from backend.utils.trade_evidence import save_plan
    expected = candidate.get('record_version', 0)
    candidate['scope_generation'] = generation
    candidate.update(state='ACTIVE', error=None, ever_filled=True, record_version=expected + 1,
                     restored_at=time.time(), restoration={'reason': diagnosis['reason_code'], 'original_episode_id': candidate['episode_id']})
    candidate.pop('flat_candidate', None)
    candidate.pop('unobserved_entry_fills', None)
    candidate.pop('cleanup_fill_unseen', None)
    candidate.pop('read_errors', None)
    with get_db_conn() as conn:
        lock_scope(conn, 'protection-account', service.account_scope, symbol)
        live = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=%s AND symbol=%s AND side=%s',
                            (service.config_id, symbol, side)).fetchone()
        live_plan = json.loads(live['payload']) if live else None
        if (bool(live_plan) != bool(current) or (live_plan and (
                live_plan.get('episode_id') != current.get('episode_id')
                or live_plan.get('record_version', 0) != current.get('record_version', 0)
                or live_plan.get('account_scope') != service.account_scope))):
            raise RuntimeError('Position cycle changed before restoration')
        for row in conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol=%s', (symbol,)):
            other = json.loads(row['payload'])
            if (other.get('account_scope') == service.account_scope and row['config_id'] != service.config_id
                    and other.get('state') not in ('DONE', 'WAITING') and other.get('side') == side):
                tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()").fetchall()}
                active_agents = {r[0] for r in conn.execute("SELECT config_id FROM agent_configs WHERE enabled=1").fetchall()} if 'agent_configs' in tables else set()
                mcp_profiles = {'mcp:' + r[0] for r in conn.execute("SELECT id FROM mcp_records WHERE kind='profile'").fetchall()} if 'mcp_records' in tables else set()
                if not (active_agents or mcp_profiles) or row['config_id'] in (active_agents | mcp_profiles):
                    raise RuntimeError('Position ownership changed before restoration')
        reserve_scope(conn, candidate)
        save_plan(conn, service.config_id, candidate, expected_version=expected)
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(config_id,symbol,side) DO UPDATE SET payload=excluded.payload', (service.config_id, symbol, side, json.dumps(candidate)))
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                     (_current_timestamp(), service.config_id, symbol, json.dumps(candidate)))
        conn.commit()
    return candidate, diagnosis
