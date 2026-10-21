"""Regression for three resized exits with delayed cancellation readback."""
import ccxt
import pytest

from backend.agent.trading_contracts import UnifiedTradeActionsSchema
from backend.agent.unified_trading import run_ordered_actions
from backend.utils.trade_operations import financial_ack_only
from backend.utils.independent_admission import assess_admission, action_intent
from test_independent_exits import local_db, live_service


def test_three_resizes_finish_in_one_batch_with_stale_cancel_reads(live_service):
    service, exchange = live_service
    exits = [service.close('ETH/USDT', 'LONG', .3, 'stop_market', trigger_price=price, contract_version=2)
             for price in (90, 85, 80)]
    cancel, fetch = exchange.cancel_order, exchange.fetch_order
    canceled = []

    def cancel_without_fill(oid, symbol, params):
        response = cancel(oid, symbol, params)
        canceled.append(str(oid))
        return {**response, 'filled': None}

    def stale_read(oid, symbol, params):
        response = fetch(oid, symbol, params)
        if str(oid) in canceled:
            return {**response, 'status': 'open', 'filled': None}
        return response

    exchange.cancel_order, exchange.fetch_order = cancel_without_fill, stale_read
    parsed = UnifiedTradeActionsSchema.model_validate({
        'contract_version': 3,
        'actions': [{'action': 'edit', 'symbol': 'ETH/USDT:USDT',
                     'order_id': row['id'], 'remaining_amount': .2, 'reason': 'resize'}
                    for row in exits],
    })

    def dispatch(action, index, operation_id, symbol):
        return service.edit(symbol, action.order_id, remaining_amount=action.remaining_amount,
                            reason=action.reason, operation_id=operation_id)

    before = len(exchange.calls)
    token = financial_ack_only.set(True)
    try:
        receipt = run_ordered_actions(parsed, 'resize-three', 'cfg', dispatch)
    finally:
        financial_ack_only.reset(token)
    assert receipt['status'] in {'completed', 'submitted'}, receipt
    assert all(row['status'] in {'completed', 'submitted'} for row in receipt['results'])
    assert len(exchange.calls) == before + 3
    assert canceled == [str(row['id']) for row in exits]
    assert [row['amount'] for row in exchange.calls[-3:]] == pytest.approx([.2, .2, .2])


def test_unknown_cancellation_does_not_create_duplicate_exit(live_service):
    service, exchange = live_service
    old = service.close('ETH/USDT', 'LONG', .3, 'stop_market', trigger_price=90)
    before = len(exchange.calls)

    def unavailable(*args, **kwargs):
        raise ccxt.RequestTimeout('No cancellation acknowledgement')

    exchange.cancel_order = unavailable
    token = financial_ack_only.set(True)
    try:
        with pytest.raises(RuntimeError, match='Cancellation acknowledgement incomplete'):
            service.edit('ETH/USDT', old['id'], remaining_amount=.2,
                         reason='resize', operation_id='cancel-unknown')
    finally:
        financial_ack_only.reset(token)
    assert len(exchange.calls) == before


@pytest.mark.parametrize('acknowledged,triggered,allowed', [
    (True, False, True), (False, False, False), (True, True, False),
])
def test_only_confirmed_replacement_clears_old_fill_blocker(acknowledged, triggered, allowed):
    old = dict(id='old', client_id='old-client', status='canceled', fill_pending=True,
               cancel_confirmed_at=1, triggered=triggered, exit_type='stop_market')
    successor = dict(id='new', client_id='new-client', replaces='old', status='open',
                     exit_type='stop_market', fill_verified=True,
                     acknowledged_at=2 if acknowledged else None)
    target = dict(id='target', client_id='target-client', status='open',
                  exit_type='stop_market', fill_verified=True)
    plan = dict(side='LONG', entries=[], exits=[old, successor, target],
                position_observation={'status': 'verified', 'amount': 1})
    intent = action_intent(plan, 'edit', target, {'remaining_amount': .2})
    assert assess_admission(plan, intent, hedged=True, target=target)['allowed'] is allowed
