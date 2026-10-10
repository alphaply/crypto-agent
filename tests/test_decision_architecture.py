from backend.agent import decision_context
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend.agent import tool_registry
from backend.utils.decision_journal import decision_journal


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY', 'SPOT_DCA'])
def test_trading_agents_cannot_edit_rules_even_with_forged_calls(mode, monkeypatch):
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda *_: {'mode': mode})
    assert 'manage_trading_rules' not in {tool.name for tool in tool_registry.get_trade_tools_for_mode(mode)}
    assert 'not allowed' in tool_registry.run_trade_tool('manage_trading_rules', {'action': 'apply'}, 'cfg', 'BTC/USDT')


def test_journal_keeps_all_receipts_and_complete_decisions():
    decision = 'plan only ' * 1000 + 'DECISION_END'
    receipt = '{"status":"unknown","order_id":"pending-1","details":"' + '待核验' * 200 + 'RECEIPT_END"}'
    messages = [
        AIMessage(content='plan', tool_calls=[{'name': 'open_position_real', 'id': 'entry-1', 'args': {}}]),
        ToolMessage(tool_call_id='entry-1', content=receipt),
        AIMessage(content='checking protection', tool_calls=[{'name': 'close_position_real', 'id': 'exit-2', 'args': {}}]),
        ToolMessage(tool_call_id='exit-2', content='{"status":"pending","order_id":"pending-2"}'),
        AIMessage(content=decision),
    ]
    text = decision_journal(messages)
    assert 'unknown=2' in text[:100]
    assert 'pending-1' in text
    assert 'pending-2' in text
    assert receipt in text
    assert decision in text
    assert 'checking protection' in text
    assert '原文截取' not in text


def test_journal_preserves_full_fallback_when_model_has_no_text():
    fallback = '完整待核验条件。' * 1000 + 'FALLBACK_END'
    assert fallback in decision_journal([], fallback=fallback)


def test_default_context_does_not_read_daily_reports(monkeypatch):
    from backend import database
    daily = Mock(side_effect=AssertionError('Daily reviews are retired'))
    monkeypatch.setattr(database, 'get_daily_summaries', daily)
    monkeypatch.setattr(decision_context, 'format_short_memory_for_llm', lambda *_, **__: 'memory')
    monkeypatch.setattr(decision_context, 'format_recent_decisions', lambda *_: 'decisions')
    assert decision_context.load_decision_memory('cfg') == decision_context.DecisionMemory('memory', 'decisions')
    daily.assert_not_called()
