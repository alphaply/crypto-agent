import json
from unittest.mock import Mock

import pytest

from backend import database
from backend.agent import trade_batch
from backend.config import config
from backend.database_schema import initialize_schema
from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status, reconcile_pending_trade_operations


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'batch.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'REAL', 'exit_mode': 'independent_exits'})


def execute(actions, operation_id='batch-1'):
    token = current_operation_id.set(operation_id)
    try:
        return json.loads(trade_batch.execute_trade_actions.func(actions=actions, config_id='cfg', symbol='ETH/USDT'))
    finally:
        current_operation_id.reset(token)


def cancellations():
    return [{'action': 'cancel', 'order_id': f'order-{i}', 'reason': 'Invalidated'} for i in range(3)]


@pytest.mark.parametrize('response,expected', [({'status': 'failed', 'error': 'Rejected'}, 'failed'),
                                             ({'status': 'pending'}, 'unknown'),
                                             ('❌ Failed to cancel', 'failed')])
def test_batch_stops_on_failure_or_uncertainty_and_never_replays_writes(local_db, monkeypatch, response, expected):
    dispatch = Mock(side_effect=[{'status': 'canceled'}, response])
    monkeypatch.setattr(trade_batch, '_dispatch', dispatch)
    result = execute(cancellations())
    assert result['status'] == expected
    assert [item['status'] for item in result['results']] == ['completed', expected, 'not_executed']
    assert [item['index'] for item in result['results']] == [0, 1, 2]
    assert result['results'][2]['blocked_by_index'] == 1
    assert dispatch.call_count == 2
    assert execute(cancellations()) == result
    assert dispatch.call_count == 2


def test_batch_validates_entire_request_before_any_exchange_write(local_db, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(trade_batch, '_dispatch', dispatch)
    cases = [
        {'action': 'amend_exit', 'order_id': 'exit', 'price': 110, 'trigger_price': 90, 'reason': 'bad'},
        {'action': 'open', 'order': {'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'stop_loss': 90, 'reason': 'attached not allowed'}},
        {'action': 'update_protection', 'pos_side': 'LONG', 'stop_loss': 90, 'reason': 'invalid mode'},
    ]
    for invalid in cases:
        with pytest.raises(ValueError):
            execute([cancellations()[0], invalid])
    dispatch.assert_not_called()
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM trade_action_runs').fetchone()[0] == 0


def test_simulated_independent_entry_cannot_amend_attached_protection(local_db, monkeypatch):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'STRATEGY', 'exit_mode': 'independent_exits'})
    dispatch = Mock()
    monkeypatch.setattr(trade_batch, '_dispatch', dispatch)
    with pytest.raises(ValueError):
        execute([cancellations()[0], {'action': 'amend_entry', 'order_id': 'entry', 'pos_side': 'LONG', 'stop_loss': 90, 'reason': 'invalid mode'}])
    dispatch.assert_not_called()


def test_submitted_orders_do_not_report_filled_or_completed(local_db, monkeypatch):
    dispatch = Mock(return_value={'status': 'open', 'id': 'limit-order'})
    monkeypatch.setattr(trade_batch, '_dispatch', dispatch)
    result = execute([{'action': 'open', 'order': {'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'Plan'}}])
    assert result['status'] == 'submitted' and result['results'][0]['status'] == 'submitted'


def test_durable_operation_receipt_blocks_changed_request_and_uncertain_retry(local_db):
    callback = Mock(side_effect=TimeoutError('Lost acknowledgement'))
    first = run_once('cfg', 'ETH/USDT', 'id', {'amount': 1}, callback)
    assert first['status'] == 'unknown'
    assert run_once('cfg', 'ETH/USDT', 'id', {'amount': 1}, callback) == first
    assert callback.call_count == 1
    with pytest.raises(ValueError):
        run_once('cfg', 'ETH/USDT', 'id', {'amount': 2}, callback)
    with pytest.raises(ValueError):
        run_once('cfg', 'BTC/USDT', 'id', {'amount': 1}, callback)


def test_inflight_operation_cannot_be_executed_concurrently(local_db):
    competing = Mock(return_value={'status': 'open'})

    def callback():
        result = run_once('cfg', 'ETH/USDT', 'inflight', {'amount': 1}, competing)
        assert result['status'] == 'unknown'
        return {'status': 'open', 'id': 'confirmed'}

    result = run_once('cfg', 'ETH/USDT', 'inflight', {'amount': 1}, callback)
    assert result['id'] == 'confirmed'
    competing.assert_not_called()


@pytest.mark.parametrize('result,expected', [
    ('{"amendment_state":"confirmed"}；pending 不得重复提交', 'completed'),
    ({'amendment_state': 'pending', 'status': 'open'}, 'unknown'),
    ({'amendment_state': 'rejected'}, 'failed'),
    ({'amendment_state': 'unchanged'}, 'completed'),
    ({'status': 'completed', 'results': [{'status': 'failed'}]}, 'failed'),
    ({'status': 'completed', 'results': [{'status': 'open'}]}, 'submitted'),
    ({'results': [{'status': 'filled'}, {'status': 'pending'}]}, 'unknown'),
    ({'success': False}, 'failed'),
])
def test_truthful_status_mapping(result, expected):
    assert tool_result_status(result) == expected


def test_unknown_receipt_refreshes_from_native_reconciliation_without_replay(local_db):
    callback = Mock(side_effect=TimeoutError('Lost acknowledgement'))
    request = {'tool': 'open', 'amount': 1}
    assert run_once('cfg', 'ETH/USDT', 'call', request, callback)['status'] == 'unknown'
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'error': 'awaiting query', 'entries': [
        {'id': 'exchange-order', 'operation_id': 'call:0', 'status': 'open', 'amount': 1, 'filled': 0}
    ]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    assert run_once('cfg', 'ETH/USDT', 'call', request, callback)['status'] == 'unknown'
    plan['error'] = None
    with database.get_db_conn() as conn:
        conn.execute('UPDATE real_protection_plans SET payload=?', (json.dumps(plan),))
        conn.commit()
    result = run_once('cfg', 'ETH/USDT', 'call', request, callback)
    assert result['status'] == 'submitted' and result['reconciled'] is True
    assert result['results'][0]['result']['results'][0]['id'] == 'exchange-order'
    assert callback.call_count == 1
    assert '未执行动作没有重放' in result['message']


@pytest.mark.parametrize('pending_kind', ['fill_pending', 'unobserved_entry_fills', 'cleanup_fill_unseen', 'EXITING'])
def test_independent_receipt_waits_for_persisted_pending_evidence_even_without_error(local_db, pending_kind):
    callback = Mock(side_effect=TimeoutError('Lost acknowledgement'))
    request = {'tool': 'open', 'amount': 1}
    run_once('cfg', 'ETH/USDT', 'call', request, callback)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'error': None, 'execution_mode': 'independent_exits',
            'entries': [{'id': 'exchange-order', 'operation_id': 'call:0', 'status': 'closed',
                         'amount': 1, 'filled': 1}]}
    if pending_kind == 'fill_pending':
        plan['entries'][0]['fill_pending'] = True
    elif pending_kind == 'EXITING':
        plan['state'] = 'EXITING'
    else:
        plan[pending_kind] = 1
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    assert reconcile_pending_trade_operations('cfg') == 0
    assert run_once('cfg', 'ETH/USDT', 'call', request, callback)['status'] == 'unknown'
    plan['state'] = 'ACTIVE'
    plan.pop(pending_kind, None)
    plan['entries'][0].pop('fill_pending', None)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE real_protection_plans SET payload=?', (json.dumps(plan),))
        conn.commit()
    assert reconcile_pending_trade_operations('cfg') == 1
    assert run_once('cfg', 'ETH/USDT', 'call', request, callback)['status'] == 'completed'
    assert callback.call_count == 1


def test_reconciliation_requires_exact_prefix_scope_and_confirmed_amendment(local_db):
    callback = Mock(side_effect=TimeoutError('Lost acknowledgement'))
    run_once('cfg', 'ETH/USDT', 'amend', {}, callback)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'entries': [
        {'id': 'order', 'operation_id': 'old-entry', 'status': 'open',
         'amendment': {'state': 'pending', 'operation_id': 'amend'}}
    ]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    assert reconcile_pending_trade_operations('cfg') == 0
    plan['entries'][0]['amendment']['state'] = 'confirmed'
    with database.get_db_conn() as conn:
        conn.execute('UPDATE real_protection_plans SET payload=?', (json.dumps(plan),))
        conn.commit()
    assert reconcile_pending_trade_operations('other') == 0
    assert reconcile_pending_trade_operations('cfg') == 1
    assert run_once('cfg', 'ETH/USDT', 'amend', {}, callback)['status'] == 'completed'
    assert callback.call_count == 1
    run_once('cfg', 'ETH/USDT', 'amen', {}, callback)
    assert reconcile_pending_trade_operations('cfg') == 0  # no loose substring match


def test_committed_mock_operation_recovers_lost_outer_receipt(local_db):
    callback = Mock(side_effect=TimeoutError('Result persistence lost'))
    run_once('cfg', 'ETH/USDT', 'call', {}, callback)
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO mock_trade_operations VALUES(?,?,?,?)',
                     ('cfg', 'ETH/USDT', 'call:0:0', json.dumps({'id': 'mock-order', 'status': 'filled'})))
        conn.commit()
    result = run_once('cfg', 'ETH/USDT', 'call', {}, callback)
    assert result['status'] == 'completed' and result['reconciled']
    assert callback.call_count == 1


def _native_evidence(operation_id, order_id='native-order', status='closed'):
    return {'operation_id': operation_id, 'id': order_id, 'status': status, 'amount': 1, 'filled': 1}


def _save_native_evidence(records):
    with database.get_db_conn() as conn:
        conn.execute('INSERT OR REPLACE INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps({'state': 'DONE', 'side': 'LONG', 'entries': records})))
        conn.commit()


def test_parent_batch_recovery_preserves_success_and_not_executed(local_db):
    original = {'status': 'unknown', 'operation_id': 'batch', 'results': [
        {'index': 0, 'action': 'cancel', 'status': 'completed', 'result': {'id': 'old-order', 'status': 'canceled'}},
        {'index': 1, 'action': 'open', 'status': 'unknown', 'result': {'error': 'lost acknowledgement'}},
        {'index': 2, 'action': 'close', 'status': 'not_executed'},
    ]}
    request = {'tool': 'execute_trade_actions', 'args': {'actions': [{'action': item['action']} for item in original['results']]}}
    callback = Mock(return_value=json.dumps(original))
    run_once('cfg', 'ETH/USDT', 'batch', request, callback)
    _save_native_evidence([_native_evidence('batch:1:0')])
    recovered = run_once('cfg', 'ETH/USDT', 'batch', request, callback)
    assert recovered['status'] == 'partial' and tool_result_status(recovered) == 'failed'
    assert recovered['results'][0] == original['results'][0]
    assert recovered['results'][2] == original['results'][2]
    assert recovered['results'][1]['status'] == 'completed'
    assert recovered['results'][1]['previous_result'] == original['results'][1]
    assert recovered['partial_execution'] is True and callback.call_count == 1


def test_recovery_never_removes_recorded_failed_item(local_db):
    original = {'status': 'unknown', 'results': [
        {'index': 0, 'status': 'failed', 'error': 'Invalid quantity'},
        {'index': 1, 'status': 'unknown', 'error': 'Lost ACK'},
        {'index': 2, 'status': 'not_executed'},
    ]}
    request = {'tool': 'execute_trade_actions', 'args': {'actions': [{}, {}, {}]}}
    callback = Mock(return_value=original)
    run_once('cfg', 'ETH/USDT', 'batch', request, callback)
    _save_native_evidence([_native_evidence('batch:0:0', 'old-order'), _native_evidence('batch:1:0')])
    result = run_once('cfg', 'ETH/USDT', 'batch', request, callback)
    assert result['status'] == 'failed'
    assert result['partial_execution'] is True
    assert result['results'][0] == original['results'][0]
    assert result['results'][2] == original['results'][2]
    assert callback.call_count == 1


def test_lost_parent_response_requires_evidence_for_every_requested_order(local_db):
    request = {'tool': 'open_position_real', 'args': {'orders': [{'action': 'BUY_LIMIT'}, {'action': 'BUY_LIMIT'}]}}
    callback = Mock(side_effect=TimeoutError('Entire response lost'))
    run_once('cfg', 'ETH/USDT', 'orders', request, callback)
    _save_native_evidence([_native_evidence('orders:0')])
    result = run_once('cfg', 'ETH/USDT', 'orders', request, callback)
    assert result['status'] == 'unknown' and result['partial_execution'] is True
    assert [item['status'] for item in result['results']] == ['completed', 'unknown']
    _save_native_evidence([_native_evidence('orders:0'), _native_evidence('orders:1', 'second-order')])
    result = run_once('cfg', 'ETH/USDT', 'orders', request, callback)
    assert result['status'] == 'completed' and result['partial_execution'] is False
    assert callback.call_count == 1


def test_old_receipt_without_request_metadata_cannot_infer_whole_batch_completion(local_db):
    callback = Mock(side_effect=TimeoutError('Lost response'))
    run_once('cfg', 'ETH/USDT', 'old-call', {}, callback)
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM trade_operation_requests')
        conn.commit()
    _save_native_evidence([_native_evidence('old-call:0')])
    result = run_once('cfg', 'ETH/USDT', 'old-call', {}, callback)
    assert result['status'] == 'unknown' and result['request_metadata_missing']
    assert result['reconciliation_evidence'][0]['id'] == 'native-order'
    assert callback.call_count == 1


def test_request_receipts_are_included_in_task_cleanup(local_db):
    from backend.database_cleanup import ConfigCleanupStore
    cleanup = ConfigCleanupStore(database.get_db_conn, lambda: '2026-10-01 00:00:00')
    run_once('cfg', 'ETH/USDT', 'operation', {}, lambda: {'status': 'completed'})
    assert cleanup.get_dependency_counts('cfg')['trade_operation_requests'] == 1
    cleanup.purge_all_data('cfg')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM trade_operation_requests').fetchone()[0] == 0
