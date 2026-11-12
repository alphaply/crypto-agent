import pytest
from langchain_core.messages import AIMessage

from backend.agent import chat_graph


@pytest.mark.parametrize('approved', [False, True])
def test_chat_only_announces_execution_after_approval(monkeypatch, approved):
    events, executed = [], []
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'symbol': 'ETH/USDT', 'mode': 'REAL'})
    monkeypatch.setattr(chat_graph, 'interrupt', lambda _: {'approved': approved})
    monkeypatch.setattr('backend.database.trade_calls.record_trade_call', lambda *a, **kw: None)
    monkeypatch.setattr('backend.agent.tool_registry.stops_tool_sequence', lambda *a: False)
    monkeypatch.setattr(chat_graph, '_emit_stream_event', lambda _, event: events.append(event))

    def run(*args, **kwargs):
        executed.append(args)
        assert events[-1] == {'type': 'tool_start', 'tool_call_id': 'call-a', 'tool': 'synthetic_read'}
        return '{"status":"completed"}'

    monkeypatch.setattr(chat_graph, '_run_tool', run)
    message = AIMessage(content='', tool_calls=[{'id': 'call-a', 'name': 'synthetic_read', 'args': {}}])
    result = chat_graph.tools_node({'messages': [message]}, {'configurable': {'config_id': 'offline'}})
    assert len(executed) == int(approved)
    assert len(events) == int(approved)
    assert result['messages'][0].name == 'synthetic_read'
    assert result['messages'][0].tool_call_id == 'call-a'


def test_prompt_explains_execution_without_forcing_a_trade():
    from backend.agent.decision_context import DecisionMemory, render_decision_prompt
    prompt = render_decision_prompt('Custom task for {symbol}', DecisionMemory('', ''), symbol='ETH/USDT')
    assert 'Custom task for ETH/USDT' in prompt
    assert '正文或代码块中写工具名、参数或 JSON 不会执行操作' in prompt
    assert '无需操作时直接说明等待原因，不必调用交易工具' in prompt
