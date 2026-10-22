"""Price evidence stays visible across receipt projections; no external calls."""
from copy import deepcopy

from backend.mcp.spot_orders import _receipt
from backend.utils.trade_receipts import project_receipt
from backend.utils.trade_summary import compact_trade_receipt


def test_spot_entry_price_is_preserved_in_compact_batch():
    original = {'results': [{'index': 0, 'action': 'open', 'result': {
        'status': 'submitted', 'entry_price': 0.00001234, 'amount': 100}}]}
    saved = deepcopy(original)
    receipt = compact_trade_receipt(original)
    assert receipt['results'][0]['result']['price'] == 0.00001234
    assert original == saved


def test_cancel_projection_retains_target_price_without_fill_substitution():
    record = {'id': '1', 'status': 'canceled', 'price': 100, 'trigger_price': 99,
              'average': 98, 'filled': .1, 'fill_verified': True}
    receipt = project_receipt({'side': 'LONG'}, record, operation_id='cancel', action='cancel')
    assert receipt['price'] == 100
    assert receipt['trigger_price'] == 99
    assert compact_trade_receipt(receipt)['price'] == 100


def test_pending_amendment_retains_observed_and_requested_prices():
    record = {'id': '1', 'status': 'open', 'price': 100, 'trigger_price': 99,
              'amendment': {'operation_id': 'edit', 'state': 'pending', 'price': 110, 'trigger_price': 109}}
    receipt = compact_trade_receipt(project_receipt({}, record, operation_id='edit', action='edit'))
    assert receipt['price'] == 100
    assert receipt['requested_price'] == 110
    assert receipt['trigger_price'] == 99
    assert receipt['requested_trigger_price'] == 109
    assert receipt['status'] == 'pending'


def test_spot_conditional_cancel_keeps_both_prices():
    row = {'exit_id': 'exit', 'symbol': 'BTC/USDT', 'kind': 'stop_loss',
           'state': 'cancelled', 'amount': 1, 'filled': 0,
           'payload': {'exchange_id': '1', 'request': {'stop_loss': 90, 'stop_limit_price': 89}}}
    receipt = compact_trade_receipt(_receipt(row))
    assert receipt['stop_loss'] == 90
    assert receipt['stop_limit_price'] == 89
    assert 'price' not in receipt


def test_missing_prices_are_not_created():
    receipt = compact_trade_receipt(project_receipt({}, {'id': '1', 'status': 'canceled'},
                                                   operation_id='cancel', action='cancel'))
    assert 'price' not in receipt
    assert 'trigger_price' not in receipt
