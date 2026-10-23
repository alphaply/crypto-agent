"""Position adjustments report collateral effects without inventing executions."""
from copy import deepcopy

import pytest

from backend.utils.trade_summary import compact_trade_receipt, render_trade_receipt


@pytest.mark.parametrize(
    'action,leverage_after,margin_after,cash_change,label,cash_text',
    [
        ('set_leverage', 10, 120, 100, '调整持仓杠杆', '账户现金增加 100 USDT'),
        ('set_leverage', 4, 270, -50, '调整持仓杠杆', '账户现金减少 50 USDT'),
        ('set_leverage', 5, 220, 0, '调整持仓杠杆', '账户现金变化 0 USDT'),
        ('add_margin', 5, 270, -50, '增加逐仓保证金', '账户现金减少 50 USDT'),
        ('reduce_margin', 5, 210, 10, '减少逐仓保证金', '账户现金增加 10 USDT'),
    ],
)
def test_position_adjustment_keeps_money_changes_without_claiming_a_fill(
    action, leverage_after, margin_after, cash_change, label, cash_text,
):
    added_margin = margin_after - 1000 / leverage_after
    payload = {
        'status': 'completed', 'action': action, 'operation_id': 'adjust-position-1',
        'position_id': 'binance:swap:BTC/USDT:USDT:LONG:cycle-7',
        'resource_id': 'binance:swap:BTC/USDT:USDT:LONG:cycle-7',
        'symbol': 'BTC/USDT:USDT', 'side': 'LONG', 'currency': 'USDT',
        'leverage_before': 5, 'leverage_after': leverage_after,
        'margin_before': 220, 'margin_after': margin_after,
        'base_margin': 1000 / leverage_after, 'added_margin': added_margin, 'cash_change': cash_change,
        'available_cash': 500 + cash_change,
        'position': {
            'resource_id': 'binance:swap:BTC/USDT:USDT:LONG:cycle-7',
            'symbol': 'BTC/USDT:USDT', 'exchange': 'binance', 'side': 'LONG',
            'amount': .01, 'entry_price': 100000, 'leverage': leverage_after,
            'margin': margin_after, 'base_margin': 1000 / leverage_after, 'added_margin': added_margin,
        },
        'account': {'cash': 500 + cash_change, 'debt': 100, 'private_history': ['large']},
        'interest_reserve': {'advisory_only': True, 'message': '请留出 1 USDT 支付下次利息。'},
    }
    if action != 'set_leverage':
        payload['amount'] = abs(cash_change)
    original = deepcopy(payload)

    compact = compact_trade_receipt(payload)
    assert payload == original
    assert 'account' not in compact
    for key in ('position_id', 'resource_id', 'symbol', 'side', 'currency', 'leverage_before',
                'leverage_after', 'margin_before', 'margin_after', 'base_margin', 'added_margin',
                'cash_change', 'available_cash', 'interest_reserve'):
        assert compact[key] == payload[key]
    assert compact['position'] == payload['position']
    assert not {'fill', 'filled_amount', 'order_status', 'price'} & compact.keys()

    for receipt in (payload, compact, {'result': compact}):
        text = render_trade_receipt(receipt)
        assert label in text
        assert payload['position_id'] in text
        assert f'杠杆 5× → {leverage_after}×' in text
        assert f'逐仓保证金 220 → {margin_after} USDT' in text
        assert f'额外保证金 {added_margin} USDT' in text
        assert cash_text in text
        assert f'调整后可用现金 {500 + cash_change} USDT' in text
        assert payload['interest_reserve']['message'] in text
        assert '成交' not in text
        assert '数量' not in text
        assert '价格' not in text


def test_failed_position_adjustment_does_not_claim_cash_or_margin_changed():
    receipt = compact_trade_receipt({
        'status': 'failed', 'action': 'add_margin', 'position_id': 'position-7',
        'operation_id': 'adjust-failed-1', 'amount': 2000, 'currency': 'USDT',
        'error': 'Available cash is insufficient',
    })
    text = render_trade_receipt(receipt)
    assert '增加逐仓保证金' in text
    assert 'position-7' in text
    assert '失败。原因：Available cash is insufficient' in text
    assert '账户现金' not in text
    assert '调整后' not in text
    assert '成交' not in text
