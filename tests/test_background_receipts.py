from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from backend import database
from backend.utils.background_receipts import reconcile
from backend.utils.trade_operations import run_once
from backend.config import config
from test_unified_trading import ledger


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY', 'SPOT_DCA'])
def test_original_v3_background_updates_without_model_or_financial_callback(ledger, monkeypatch, mode):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'config_id': 'cfg', 'mode': mode, 'symbol': 'ETH/USDT'})
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {'processed': 0})
    original = {'receipt_version': 1, 'contract_version': 3, 'operation_id': 'original-parent',
        'status': 'unknown', 'results': [
            {'index': 0, 'operation_id': 'child-0', 'status': 'unknown'},
            {'index': 1, 'operation_id': 'child-1', 'status': 'not_executed'}]}
    callback = Mock(return_value=deepcopy(original))
    request = {'contract_version': 3, 'tool': 'execute_trade_actions'}
    run_once('cfg', 'ETH/USDT', 'original-parent', request, callback)
    latest = deepcopy(original)
    latest['status'] = 'submitted'
    latest['results'][0].update(status='completed', order_id='regular:original', filled_amount=.03)
    readonly = Mock(return_value=latest)
    monkeypatch.setattr('backend.agent.unified_trading.query_unified_operation', readonly)
    outcome = reconcile(limit=1)
    readonly.assert_called_once_with('cfg', 'ETH/USDT', 'original-parent', read_exchange=True)
    assert outcome['processed'] == 1
    with database.get_db_conn() as conn:
        stored = conn.execute('SELECT result,request_hash FROM trade_action_runs WHERE operation_id=?', ('original-parent',)).fetchone()
        versions = conn.execute('SELECT payload FROM trade_receipt_revisions WHERE operation_id=? ORDER BY revision', ('original-parent',)).fetchall()
    assert json.loads(stored['result']) == latest
    assert json.loads(versions[0]['payload']) == original
    assert latest['results'][1]['status'] == 'not_executed'
    callback.assert_called_once()


def test_background_failure_keeps_original_and_backs_off(ledger, monkeypatch):
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {'processed': 0})
    raw = {'status': 'unknown', 'operation_id': 'retry-original', 'client_id': 'cai-original'}
    run_once('cfg', 'ETH/USDT', 'retry-original', {'contract_version': 3}, lambda: raw)
    fetch = Mock(side_effect=TimeoutError('fake slow dependency'))
    monkeypatch.setattr('backend.agent.unified_trading.query_unified_operation', fetch)
    reconcile(limit=2)
    reconcile(limit=2)
    assert fetch.call_count == 1
    with database.get_db_conn() as conn:
        assert json.loads(conn.execute('SELECT result FROM trade_action_runs').fetchone()[0]) == raw
        row = conn.execute('SELECT attempts,next_attempt,result FROM background_receipt_retries').fetchone()
    assert row['attempts'] == 1 and row['result'] == 'read_failed'


def test_large_original_is_skipped_before_parse_without_changing_state(ledger, monkeypatch):
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {'processed': 0})
    run_once('cfg', 'ETH/USDT', 'large-original', {'contract_version': 3},
        lambda: {'status': 'unknown', 'operation_id': 'large-original', 'raw': 'x' * 10000})
    monkeypatch.setenv('RUNTIME_FINANCIAL_PAYLOAD_BYTES', '1024')
    fetch = Mock(side_effect=AssertionError('Oversized original must not be loaded'))
    monkeypatch.setattr('backend.agent.unified_trading.query_unified_operation', fetch)
    result = reconcile(limit=2)
    assert result['skipped'][0]['operation_id'] == 'large-original'
    fetch.assert_not_called()
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT status FROM trade_action_runs').fetchone()[0] == 'unknown'


def test_background_reserves_one_complete_record_before_read_and_keeps_batch_bounded(ledger, monkeypatch):
    from backend.utils import runtime_capacity as capacity
    manager = capacity.CapacityManager(total=3, reserved=1, byte_limit=4096)
    monkeypatch.setattr(capacity, '_manager', manager)
    monkeypatch.setattr('backend.utils.background_receipts.reconcile_mcp', lambda **_: {})
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode':'REAL'})
    for index in range(10):
        run_once('cfg','ETH/USDT',str(index),{'contract_version':3},lambda:{'status':'unknown'})
    fetch = Mock(side_effect=AssertionError('Complete payload commitments cannot fit together'))
    monkeypatch.setattr('backend.agent.unified_trading.query_unified_operation',fetch)
    result = reconcile(limit=10)
    assert len(result['skipped']) == 10
    fetch.assert_not_called()
    assert manager.snapshot()['bytes'] == 0 and manager.snapshot()['peaks']['bytes'] <= 4096


def test_mcp_background_keeps_principal_external_id_and_failure_backoff(ledger, monkeypatch):
    import time
    from backend.mcp import store, canonical_tools, settings
    from backend.utils.background_receipts import reconcile_mcp
    store.reserve_operation('profile','external','alice',{'tool':'close','symbol':'ETH/USDT'})
    store.complete_operation('profile','external',{'status':'submitted','order_id':'algo:original'})
    monkeypatch.setattr(settings,'get_runtime_profile',lambda _: {'mode':'REAL'})
    fetch=Mock(side_effect=TimeoutError('fixture'))
    monkeypatch.setattr(canonical_tools,'recover_unified',fetch)
    reconcile_mcp(limit=32,cap=1048576,deadline=time.monotonic()+5)
    reconcile_mcp(limit=32,cap=1048576,deadline=time.monotonic()+5)
    assert fetch.call_count == 1
    caller, profile, op = fetch.call_args.args
    assert caller['id']=='alice' and caller['scopes']==['read']
    assert (profile,op)==('profile','external')
