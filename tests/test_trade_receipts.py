import json
from backend import storage
from pathlib import Path
import re

import pytest

from backend.utils.trade_receipts import TradeReceipt, project_receipt
from backend.utils.trade_operations import initialize_trade_operations_schema, tool_result_status
from backend.utils.trade_evidence import append_receipt, save_plan
from backend.utils.trade_order_evidence import classify_order, classify_saved_order
from backend.utils.trade_receipts import unresolved_orders


@pytest.mark.parametrize('record,expected', [
    ({'status': 'rejected', 'submission_state': 'not_submitted',
      'submission_evidence': {'source': 'exchange_rejection', 'no_execution': True}}, 'not_accepted'),
    ({'status': 'rejected', 'submission_state': 'not_submitted'}, 'submission_unknown'),
    ({'status': 'submitting', 'submission_state': 'unknown'}, 'submission_unknown'),
    ({'id': '1', 'status': 'canceled', 'fill_pending': True}, 'terminal_fill_unverified'),
    ({'id': '1', 'status': 'open', 'quantity_mode': 'all', 'waiting_trigger': True}, 'live_verified'),
    ({'id': '1', 'status': 'open', 'fill_verified': True, 'amendment': {'state': 'rejected'}}, 'live_verified'),
    ({'id': '1', 'status': 'open'}, 'submission_unknown'),
    ({'id': '1', 'status': 'open', 'fill_verified': True, 'cancel_requested_at': 100}, 'submission_unknown'),
    ({'id': '1', 'status': 'rejected', 'execution_order_id': '2'}, 'terminal_fill_unverified'),
    ({'id': '1', 'status': 'closed', 'fill_verified': True}, 'terminal_verified'),
])
def test_execution_classification_drives_unresolved_view(record, expected):
    record = {**record, 'operation_id': 'op', 'client_id': 'client', 'execution_error': 'irrelevant failed unknown'}
    assert classify_order(record)['state'] == expected
    assert bool(unresolved_orders({'entries': [record]})) == (expected in {'submission_unknown', 'terminal_fill_unverified'})


def test_legacy_rejection_requires_evidence_and_keeps_original_fields():
    record = dict(status='rejected', submission_state='not_submitted', execution_error='binance {"code":-2021,"msg":"reject"}')
    original = dict(record)
    assert classify_saved_order(record)['state'] == 'not_accepted'
    assert all(record[k] == v for k, v in original.items())
    assert record['submission_evidence']['migrated'] is True


@pytest.mark.parametrize('status', ['failed', 'pending', 'unknown', 'submitted', 'completed', 'skipped', 'not_executed'])
def test_versioned_status_is_not_reinterpreted_from_cleanup(status):
    value = project_receipt(None, None, operation_id='example', action='close', submission_state='not_submitted')
    value.update(status=status, cleanup={'status': 'pending', 'error': 'old order unknown'})
    assert TradeReceipt.model_validate(value).status == status
    assert tool_result_status(value) == status


def test_unknown_fill_is_null_but_last_verified_fill_survives():
    record = dict(id='example', client_id='example-client', status='canceled', filled=.2,
                  amount=1, fill_verified=True, fill_pending=True, fill_observed_at=123)
    result = project_receipt({'contract_size': 2}, record, operation_id='cancel-1', action='cancel')
    assert result['status'] == 'pending'
    assert result['filled_amount'] is result['remaining_amount'] is None
    assert result['last_known_filled_amount'] == .4
    assert result['last_known_fill_at'] == 123


def test_native_evidence_and_original_receipt_are_idempotent(financial_pg):
    conn = storage.connect()
    initialize_trade_operations_schema(conn.cursor())
    conn.execute('INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                 ('cfg', 'op', 'ETH/USDT', 'unchanged-hash', 'unknown', '{}', 1, 1))
    append_receipt(conn, 'cfg', 'op', {'status': 'unknown'})
    append_receipt(conn, 'cfg', 'op', {'status': 'completed'})
    append_receipt(conn, 'cfg', 'op', {'status': 'completed'})
    initialize_trade_operations_schema(conn.cursor())
    assert conn.execute('SELECT request_hash FROM trade_action_runs').fetchone()[0] == 'unchanged-hash'
    revisions = conn.execute('SELECT payload FROM trade_receipt_revisions ORDER BY revision').fetchall()
    assert [json.loads(row[0])['status'] for row in revisions] == ['unknown', 'completed']
    for episode in ['old', 'new']:
        save_plan(conn, 'cfg', dict(account_scope='test-account', symbol='ETH/USDT', episode_id=episode,
                                   record_version=1, entries=[dict(client_id=episode, operation_id=episode, filled=1)]))
    assert conn.execute('SELECT count(*) FROM trade_cycle_evidence').fetchone()[0] == 2
    with pytest.raises(RuntimeError, match='changed'):
        save_plan(conn, 'cfg', dict(account_scope='test-account', symbol='ETH/USDT', episode_id='old', record_version=0))
    with pytest.raises(RuntimeError, match='backwards'):
        save_plan(conn, 'cfg', dict(account_scope='test-account', symbol='ETH/USDT', episode_id='old', record_version=2,
                                   entries=[dict(client_id='old', operation_id='old', filled=0)]))
    conn.close()


def test_native_restart_preserves_raw_first_receipt_and_hash(financial_pg):
    original = json.dumps({'status': 'unknown', 'error': 'historical timeout'})
    plan = dict(account_scope='old-account', episode_id='old-cycle', execution_mode='independent_exits', symbol='ETH/USDT',
                record_version=1, entries=[dict(client_id='old-client', operation_id='old-op', id='1', filled=1, status='closed')])
    with storage.connect() as conn:
        conn.execute('INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', ('cfg', 'old-op', 'ETH/USDT', 'old-hash', 'unknown', original, 1, 2))
        append_receipt(conn, 'cfg', 'old-op', json.loads(original))
        save_plan(conn, 'cfg', plan)
    with storage.connect() as reopened:
        before = [tuple(row) for row in reopened.execute('SELECT * FROM trade_operation_evidence')]
        for _ in range(2):
            initialize_trade_operations_schema(reopened.cursor())
        assert [tuple(row) for row in reopened.execute('SELECT * FROM trade_operation_evidence')] == before
        assert tuple(reopened.execute('SELECT request_hash,result FROM trade_action_runs').fetchone()) == ('old-hash', original)
        assert json.loads(reopened.execute('SELECT payload FROM trade_receipt_revisions').fetchone()[0]) == json.loads(original)
        assert reopened.execute('SELECT count(*) FROM trade_cycle_evidence').fetchone()[0] == 1
        reopened.execute('DELETE FROM trade_operation_evidence')
        reopened.rollback()
        assert [tuple(row) for row in reopened.execute('SELECT * FROM trade_operation_evidence')] == before


def test_documented_synthetic_receipt_matches_schema():
    document = Path('docs/TRADE_RECEIPTS_AND_RECOVERY.md').read_text(encoding='utf-8')
    examples = re.findall(r'```json\n(.*?)\n```', document, re.S)
    assert examples
    for sample in examples:
        payload = json.loads(sample)
        if 'actions' in payload:
            from backend.agent.trading_contracts import CanonicalTradeActionsSchema
            CanonicalTradeActionsSchema.model_validate(payload)
        else:
            TradeReceipt.model_validate(payload)
        assert 'sk-' not in sample
def test_historical_diagnostic_does_not_create_a_cleanup_dependency():
    from backend.utils.trade_receipts import project_receipt
    record = dict(id='finished', operation_id='original', status='closed', filled=.3,
                  amount=.3, fill_verified=True, submission_state='submitted')
    plan = dict(side='LONG', state='DONE', error='historical rejection of another order',
                position_observation={'status': 'verified', 'amount': 0}, entries=[], exits=[record])
    receipt = project_receipt(plan, record, operation_id='original', action='close')
    assert receipt['status'] == 'completed' and receipt['filled_amount'] == .3
    assert receipt['cleanup']['status'] == 'completed'
