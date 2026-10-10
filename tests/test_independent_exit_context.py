"""The prompt must distinguish exchange exposure from this task's managed cycle."""
import pytest

from backend.utils.exit_context import independent_exit_context


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
