"""Case-only model tool names keep authorization, validation and idempotency."""
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from backend.agent import independent_tools, tool_registry
from backend.config import config
from test_trade_tool_integration import simulation, call


@pytest.mark.parametrize('name', ['Edit', 'EDIT', 'eDiT'])
def test_edit_alias_and_canonical_replay_share_one_operation(simulation, monkeypatch, name):
    dispatch = Mock(return_value=json.dumps({'status': 'completed'}))
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    args = {'order_id': 'algo:1000002618311010', 'trigger_price': 2357, 'reason': '调整保护'}
    assert call(name, args, 'edit-case')['status'] == 'completed'
    assert call('edit', args, 'edit-case')['status'] == 'completed'
    dispatch.assert_called_once()
    assert dispatch.call_args.args[0] == 'edit'
    assert tool_registry.uses_scoped_admission(name, 'cfg', args)


@pytest.mark.parametrize('mode,exit_mode', [('REAL', 'attached_optional'), ('STRATEGY', 'attached_required'), ('SPOT_DCA', 'independent_exits')])
def test_alias_cannot_bypass_mode(simulation, monkeypatch, mode, exit_mode):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': mode, 'exit_mode': exit_mode})
    dispatch = Mock()
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    assert 'not allowed' in tool_registry.run_trade_tool('Edit', {}, 'cfg', 'ETH/USDT')
    dispatch.assert_not_called()


def test_file_edit_parameters_are_never_a_trading_action(simulation, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    with pytest.raises(ValueError):
        tool_registry.run_trade_tool('Edit', {'file_path': '/etc/config', 'old_string': 'x', 'new_string': 'y'}, 'cfg', 'ETH/USDT')
    dispatch.assert_not_called()
    assert tool_registry.canonical_tool_name('EDIT_FILE') == 'EDIT_FILE'


@pytest.mark.parametrize('approved', [False, True])
def test_chat_case_alias_still_requires_approval(simulation, monkeypatch, approved):
    from backend.agent import chat_graph
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'mode': 'STRATEGY', 'symbol': 'ETH/USDT', 'exit_mode': 'independent_exits'})
    approval = Mock(return_value=approved)
    dispatch = Mock(return_value=json.dumps({'status': 'completed'}))
    monkeypatch.setattr(chat_graph, 'interrupt', approval)
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    message = AIMessage(content='', tool_calls=[{'id': 'chat-edit', 'name': 'Edit',
                        'args': {'order_id': 'algo:123', 'trigger_price': 2357, 'reason': '调整保护'}}])
    result = chat_graph.tools_node({'messages': [message]}, {'configurable': {'config_id': 'cfg'}})
    assert approval.call_count == 1 and dispatch.call_count == int(approved)
    if not approved:
        assert result['messages'][0].content == 'Rejected by user.'
