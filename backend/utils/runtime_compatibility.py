"""Read-only preflight for a proposed rollback to the previous trading contract."""
import json


def trading_compatibility_report(conn, *, supported_contract_version=3):
    if supported_contract_version not in {2, 3}:
        raise ValueError('Supported target versions are 2 and 3')
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    blockers = []

    def rows(table):
        if table not in tables:
            return []
        cursor = conn.execute(f'SELECT * FROM {table}')
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, tuple(row))) for row in cursor]

    def add(table, reason, count):
        if count:
            blockers.append({'table': table, 'reason': reason, 'count': count})

    if supported_contract_version < 3:
        add('visible_management_claims', 'persistent_management_owner',
            sum(row['state'] == 'owned' and not _payload(row).get('legacy_policy') for row in rows('visible_management_claims')))
        add('visible_management_actions', 'unresolved_management_action',
            sum(row['state'] in {'running', 'unknown', 'pending', 'unresolved'} for row in rows('visible_management_actions')))
        add('unified_trade_batches', 'active_v3_worker',
            sum(row['state'] != 'finished' for row in rows('unified_trade_batches')))
        requests = {(row['config_id'], row['operation_id']) for row in rows('trade_operation_requests')
                    if _json(row.get('request_json')).get('contract_version') == 3}
        add('trade_action_runs', 'unresolved_or_live_v3_operation',
            sum((row['config_id'], row['operation_id']) in requests and row['status'] in {'running', 'unknown', 'pending', 'submitted'}
                for row in rows('trade_action_runs')))
        add('unified_native_intents', 'native_v3_identity_needs_recovery',
            sum(_json(row.get('result')).get('status', 'unknown') in {'running', 'unknown', 'pending', 'submitted'}
                for row in rows('unified_native_intents')))
        add('paper_batches', 'active_or_unresolved_paper_batch',
            sum(_payload(row).get('status') in {'started', 'running', 'unknown', 'pending', 'submitted'}
                for row in rows('paper_batches')))
        add('management_recovery_grants', 'accepted_recovery_responsibility', len(rows('management_recovery_grants')))
        add('mcp_spot_exits', 'spot_exit_needs_current_management',
            sum(row.get('state') in {'submitting', 'cancelling', 'unknown', 'pending'} for row in rows('mcp_spot_exits')))
        operation_states = {(row['config_id'], row['operation_id']): row['status'] for row in rows('trade_action_runs')}
        add('spot_management_intents', 'native_spot_identity_needs_recovery',
            sum(operation_states.get((row['config_id'], row['operation_id']),
                (_payload(row).get('failed_result') or {}).get('status', 'unknown'))
                in {'running', 'unknown', 'pending', 'submitted'} for row in rows('spot_management_intents')))
    return {'status': 'blocked' if blockers else 'compatible',
            'supported_contract_version': supported_contract_version,
            'read_only': True, 'blockers': blockers,
            'preserve_tables': sorted(table for table in tables if table.startswith(('visible_management_', 'management_', 'unified_', 'paper_batch', 'spot_management_')) or table == 'spot_budget_reservations')}


def _json(value):
    if not value:
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {'status': 'unknown'}
    except (TypeError, ValueError):
        return {'status': 'unknown'}


def _payload(row):
    return _json(row.get('payload'))
