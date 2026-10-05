from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend.agent import agent_graph, tool_registry
from backend.utils.decision_journal import decision_journal


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY', 'SPOT_DCA'])
def test_trading_agents_cannot_edit_rules_even_with_forged_calls(mode, monkeypatch):
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda *_: {'mode': mode})
    assert 'manage_trading_rules' not in {tool.name for tool in tool_registry.get_trade_tools_for_mode(mode)}
    assert 'not allowed' in tool_registry.run_trade_tool('manage_trading_rules', {'action': 'apply'}, 'cfg', 'BTC/USDT')


def test_bounded_journal_keeps_uncertain_execution_before_long_narrative():
    messages = [
        AIMessage(content='plan', tool_calls=[{'name': 'open_position_real', 'id': 'entry-1', 'args': {}}]),
        ToolMessage(tool_call_id='entry-1', content='{"status":"unknown","order_id":"pending-1"}'),
        AIMessage(content='plan only ' * 1000),
    ]
    text = decision_journal(messages)
    assert len(text) <= 500
    assert 'unknown=1' in text[:100]
    assert 'pending-1' in text
    assert '原文截取' in text


def test_default_context_does_not_read_daily_reports(monkeypatch):
    daily = Mock(side_effect=AssertionError('Daily reviews must be explicitly requested by a custom template'))
    monkeypatch.setattr(agent_graph, 'get_daily_summaries', daily)
    monkeypatch.setattr(agent_graph, 'format_short_memory_for_llm', lambda *_, **__: 'memory')
    monkeypatch.setattr(agent_graph, 'format_recent_decisions', lambda *_: 'decisions')
    monkeypatch.setattr(agent_graph, 'format_trading_rules_context', lambda *_: 'rules')
    assert agent_graph._load_decision_memory('cfg') == ([], 'memory', 'decisions', 'rules')
    daily.assert_not_called()
