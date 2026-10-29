"""A resource's creation origin does not decide its current authority."""
from backend.utils.trade_context import project_trade_state
from backend.utils.trade_context_format import render_positions, render_orders
from backend.app.services.stats_service import _management_position_metadata, _management_order_metadata


def projection(state='eligible_unclaimed', owner=None):
    return {'policy': 'visible_first_claim', 'scopes': [], 'owner': 'cfg',
            'resource_states': {'LONG': {'state': state, 'visible': True, 'eligible': True,
                                        'can_claim': state == 'eligible_unclaimed', 'owner': owner}}}


def view(state='eligible_unclaimed', owner=None):
    management = projection(state, owner)
    positions = _management_position_metadata([{'side': 'LONG', 'symbol': 'ETH/USDT', 'amount': .02, 'entry_price': 2400}], management)
    orders = [_management_order_metadata({'order_id': 'regular:manual', 'pos_side': 'LONG', 'order_type': 'limit', 'status': 'open', 'remaining': .01}, {}, management)]
    return project_trade_state({'hedged': True, 'management': management, 'real_positions': positions, 'real_open_orders': orders},
        config_id='cfg', account_scope='account', symbol='ETH/USDT', plans=[])


def test_manual_resource_can_be_claimed_without_cycle_or_false_history():
    state = view()
    position, order = state.positions[0], state.orders[0]
    assert position['amount'] == .02 and position['can_claim'] and not position['managed']
    assert position['supported_actions'] == ['close'] and order['supported_actions'] == ['cancel', 'edit']
    assert not any(issue['reason_code'] == 'ownership_unverified' for issue in state.issues)
    text = render_positions(state) + render_orders(state)
    assert '可自动认领' in text


def test_owned_manual_resource_does_not_need_agent_entry_history():
    state = view('owned', 'cfg')
    assert state.positions[0]['ownership'] == 'task'
    assert state.orders[0]['managed'] and state.orders[0]['owner'] == 'cfg'
    assert '已纳管' in render_positions(state)


def test_competitor_is_local_and_preserves_current_quantities():
    state = view('conflict', 'another-task')
    assert state.positions[0]['amount'] == .02 and not state.positions[0]['managed']
    assert not state.positions[0]['can_claim'] and state.orders[0]['owner'] == 'another-task'
    assert '其他管理者占用' in render_orders(state)


def test_read_does_not_turn_stale_plan_into_current_exchange_fact():
    state = view('owned', 'cfg')
    assert state.positions[0]['observed_at'] is None
    assert state.orders[0]['verified'] is False
    assert state.orders[0]['remaining'] == .01


def test_attached_and_spot_formatters_show_authority_and_partial_evidence():
    from backend.utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly
    state = view('owned', 'cfg')
    position = {**state.positions[0], 'symbol': 'ETH/USDT', 'market_type': 'spot', 'available_amount': .01}
    text = format_positions_to_agent_friendly([position]) + format_orders_to_agent_friendly(state.orders)
    assert '已纳管' in text and '管理者 cfg' not in text and 'close' in text and 'cancel/edit' in text
    assert 'flat exposure unconfirmed' in format_positions_to_agent_friendly([], observation={'error': 'offline'})
    assert 'no-orders status unconfirmed' in format_orders_to_agent_friendly([], observation={'error': 'offline'})
