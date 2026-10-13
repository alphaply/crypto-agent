import copy
import json
from types import SimpleNamespace
import pytest

from backend.utils.trade_context import project_trade_state, render_orders, render_positions, load_trade_plans
from backend import database
from test_trade_tool_integration import simulation, real_short, call


def sample():
    orders = [dict(id='sl', status='open', waiting_trigger=True, fill_verified=True, filled=0,
                   amount=.15, order_type='stop_market', quantity_mode='fixed', trigger_price=95,
                   operation_id='protect')]
    plan = dict(symbol='ETH/USDT', account_scope='account', side='LONG', episode_id='cycle',
                execution_mode='independent_exits', state='ACTIVE', contract_size=1,
                position_observation={'status': 'verified', 'amount': .15}, exits=orders)
    account = dict(hedged=True, real_positions=[dict(side='LONG', amount=.15, entry_price=100)],
                   real_open_orders=[dict(order_id='algo:sl', pos_side='LONG')])
    return account, plan


def view(account, plans, **kwargs):
    return project_trade_state(account, config_id='cfg', account_scope='account', symbol='ETH/USDT',
                               plans=plans, contract_size=1, **kwargs)


def test_current_view_is_invariant_under_completed_history_and_error_prose():
    account, plan = sample()
    expected = view(account, [plan])
    assert expected.issues == [] and expected.coverage['uncovered']['LONG'] == 0
    assert len(expected.orders) == 1
    plan['error'] = '旧拒单，禁止加仓'
    history = [dict(plan, episode_id=f'old-{n}', state='DONE', exits=[dict(plan['exits'][0], status='closed')]) for n in range(100)]
    account['trade_operation_receipts'] = {str(n): {'status': 'completed'} for n in range(14)}
    assert render_orders(view(account, [plan, *history])) == render_orders(expected)
    assert '禁止加仓' not in render_orders(view(account, [plan]))


def test_parent_child_identity_and_unknown_quantity_are_not_duplicated():
    account, plan = sample()
    plan['exits'][0].update(execution_order_id='child', filled=None, quantity_mode='all')
    account['real_open_orders'].append(dict(order_id='regular:child'))
    state = view(account, [plan])
    assert len(state.orders) == 1
    assert state.orders[0]['order_id'] == 'regular:child'
    assert state.orders[0]['parent_order_id'] == 'algo:sl'
    assert state.orders[0]['remaining'] is None
    assert state.coverage['uncovered']['LONG'] == 0
    plan['exits'][0]['quantity_mode'] = 'fixed'
    assert view(account, [plan]).coverage['uncovered']['LONG'] is None


def test_mock_and_real_coverage_and_amount_semantics_match():
    account, plan = sample()
    real = view(account, [plan])
    mock = {'independent_exits': {'positions': account['real_positions'], 'exits': [
        dict(plan['exits'][0], pos_side='LONG', remaining=.15)]}}
    simulated = view(mock, [])
    assert simulated.coverage == real.coverage
    assert simulated.positions[0]['amount'] == real.positions[0]['amount']
    assert simulated.orders[0]['remaining'] == real.orders[0]['remaining']


def test_old_unresolved_all_is_scoped_and_not_dropped_after_twenty(real_short):
    service, exchange = real_short
    exchange.timeout_next = True
    call('close', dict(side='SHORT', order_type='market', close_all=True, reason='close'), 'old-unknown')
    with database.get_db_conn() as conn:
        for n in range(25):
            conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                         ('cfg', f'history-{n}', 'ETH/USDT', 'hash', 'completed', '{}', 0, n))
        conn.commit()
    writes = len(exchange.calls)
    plans = load_trade_plans('cfg', service.account_scope, 'ETH/USDT')
    account = {'hedged': True, 'real_positions': [], 'real_open_orders': []}
    state = project_trade_state(account, config_id='cfg', account_scope=service.account_scope,
                               symbol='ETH/USDT', plans=plans, contract_size=1)
    relevant = [i for i in state.issues if i['operation_id'] == 'old-unknown']
    assert relevant and all(i['side'] == 'SHORT' and i['scope'] == 'same_side' for i in relevant)
    account['hedged'] = False
    shared = project_trade_state(account, config_id='cfg', account_scope=service.account_scope,
                                symbol='ETH/USDT', plans=plans, contract_size=1)
    assert any(i['scope'] == 'shared_net_position' for i in shared.issues)
    assert all('blocked_actions' not in i for i in shared.issues)
    assert len(exchange.calls) == writes


def test_condition_channel_read_failure_makes_coverage_unknown():
    account, plan = sample()
    account['evidence_state'] = {'trigger_orders': {'error': 'unavailable'}}
    state = view(account, [plan])
    assert state.coverage['uncovered']['LONG'] is None
    assert any(i['reason_code'] == 'trigger_orders_unavailable' for i in state.issues)
    account['error'] = 'orders failed'
    account['evidence_state']['positions'] = {'observed_at': 100}
    account['evidence_state']['orders'] = {'error': 'orders failed'}
    partial = view(account, [plan])
    assert partial.positions[0]['amount'] == .15  # A separate failed read does not erase observed exposure.


@pytest.mark.parametrize('trigger_failure', [False, True])
def test_actual_account_snapshot_provides_separate_observation_times_and_gaps(trigger_failure):
    from backend.utils.market_data import MarketTool
    def fetch_orders(symbol, params=None):
        if params:
            if trigger_failure:
                raise TimeoutError('trigger channel unavailable')
            return []
        return [dict(id='entry', symbol=symbol, side='buy', type='limit', status='open', price=90,
                     amount=2, remaining=1, info={'positionSide': 'LONG'})]
    tool = object.__new__(MarketTool)
    tool.market_type = 'swap'
    tool.exchange = SimpleNamespace(options={}, fetch_balance=lambda: {'USDT': {'free': 100, 'total': 100}},
        fetch_positions=lambda _: [dict(symbol='ETH/USDT', side='long', contracts=2, contractSize=.1,
                                        entryPrice=95, unrealizedPnl=1, hedged=True)],
        fetch_open_orders=fetch_orders, market=lambda _: {'contract': True, 'contractSize': .1})
    result = tool.get_account_status('ETH/USDT', is_real=True, config_id='cfg')
    assert not result['error']
    assert result['real_positions'][0]['amount'] == .2 and result['hedged'] is True
    assert result['real_open_orders'][0]['remaining'] == .1
    assert result['real_open_orders'][0]['exchange_channel'] == 'regular'
    assert result['evidence_state']['positions']['observed_at'] <= result['evidence_state']['orders']['observed_at']
    assert ('trigger_orders' in result['evidence_state']) == trigger_failure


def test_completed_operation_with_cleanup_pending_is_not_silently_resolved():
    account, plan = sample()
    evidence = {'close-old': {'resolution': 'matched', 'latest': {
        'status': 'completed', 'operation_id': 'close-old', 'side': 'LONG', 'filled_amount': .37,
        'cleanup': {'status': 'pending', 'orders': []}}}}
    state = view(account, [plan], referenced_evidence=evidence)
    assert any(i['reason_code'] == 'cycle_cleanup_unverified' and i['operation_id'] == 'close-old' for i in state.issues)


def test_failed_read_does_not_mean_empty_account_and_terminal_missing_fill_is_issue():
    account, plan = sample()
    account.update(error='positions failed', real_positions=[])
    plan['exits'][0].update(status='canceled', fill_pending=True, fill_verified=False)
    state = view(account, [plan])
    assert state.coverage['uncovered']['LONG'] is None
    assert '未知' in render_positions(state)
    assert any(i['reason_code'] == 'fill_unverified' for i in state.issues)
    assert not any(o['order_id'] == 'algo:sl' and o['ownership'] == 'task' for o in state.orders)
