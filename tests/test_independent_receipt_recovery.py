import json
from unittest.mock import Mock

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.trade_operations import run_once


@pytest.mark.parametrize('triggered', [False, True])
def test_recovered_all_keeps_plan_null_and_actual_fill_separate(tmp_path, monkeypatch, triggered):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'recovery.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    callback = Mock(side_effect=TimeoutError('lost ack'))
    request = {'tool': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'close_all': True}
    run_once('cfg', 'ETH/USDT', 'op', request, callback)
    record = {'id': '42', 'operation_id': 'op', 'status': 'open', 'contract_version': 2,
              'order_type': 'stop_market', 'quantity_mode': 'all', 'amount': None, 'filled': 1 if triggered else 0,
              'waiting_trigger': not triggered, 'fill_verified': triggered, 'triggered': triggered}
    if triggered:
        record.update(execution_order_id='43', execution_amount=3, resolved_amount=.3)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'execution_mode': 'independent_exits', 'contract_size': .1, 'exits': [record]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    receipt = run_once('cfg', 'ETH/USDT', 'op', request, callback)
    evidence = receipt['reconciliation_evidence'][0]
    assert receipt['status'] == 'submitted' and callback.call_count == 1
    assert evidence['amount'] is None and evidence['remaining'] is None
    assert evidence['quantity_unit'] == 'base'
    assert evidence['order_id'] == ('regular:43' if triggered else 'algo:42')
    assert evidence['order_status'] == ('partially_filled' if triggered else 'waiting_trigger')
    assert evidence['filled_amount'] == (.1 if triggered else None)
    assert evidence['actual_remaining'] == (pytest.approx(.2) if triggered else None)


def test_failed_replacement_does_not_recover_as_successful_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'replacement.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    callback = Mock(side_effect=TimeoutError('lost outer receipt'))
    request = {'tool': 'edit', 'order_id': 'algo:42', 'remaining_amount': .5}
    run_once('cfg', 'ETH/USDT', 'edit-op', request, callback)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'execution_mode': 'independent_exits', 'contract_size': 1,
            'exits': [{'id': '42', 'operation_id': 'original', 'last_operation_id': 'edit-op', 'status': 'canceled',
                       'order_type': 'stop_limit', 'contract_version': 2, 'amount': .7, 'filled': 0,
                       'replacement_error': 'Replacement quantity exceeds current position'}]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    receipt = run_once('cfg', 'ETH/USDT', 'edit-op', request, callback)
    assert receipt['status'] == 'failed' and callback.call_count == 1
    assert receipt['reconciliation_evidence'][0]['order_status'] == 'canceled'
    assert 'exceeds current position' in receipt['reconciliation_evidence'][0]['error']
