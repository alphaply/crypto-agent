import copy
import json
from dataclasses import replace

import pytest

from backend.utils.trade_context import DecisionTradeState, render_orders, render_positions
from test_decision_state_context import sample, view


def user_example():
    position = dict(side='LONG', amount=0.07, last_known_amount=None, entry_price=2415.0,
                    unrealized_pnl=7.93163651, ownership='task', adoption=None,
                    source='exchange_snapshot', observed_at=1791869420.5128891)
    orders = []
    for identity, kind, amount, price, observed in [
        ('algo:1000002617965160', 'take_profit_market', .07, 2545., 1791869402.8427713),
        ('algo:1000002618311010', 'stop_market', .07, 2357., 1791869402.9315712),
        ('regular:8389766294627213395', 'limit', .1, 2385., 1791869402.7528615),
    ]:
        conditional = kind != 'limit'
        orders.append(dict(order_id=identity, parent_order_id=None, client_id='fixture-original-client-id', side='LONG',
                           role='exit' if conditional else 'entry', order_type=kind, status='waiting_trigger' if conditional else 'open',
                           price=None if conditional else price, trigger_price=price if conditional else None, quantity_mode='fixed',
                           remaining=amount, resolved_amount=None, ownership='task', source='local_execution_evidence',
                           observed_at=observed, verified=True, snapshot_observed_at=1791869420.7234569,
                           visible_in_snapshot=True, snapshot_remaining=amount, snapshot_status='open', snapshot_price=price,
                           snapshot_trigger_price=price if conditional else None))
    return DecisionTradeState(dict(symbol='ETH/USDT', account_scope='fixture', config_id='cfg'), [position], orders,
                              dict(uncovered={'LONG': 0., 'SHORT': 0.}, quantity_conflict=[]), [],
                              dict(positions='read', local_cycles='read', sources={}))


def old_text(state):
    return '\n'.join(json.dumps(item, ensure_ascii=False) for item in [*state.positions, *state.orders])


def test_user_three_orders_compact_complete_and_read_only():
    state = user_example()
    before = copy.deepcopy(state)
    text = render_positions(state) + '\n' + render_orders(state)
    assert '多仓 0.07｜均价 2415｜浮盈约 +7.93 USDT｜本任务' in text
    assert '持仓：持仓' not in text
    for row in ['algo:1000002617965160 | 多仓止盈市价 | 0.07 | 触发 2545 | 待触发',
                'algo:1000002618311010 | 多仓止损市价 | 0.07 | 触发 2357 | 待触发',
                'regular:8389766294627213395 | 多仓限价入场 | 0.1 | 限价 2385 | 挂单中']:
        assert row in text
    for order in state.orders:
        assert text.count(order['order_id']) == 1
    assert 'UTC+8' in text and '本地核验' in text and '交易所快照' in text
    for omitted in ['null', 'client_id', 'observed_at', 'snapshot_', 'verified', '1791869420', '{']:
        assert omitted not in text
    assert len(text) < len(old_text(state)) * .6
    assert state == before
    for item in [*state.orders, *state.positions]:
        item['unrelated_receipt'] = 'unrelated history ' * 10000
    assert render_positions(state) + '\n' + render_orders(state) == text


def test_tiny_exact_quantities_two_prices_parent_and_dynamic_all():
    state = user_example()
    tiny = '0.0000000000000000000000001234500'
    order = dict(state.orders[0], order_id='regular:child', parent_order_id='algo:parent', order_type='stop_limit',
                 remaining=tiny, price='2350.000000000000000000123', trigger_price='2357.000000000000000000001')
    dynamic = dict(state.orders[1], order_id='algo:all', quantity_mode='all', remaining=None, resolved_amount=.07)
    text = render_orders(replace(state, orders=[order, dynamic]))
    for value in ['regular:child', '父单 algo:parent', tiny.rstrip('0'), '限价 2350.000000000000000000123',
                  '触发 2357.000000000000000000001', '全部（触发时该方向）；已确定 0.07', '仍须限价成交']:
        assert value in text


@pytest.mark.parametrize('unknown', [None, float('nan'), float('inf')])
def test_unknown_is_not_zero_and_last_known_ownership_remain(unknown):
    state = user_example()
    position = dict(state.positions[0], amount=unknown, last_known_amount=.07, ownership='unverified', observed_at=None)
    orders = [dict(state.orders[0], remaining=unknown, ownership='unverified', verified=False),
              dict(state.orders[1], remaining=0, verified=True)]
    text = render_positions(replace(state, positions=[position])) + render_orders(replace(state, orders=orders))
    assert '未知（最后已知 0.07）' in text and '归属未核验' in text and '事实待核验' in text
    assert '多仓止盈市价 | 未知 |' in text and '多仓止损市价 | 0 |' in text
    assert '交易所快照 未知' in text


def test_snapshot_disagreement_coverage_and_scoped_issues_keep_facts():
    account, plan = sample()
    plan['verified_at'] = 1791869300
    account['evidence_state'] = {'orders': {'observed_at': 1791869420}}
    account['real_open_orders'][0]['remaining'] = .4
    state = view(account, [plan])
    text = render_orders(state)
    for fact in ['快照余量 0.4', '订单余量冲突', '同方向', 'algo:sl', '快照余量与本地成交对齐', '止损未覆盖估计：多仓 0.15']:
        assert fact in text
    assert 'identity' not in text and 'missing_evidence' not in text
    conflicts = replace(state, coverage={'uncovered': {'LONG': None}, 'quantity_conflict': [
        dict(side='LONG', group='stop_loss', fixed_remaining=.4, position_amount=.15)]})
    text = render_orders(conflicts)
    assert '多仓 未知' in text and '多仓止损固定余量 0.4 > 持仓 0.15' in text


def test_missing_account_never_claims_no_orders_or_no_issues():
    state = view({'error': 'snapshot offline'}, None)
    text = render_positions(state) + render_orders(state)
    assert '未知' in text and '账户快照不可用' in text and '普通单读取失败' in text
    assert '无挂单。' not in text and '无（当前数据源读取完整）' not in text


def test_multiple_observation_times_and_missing_time_remain_explicit():
    state = user_example()
    state.orders[0]['observed_at'] = 1791869000
    state.orders[1]['observed_at'] = None
    text = render_orders(state)
    assert '～' in text and '部分未知' in text
    assert text == render_orders(state)


def test_client_only_identity_and_issue_effect_not_lost():
    state = user_example()
    order = dict(state.orders[0], order_id=None, client_id='pending-original-id', verified=False)
    issue = dict(reason_code='execution_unverified', operation_id='original-operation', side='LONG',
                 scope='shared_net_position', affected_effects=['increase_exposure'], last_known_amount=.37,
                 missing_evidence='verified_execution_outcome', observed_at=1791869420)
    text = render_orders(replace(state, orders=[order], issues=[issue]))
    for value in ['client:pending-original-id', 'original-operation', '共享净仓', '增加敞口', '最后已知量 0.37']:
        assert value in text
