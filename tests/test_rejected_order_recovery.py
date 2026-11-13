"""Explicit venue rejection must never leave a nonexistent order blocking trading."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest

from backend.agent.single_trade_tools import SingleTradeParameterError, prepare_single_trade_request
from backend.utils.independent_exits import IndependentExits
from backend.utils.trade_order_evidence import classify_saved_order
from backend.utils.trade_receipts import TradeAttempt, current_attempt, project_receipt, unresolved_orders
from test_trade_tool_integration import simulation, real_short, call, recover


REJECTION = ('binanceusdm {"code":-4130,"msg":'
             '"An open stop or take profit order with GTE and closePosition in the direction is existing."}')
PARAMETER_REJECTION = 'binanceusdm {"code":-1102,"msg":"Mandatory parameter was not sent."}'


def stale_record(**overrides):
    return dict(client_id='original-client', operation_id='original-stop',
                id=None, status='submitting', submission_state='unknown',
                exit_type='stop_market', order_type='stop_market', contract_version=2,
                quantity_mode='all', amount=None, filled=0, trigger_price=2115,
                execution_error=REJECTION, **overrides)


@pytest.mark.parametrize('error', [ccxt.ExchangeError(REJECTION), ccxt.BadRequest(PARAMETER_REJECTION)])
def test_submission_recognizes_real_ccxt_rejection_classes(error):
    service = object.__new__(IndependentExits)
    service.ex = SimpleNamespace(create_order=Mock(side_effect=error))
    service._save = Mock()
    record = stale_record()
    plan = dict(symbol='ETH/USDT:USDT', side='SHORT', entries=[], exits=[record])
    attempt = TradeAttempt('original-stop', 'close')
    token = current_attempt.set(attempt)
    try:
        with pytest.raises(ValueError, match='Exchange rejected order'):
            service._submit(plan, record, 'market', 'buy', {})
    finally:
        current_attempt.reset(token)
    assert attempt.submission_state == record['submission_state'] == 'not_submitted'
    assert record['status'] == 'rejected'
    assert unresolved_orders(plan) == []
    assert project_receipt(plan, record, operation_id='original-stop', action='close')['status'] == 'failed'
    service.ex.create_order.assert_called_once()
    service._save.assert_called_once_with(plan)


def test_saved_rejection_retires_without_query_cancel_or_resubmit():
    record = stale_record(fill_pending=True, query_error='Order outcome unresolved',
                          attempt_count=3405, next_retry_at=9999999999, verification_pending=True)
    service = object.__new__(IndependentExits)
    service.config_id = 'cfg'
    service._query = Mock(side_effect=AssertionError('Rejected submission has no exchange order to query'))
    plan = dict(entries=[], exits=[record])
    assert service._collect_order_evidence(plan) == []
    assert record['status'] == 'rejected'
    assert record['submission_evidence']['code'] == '-4130'
    assert record['submission_evidence']['recovered_from'] == 'original_submission_error'
    assert record['execution_error'] == REJECTION
    assert record['attempt_count'] == 3405
    assert not any(key in record for key in ('fill_pending', 'query_error', 'next_retry_at', 'verification_pending'))
    assert unresolved_orders(plan) == []
    service._query.assert_not_called()
    previous = copy.deepcopy(record)
    classify_saved_order(record)
    assert record == previous


@pytest.mark.parametrize('change', [
    {'execution_error': 'binanceusdm {"code":-1007,"msg":"Execution status unknown"}'},
    {'execution_error': 'binanceusdm {"code":-2010,"msg":"Duplicate order sent."}'},
    {'execution_error': '', 'query_error': REJECTION},
    {'execution_error': '', 'cancel_error': REJECTION},
    {'id': 'accepted'}, {'exchange_order_id': 'accepted'}, {'execution_order_id': 'child'},
    {'acknowledged_at': 123}, {'filled': .1}, {'triggered': True},
    {'amendment': {'state': 'rejected'}}, {'replacement': {'operation_id': 'replacement'}},
])
def test_missing_order_or_later_errors_do_not_erase_uncertain_execution(change):
    record = {**stale_record(), **change}
    assert classify_saved_order(record)['state'] == 'submission_unknown'
    assert record['status'] == 'submitting'
    assert 'submission_evidence' not in record


@pytest.mark.parametrize('tool,extra', [('cancel_order', {}), ('edit_order', {'trigger_price': 2100})])
def test_local_submission_reference_cannot_be_sent_as_an_exchange_order(tool, extra):
    config = dict(mode='REAL', exit_mode='independent_exits', symbol='ETH/USDT:USDT')
    with pytest.raises(SingleTradeParameterError, match='本地提交引用'):
        prepare_single_trade_request(tool, dict(order_id='client:original-client', reason='cleanup', **extra),
                                     config, 'ETH/USDT:USDT')
    from backend.agent.trading_contracts import parse_v3_request
    action = 'cancel' if tool == 'cancel_order' else 'edit'
    with pytest.raises(ValueError, match='本地提交引用'):
        parse_v3_request({'contract_version': 3, 'actions': [dict(action=action,
                          order_id='client:original-client', reason='cleanup', **extra)]}, market='swap')


@pytest.mark.parametrize('error', [ccxt.ExchangeError(REJECTION), ccxt.BadRequest(PARAMETER_REJECTION)])
def test_rejected_stop_does_not_block_a_later_explicit_action(real_short, monkeypatch, error):
    service, exchange = real_short
    create = exchange.create_order
    rejected_create = Mock(side_effect=error)
    monkeypatch.setattr(exchange, 'create_order', rejected_create)
    arguments = dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect')
    assert call('close', arguments, 'rejected-stop')['status'] == 'failed'
    monkeypatch.setattr(exchange, 'create_order', create)
    result = call('close', {**arguments, 'trigger_price': 115}, 'new-stop')
    assert result['status'] == 'submitted' and result['admission']['allowed']
    assert service.snapshot('ETH/USDT')['unresolved_orders'] == []
    rejected_create.assert_called_once()


def test_old_unknown_receipt_recovers_from_original_error_and_stays_recovered(real_short, monkeypatch):
    from backend import database
    from backend.utils.trade_order_evidence import rejection_evidence
    service, exchange = real_short
    # Recreate the old exception-filter bug while keeping its real stored facts.
    monkeypatch.setattr('backend.utils.independent_exits.rejection_evidence', lambda _: None)
    monkeypatch.setattr('backend.utils.trade_order_evidence.rejection_evidence', lambda _: None)
    monkeypatch.setattr(exchange, 'create_order', Mock(side_effect=ccxt.ExchangeError(REJECTION)))
    arguments = dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect')
    assert call('close', arguments, 'legacy-stop')['status'] == 'unknown'
    monkeypatch.setattr('backend.utils.independent_exits.rejection_evidence', rejection_evidence)
    monkeypatch.setattr('backend.utils.trade_order_evidence.rejection_evidence', rejection_evidence)
    for method in ('create_order', 'cancel_order', 'edit_order'):
        monkeypatch.setattr(exchange, method, Mock(side_effect=AssertionError('Recovery cannot write to exchange')))
    result = recover('legacy-stop')
    assert result['status'] == 'failed'
    assert result['execution']['submission_state'] == 'not_submitted'
    assert 'close' not in result['continuation']['blocked_actions']
    stored = service._load('ETH/USDT:USDT', 'SHORT')['exits'][0]
    assert stored['status'] == 'rejected' and stored['submission_evidence']['code'] == '-4130'
    assert recover('legacy-stop')['status'] == 'failed'
    with database.get_db_conn() as conn:
        revisions = conn.execute('SELECT payload FROM trade_receipt_revisions WHERE operation_id=%s ORDER BY revision',
                                 ('legacy-stop',)).fetchall()
    from backend.utils.trade_operations import _decode_receipt
    assert _decode_receipt(revisions[0]['payload'])['status'] == 'unknown'
    assert _decode_receipt(revisions[-1]['payload'])['status'] == 'failed'


def test_agent_context_does_not_offer_cancel_for_a_client_only_record():
    from backend.utils.trade_context import project_trade_state
    from backend.utils.trade_context_format import render_orders
    record = stale_record()
    plan = dict(symbol='ETH/USDT:USDT', side='SHORT', account_scope='test-account',
                episode_id='original-cycle', state='ACTIVE', execution_mode='independent_exits',
                entries=[], exits=[record], contract_size=1, verified_at=123,
                position_observation={'status': 'verified', 'amount': 0})
    account = dict(real_positions=[], real_open_orders=[], hedged=True)
    state = project_trade_state(account, config_id='cfg', account_scope='test-account',
                                symbol=plan['symbol'], plans=[plan])
    assert len(state.orders) == 1 and state.orders[0]['supported_actions'] == []
    assert '本地提交记录，非已确认的交易所挂单' in render_orders(state)
    classify_saved_order(record)
    recovered = project_trade_state(account, config_id='cfg', account_scope='test-account',
                                    symbol=plan['symbol'], plans=[plan])
    assert recovered.orders == []
    assert not any(issue['reason_code'] in {'order_unresolved', 'fill_unverified'} for issue in recovered.issues)
