"""Repeated startup and a rejected old-runtime rollback preserve original evidence."""
import json
from unittest.mock import Mock

from backend import database
from backend.agent import unified_trading as unified
from backend.database_schema import initialize_schema
from backend.utils.runtime_compatibility import trading_compatibility_report
from test_unified_trading import ledger, execute, batch, cancel


def test_repeated_schema_initialization_preserves_unknown_parent_and_child(ledger):
    dispatcher = Mock(return_value={'status': 'unknown', 'receipt_version': 1, 'client_id': 'original-client'})
    first = execute(batch(cancel(), cancel('regular:tail')), dispatcher)
    child = first['results'][0]['operation_id']
    with database.get_db_conn() as conn:
        before = list(conn.execute('SELECT operation_id,request_hash FROM trade_action_runs ORDER BY operation_id'))
        initialize_schema(conn)
        initialize_schema(conn)
        assert [tuple(row) for row in conn.execute('SELECT operation_id,request_hash FROM trade_action_runs ORDER BY operation_id')] == [tuple(row) for row in before]
        blocked = trading_compatibility_report(conn, supported_contract_version=2)
        assert blocked['status'] == 'blocked' and blocked['read_only']
        assert any(row['reason'] == 'unresolved_or_live_v3_operation' for row in blocked['blockers'])
        assert trading_compatibility_report(conn, supported_contract_version=3)['status'] == 'compatible'
    recovered = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False)
    assert recovered['results'][0]['operation_id'] == child
    assert recovered['results'][1]['status'] == 'not_executed'
    assert dispatcher.call_count == 1


def test_rollback_report_is_read_only_and_keeps_management_owner(ledger):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO visible_management_claims VALUES(?,?,?,?,?,?,?,?,?)',
                     ('claim', 'physical:offline', 'swap', 'ETH/USDT', 'BOTH', 'cfg', 'owned', 1, json.dumps({'source': 'first_claim'})))
        conn.commit()
        before = conn.total_changes
        report = trading_compatibility_report(conn, supported_contract_version=2)
        assert report['status'] == 'blocked'
        assert conn.total_changes == before
        assert conn.execute('SELECT owner FROM visible_management_claims').fetchone()[0] == 'cfg'
        assert 'visible_management_claims' in report['preserve_tables']
