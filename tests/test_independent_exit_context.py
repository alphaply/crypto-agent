"""The prompt must distinguish exchange exposure from this task's managed cycle."""
import pytest

from backend.utils.exit_context import independent_exit_context
from backend.utils.exit_context import independent_exit_chart_lines


def test_historical_error_and_receipts_are_not_current_pending():
    text = independent_exit_context({'real_positions': [], 'trade_operation_receipts': {'old': {'status': 'unknown'}}},
        plans=[{'symbol': 'ETH/USDT', 'side': 'LONG', 'execution_mode': 'independent_exits',
                'state': 'ACTIVE', 'error': 'old rejection', 'entries': [], 'exits': []}],
        contract_size=1, symbol='ETH/USDT')
    assert '待核验=False' in text
    assert '最新原操作' not in text and 'old rejection' not in text


def test_v2_dynamic_all_and_quantity_conflict_are_not_pending():
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'state': 'ACTIVE',
            'execution_mode': 'independent_exits', 'exits': [
                {'id': '1', 'status': 'waiting_trigger', 'contract_version': 2, 'order_type': 'stop_limit', 'amount': .7, 'filled': 0, 'price': 90, 'trigger_price': 95},
                {'id': '2', 'status': 'waiting_trigger', 'contract_version': 2, 'order_type': 'stop_limit', 'amount': .6, 'filled': 0, 'price': 89, 'trigger_price': 94},
                {'id': '3', 'status': 'waiting_trigger', 'contract_version': 2, 'order_type': 'take_profit_market', 'quantity_mode': 'all', 'amount': None, 'trigger_price': 110},
            ]}
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 1}]}, plans=[plan], contract_size=1, symbol='ETH/USDT')
    assert 'quantity_conflict=' in text and '待核验=False' in text
    assert 'remaining=全部（触发时该方向仓位）' in text
    assert 'SL LIMIT 触发后仍需限价成交' in text
    assert 'algo:3' in text


def test_chart_distinguishes_legacy_limit_and_two_conditional_prices():
    lines = independent_exit_chart_lines({'exits': [
        {'exit_type': 'take_profit_limit', 'price': 100, 'remaining': .2},
        {'contract_version': 2, 'order_type': 'stop_limit', 'price': 90, 'trigger_price': 95, 'remaining': .3},
        {'contract_version': 2, 'order_type': 'take_profit_market', 'trigger_price': 110, 'quantity_mode': 'all', 'remaining': None},
    ]})
    assert [line['label'] for line in lines] == ['LIMIT', 'SL TRIGGER', 'SL LIMIT', 'TP TRIGGER']
    assert lines[0]['type'] == 'limit'
    assert lines[-1]['amount'] is None and lines[-1]['quantity_mode'] == 'all'


def test_raw_real_waiting_trigger_does_not_claim_fill():
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'state': 'ACTIVE', 'execution_mode': 'independent_exits',
            'exits': [{'id': '3', 'status': 'open', 'waiting_trigger': True, 'fill_verified': False,
                       'contract_version': 2, 'order_type': 'stop_market', 'quantity_mode': 'all', 'amount': None}]}
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 1}]}, plans=[plan], contract_size=1, symbol='ETH/USDT')
    assert 'status=waiting_trigger' in text and 'remaining=全部' in text
    assert '待核验=False' in text


def test_market_all_context_is_a_submission_snapshot():
    text = independent_exit_context({'independent_exits': {'exits': [
        {'order_type': 'market', 'contract_version': 2, 'quantity_mode': 'all', 'resolved_amount': .5,
         'amount': None, 'remaining': None, 'order_status': 'open'},
    ]}})
    assert '全部（提交前快照；已解析=0.5）' in text
    assert '触发时该方向仓位）' not in text


@pytest.mark.parametrize('plans', [[], [
    {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'execution_mode': 'independent_exits', 'state': 'DONE'},
], [
    {'symbol': 'BTC/USDT:USDT', 'side': 'LONG', 'execution_mode': 'independent_exits', 'state': 'ACTIVE'},
]])
def test_visible_position_without_matching_cycle_is_not_presented_as_managed(plans):
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 2}]},
                                    plans=plans, contract_size=1, symbol='ETH/USDT')
    assert 'LONG 可见交易所持仓，但本任务没有活跃独立退出周期' in text
    assert 'LONG=2.0' in text and '待核验=True' in text
    assert '不能将手动仓或其他任务持仓自动认领' not in text


def test_matching_active_cycle_and_coverage_are_retained():
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'state': 'ACTIVE',
            'execution_mode': 'independent_exits', 'exits': [
                {'id': 'stop', 'status': 'open', 'exit_type': 'stop_market', 'amount': 2, 'filled': 0},
            ]}
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 2}]},
                                    plans=[plan], contract_size=1, symbol='ETH/USDT')
    assert 'LONG 本任务独立退出周期：ACTIVE' in text
    assert '没有活跃独立退出周期' not in text
    assert 'LONG=0' in text and '待核验=False' in text


@pytest.mark.parametrize('plans', [None, [{'symbol': 'ETH/USDT', 'read_error': 'unavailable'}]])
def test_unreadable_cycles_are_unknown_not_absent(plans):
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 2}]},
                                    plans=plans, contract_size=1, symbol='ETH/USDT')
    assert '周期记录读取不完整，归属未知' in text
    assert '没有活跃独立退出周期' not in text
    assert 'LONG=未知' in text and '待核验=True' in text


def test_cancelled_stop_with_replacement_intent_is_still_pending():
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'state': 'ACTIVE',
            'execution_mode': 'independent_exits', 'exits': [
                {'id': 'old-stop', 'status': 'canceled', 'exit_type': 'stop_market',
                 'amount': 2, 'filled': 0, 'replacement': {'client_id': 'new-stop'}},
            ]}
    text = independent_exit_context({'real_positions': [{'side': 'long', 'amount': 2}]},
                                    plans=[plan], contract_size=1, symbol='ETH/USDT')
    assert 'LONG=2.0' in text and '待核验=True' in text


def test_manual_adoption_is_distinguished_from_agent_entry_and_missing_cycle_has_next_step():
    account = {'real_positions': [{'side': 'short', 'amount': .37}]}
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'SHORT', 'state': 'ACTIVE',
            'execution_mode': 'independent_exits', 'entries': [], 'exits': [],
            'adoption': {'amount': .37, 'adopted_at': 100}}
    text = independent_exit_context(account, plans=[plan], contract_size=1, symbol='ETH/USDT')
    assert '来源为用户委托接管的手动仓' in text
    assert '不是本任务开仓成交' in text
    missing = independent_exit_context(account, plans=[], contract_size=1, symbol='ETH/USDT')
    assert '没有活跃独立退出周期' in missing and 'adopt_position_real' not in missing
