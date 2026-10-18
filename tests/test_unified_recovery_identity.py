"""A recovery read cannot borrow another order's successful acknowledgement."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import database
from backend.agent import agent_tools, unified_dispatch
from backend.config import config
from backend.database.schema import initialize_schema


@pytest.fixture
def ledger(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'identity.sqlite'))
    with database.get_db_conn() as conn:
        pass  # financial_pg installs the complete native migration schema.
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'REAL', 'exit_mode': 'attached_optional'})


@pytest.mark.parametrize('observed', [
    {'id': 'unrelated', 'symbol': 'ETH/USDT:USDT'},
    {'id': 'original', 'symbol': 'BTC/USDT:USDT'},
])
def test_native_close_recovery_rejects_wrong_saved_identity_or_market(ledger, monkeypatch, observed):
    action = {'action': 'close', 'side': 'LONG', 'order_type': 'market', 'amount': .2, 'reason': 'reduce'}
    with database.get_db_conn() as conn:
        unified_dispatch._native_table(conn)
        conn.execute('INSERT INTO unified_native_intents(config_id,operation_id,symbol,account_scope,client_id,request_json,result) VALUES(%s,%s,%s,%s,%s,%s,%s)', (
            'cfg', 'close:0', 'ETH/USDT', 'account', 'original-client',
            json.dumps({'action': action, 'contract_size': 1}), json.dumps({'id': 'original'})))
        conn.commit()
    exchange = SimpleNamespace(fetch_order=Mock(return_value={**observed, 'status': 'closed', 'filled': .2}))
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=exchange))
    monkeypatch.setattr('backend.utils.execution_ledger.account_scope', lambda *_: 'account')
    receipt = unified_dispatch.refresh_native_operation('cfg', 'ETH/USDT', 'close:0', {'status': 'unknown'})
    assert receipt['status'] == 'unknown'
    assert receipt['query']['status'] == 'failed'
    assert 'filled_amount' not in receipt
    exchange.fetch_order.assert_called_once_with('original', 'ETH/USDT', {})


@pytest.mark.parametrize('identity,client,observed,accepted', [
    ('original', 'client', {'id': 'other', 'clientOrderId': 'client'}, False),
    ('original', 'client', {'id': 'original', 'symbol': 'BTC/USDT:USDT'}, False),
    (None, 'client', {'id': 'new', 'clientOrderId': 'other'}, False),
    (None, 'client', {'id': 'new', 'info': {'clientAlgoId': 'client'}}, True),
    ('original', 'client', {'id': 'original', 'symbol': 'ETH/USDT:USDT'}, True),
])
def test_attached_mutable_query_checks_original_identity_before_projection(identity, client, observed, accepted):
    record = {'id': identity, 'client_id': client, 'status': 'open'}
    def query(target, symbol, trigger):
        target.update(id=observed['id'], status='closed')
        return {**observed, 'status': 'closed', 'filled': .2}
    service = SimpleNamespace(_query=Mock(side_effect=query))
    if accepted:
        assert unified_dispatch._observe_attached_order(service, record, 'ETH/USDT', True)['filled'] == .2
    else:
        with pytest.raises(ValueError, match='Original attached order'):
            unified_dispatch._observe_attached_order(service, record, 'ETH/USDT', True)


def test_attached_recovery_does_not_persist_mismatched_observation(ledger, monkeypatch):
    from backend.utils.trade_operations import initialize_trade_operations_schema
    payload = {'account_scope': 'account', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'record_version': 0,
               'entries': [{'id': 'original', 'client_id': 'client', 'operation_id': 'open:0', 'status': 'open'}]}
    with database.get_db_conn() as conn:
        initialize_trade_operations_schema(conn.cursor())
        conn.execute('INSERT INTO trade_operation_requests(config_id,operation_id,request_json) VALUES(%s,%s,%s)', (
            'cfg', 'open:0', json.dumps({'contract_version': 3, 'action': 'open', 'arguments': {'side': 'LONG'}})))
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                     ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(payload)))
        conn.commit()
    def query(record, symbol, trigger):
        record.update(id='unrelated', status='closed', filled=.2)
        return {'id': 'unrelated', 'status': 'closed', 'filled': .2}
    service = SimpleNamespace(account_scope='account', _market=lambda _: {'symbol': 'ETH/USDT:USDT', 'contractSize': 1},
                              _load=lambda *args: deepcopy(payload), _query=query)
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: object())
    monkeypatch.setattr('backend.utils.position_protection.PositionProtection', lambda _: service)
    receipt = unified_dispatch._refresh_attached_operation('cfg', 'ETH/USDT', 'open:0', {'status': 'unknown'})
    assert receipt['status'] == 'unknown' and receipt['query']['status'] == 'failed'
    with database.get_db_conn() as conn:
        saved = json.loads(conn.execute('SELECT payload FROM real_protection_plans').fetchone()[0])
    assert saved == payload
