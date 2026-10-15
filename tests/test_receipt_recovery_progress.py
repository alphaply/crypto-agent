"""Background aggregation must not consume each child's exchange budget twice."""
import json
from unittest.mock import Mock

import pytest

from backend import database
from backend.agent import unified_trading as unified
from backend.config import config
from backend.utils.background_receipts import reconcile
from backend.utils.trade_operations import run_once
from test_unified_trading import ledger, batch, cancel, execute


def test_background_parent_aggregates_without_refreshing_children_twice(ledger, monkeypatch):
    execute(batch(cancel(), cancel('regular:2')),
            lambda *args: {'receipt_version': 1, 'status': 'submitted'})
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {})
    refreshed = []

    def refresh(config_id, symbol, operation_id, previous, read_exchange, **kwargs):
        assert read_exchange
        refreshed.append(operation_id)
        return {**previous, 'status': 'completed', 'query': {'status': 'completed'}}

    monkeypatch.setattr(unified, '_refresh_child', refresh)
    reconcile(limit=32)
    assert refreshed == ['parent:0', 'parent:1']
    # Next pass updates the aggregate from durable children without more reads.
    with database.get_db_conn() as conn:
        conn.execute('UPDATE background_receipt_retries SET next_attempt=0')
        conn.commit()
    reconcile(limit=32)
    assert refreshed == ['parent:0', 'parent:1']
    with database.get_db_conn() as conn:
        parent = json.loads(conn.execute("SELECT result FROM trade_action_runs WHERE operation_id='parent'").fetchone()[0])
    assert parent['status'] == 'completed'


@pytest.mark.parametrize('query_status', ['partial', 'failed'])
def test_completed_execution_with_incomplete_query_is_retried_and_backed_off(ledger, monkeypatch, query_status):
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {})
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'REAL'})
    receipt = {'status': 'completed', 'filled_amount': .2, 'query': {'status': query_status, 'errors': ['read timeout']}}
    run_once('cfg', 'ETH/USDT', 'original', {'contract_version': 3, 'tool': 'open'}, lambda: receipt)
    observe = Mock(return_value=receipt)
    monkeypatch.setattr('backend.agent.unified_dispatch.refresh_native_operation', observe)
    reconcile(limit=32)
    reconcile(limit=32)
    observe.assert_called_once()
    with database.get_db_conn() as conn:
        retry = conn.execute('SELECT attempts,result FROM background_receipt_retries').fetchone()
        saved = json.loads(conn.execute('SELECT result FROM trade_action_runs').fetchone()[0])
    assert retry['attempts'] == 1 and retry['result'] == 'read_failed'
    assert saved['status'] == 'completed' and saved['filled_amount'] == .2


def test_partial_child_query_is_not_reported_as_complete_batch_query(ledger, monkeypatch):
    execute(batch(cancel(), cancel('regular:2')), lambda *args: {'status': 'submitted'})
    with database.get_db_conn() as conn:
        conn.execute('UPDATE trade_action_runs SET result=%s WHERE operation_id=%s',
                     (json.dumps({'status': 'submitted', 'query': {'status': 'partial', 'errors': ['read timeout']}}), 'parent:1'))
        conn.commit()
    monkeypatch.setattr(unified, '_refresh_child', Mock(side_effect=AssertionError('Aggregate must not refresh')))
    result = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False, refresh_children=False)
    assert result['query']['status'] == 'partial'
    assert result['query']['errors'] == [{'index': 1, 'errors': ['read timeout']}]
