import json
import sqlite3
from pathlib import Path
import re

import pytest

from backend.utils.trade_receipts import TradeReceipt, project_receipt
from backend.utils.trade_operations import initialize_trade_operations_schema, tool_result_status
from backend.utils.trade_evidence import append_receipt, save_plan


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


def test_evidence_migration_and_original_receipt_are_idempotent():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    initialize_trade_operations_schema(conn.cursor())
    conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
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


def test_real_old_database_upgrade_preserves_raw_first_receipt_and_hash(tmp_path):
    conn = sqlite3.connect(tmp_path / 'old.sqlite')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at)')
    original = json.dumps({'status': 'unknown', 'error': 'historical timeout'})
    conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)', ('cfg', 'old-op', 'ETH/USDT', 'old-hash', 'unknown', original, 1, 2))
    conn.execute('CREATE TABLE real_protection_events(id INTEGER PRIMARY KEY,config_id,symbol,payload)')
    plan = dict(account_scope='old-account', episode_id='old-cycle', execution_mode='independent_exits', symbol='ETH/USDT',
                entries=[dict(client_id='old-client', operation_id='old-op', id='1', filled=1, status='closed')])
    conn.execute('INSERT INTO real_protection_events VALUES(?,?,?,?)', (1, 'cfg', 'ETH/USDT', json.dumps(plan)))
    for _ in range(2):
        initialize_trade_operations_schema(conn.cursor())
    assert conn.execute('SELECT payload FROM trade_receipt_revisions WHERE revision=0').fetchone()[0] == original
    assert conn.execute('SELECT request_hash FROM trade_action_runs').fetchone()[0] == 'old-hash'
    assert conn.execute('SELECT count(*) FROM trade_cycle_evidence').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM trade_operation_evidence').fetchone()[0] == 1
    conn.commit()
    # Rollback drill: stop writers, take a full backup, inspect through the old
    # tables without deleting new evidence, then upgrade the restored copy.
    backup = sqlite3.connect(tmp_path / 'rollback.sqlite')
    backup.row_factory = sqlite3.Row
    conn.backup(backup)
    conn.close()
    evidence_before = list(backup.execute('SELECT * FROM trade_operation_evidence'))
    assert backup.execute('SELECT status,result FROM trade_action_runs').fetchone()['result'] == original
    initialize_trade_operations_schema(backup.cursor())
    assert list(backup.execute('SELECT * FROM trade_operation_evidence')) == evidence_before
    assert backup.execute('SELECT request_hash FROM trade_action_runs').fetchone()[0] == 'old-hash'
    assert backup.execute('SELECT payload FROM trade_receipt_revisions').fetchone()[0] == original
    backup.close()


def test_documented_synthetic_receipt_matches_schema():
    document = Path('docs/TRADE_RECEIPTS_AND_RECOVERY.md').read_text(encoding='utf-8')
    examples = re.findall(r'```json\n(.*?)\n```', document, re.S)
    assert examples
    for sample in examples:
        TradeReceipt.model_validate(json.loads(sample))
        assert 'sk-' not in sample
