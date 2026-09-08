import ccxt
import pytest

from test_position_protection import service, entry


def setup_exchange(ex, order_id):
    ex.orders[order_id].update(price=100, amount=1)

    def edit(oid, symbol, type, side, amount, price):
        ex.calls.append(('edit', oid, amount, price))
        ex.orders[oid].update(price=price, amount=amount)
        return {'id': oid}

    ex.edit_order = edit


def test_native_amend_keeps_order_id_protection_and_no_cancel(service):
    svc, ex = service
    oid = svc.open('ETH/USDT', entry())['id']
    setup_exchange(ex, oid)
    result = svc.amend_entry('ETH/USDT', oid, price=101, reason='new support confirmed')
    assert result['amendment_state'] == 'confirmed'
    assert result['id'] == oid
    assert not any(c[0] == 'cancel' for c in ex.calls)
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['stop_loss'] == 90 and plan['take_profit'] == 120


def test_amend_partial_fill_total_quantity_cannot_cancel_filled_part(service):
    svc, ex = service
    oid = svc.open('ETH/USDT', entry())['id']
    setup_exchange(ex, oid)
    ex.orders[oid]['filled'] = .4
    ex.quantity = .4
    with pytest.raises(ValueError, match='already filled'):
        svc.amend_entry('ETH/USDT', oid, amount=.4, reason='reduce order')
    assert not any(c[0] == 'edit' for c in ex.calls)


def test_timeout_then_reconciliation_does_not_resubmit(service):
    svc, ex = service
    oid = svc.open('ETH/USDT', entry())['id']
    setup_exchange(ex, oid)
    edit = ex.edit_order

    def lost_response(*args):
        edit(*args)
        raise ccxt.RequestTimeout('response lost')

    ex.edit_order = lost_response
    with pytest.raises(ccxt.RequestTimeout):
        svc.amend_entry('ETH/USDT', oid, price=101, reason='confirmed support')
    svc.reconcile_all()
    assert svc._load('ETH/USDT:USDT', 'LONG')['entries'][0]['amendment']['state'] == 'confirmed'
    assert len([c for c in ex.calls if c[0] == 'edit']) == 1


def test_amend_rejects_invalid_protection_price_and_unowned_order(service):
    svc, ex = service
    oid = svc.open('ETH/USDT', entry())['id']
    setup_exchange(ex, oid)
    with pytest.raises(ValueError, match='reference'):
        svc.amend_entry('ETH/USDT', oid, price=89, reason='move entry')
    with pytest.raises(ValueError, match='configuration'):
        svc.amend_entry('ETH/USDT', 'manual-order', price=101, reason='move entry')
