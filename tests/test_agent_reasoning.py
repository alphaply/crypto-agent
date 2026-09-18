from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from backend.agent.agent_graph import (
    _collect_agent_reasoning,
    _collect_agent_reasoning_token_count,
    _stream_agent_response,
    _stream_agent_turn,
    agent_node,
    finalize_node,
    summarize_content,
)
from backend.agent.agent_models import AgentState
from backend.utils.llm_utils import extract_reasoning_content


class _StreamingModel:
    def stream(self, messages, config=None):
        assert messages
        assert config == {"metadata": {"source": "test"}, "tags": ["agent"]}
        yield AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "inspect trend; "},
        )
        yield AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "control risk"},
            tool_call_chunks=[
                {
                    "id": "call-1",
                    "name": "market_tool",
                    "args": '{"symbol":"BTCUSDT"}',
                    "index": 0,
                }
            ],
        )


class _ToolStreamWithoutReasoning:
    def __init__(self):
        self.calls = 0

    def stream(self, messages, config=None):
        self.calls += 1
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "id": "call-2",
                    "name": "trade_tool",
                    "args": "{}",
                    "index": 0,
                }
            ],
        )


class _BindableModel:
    def __init__(self):
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return "tool-model"


class _SummaryModel:
    def invoke(self, messages):
        assert messages
        return AIMessage(content="summary")


def test_stream_agent_response_preserves_reasoning_tools_and_progress():
    progress = []
    response = _stream_agent_response(
        _StreamingModel(),
        [HumanMessage(content="analyze")],
        configurable={"progress_callback": progress.append},
        run_config={"metadata": {"source": "test"}, "tags": ["agent"]},
    )

    assert extract_reasoning_content(response) == "inspect trend; control risk"
    assert response.tool_calls == [
        {
            "name": "market_tool",
            "args": {"symbol": "BTCUSDT"},
            "id": "call-1",
            "type": "tool_call",
        }
    ]
    assert progress
    assert progress[-1]["phase"] == "thinking"
    assert "inspect trend" in progress[-1]["reasoning_content"]


def test_stream_agent_turn_does_not_replay_when_gateway_omits_reasoning():
    model = _ToolStreamWithoutReasoning()
    response = _stream_agent_turn(
        model,
        [HumanMessage(content="analyze")],
        configurable={},
        run_config={},
    )

    assert extract_reasoning_content(response) == ""
    assert model.calls == 1
    assert response.content == ""
    assert response.tool_calls[0]["name"] == "trade_tool"


def test_agent_node_builds_reasoning_model_before_binding_tools():
    model = _BindableModel()
    state = AgentState(
        symbol="BTC/USDT",
        messages=[HumanMessage(content="analyze")],
        market_context={},
        account_context={},
        history_context=[],
    )
    config = {
        "configurable": {
            "config_id": "cfg-test",
            "agent_config": {
                "model": "test-model",
                "api_key": "test-key",
                "api_base": "https://example.test/v1",
                "mode": "STRATEGY",
                "thinking_enabled": True,
                "reasoning_effort": "high",
            },
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", return_value=model), patch(
        "backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]
    ), patch(
        "backend.agent.agent_graph._stream_agent_turn",
        return_value=AIMessage(content="done", additional_kwargs={"reasoning_content": "analysis"}),
    ) as stream_turn:
        result = agent_node(state, config)

    assert model.bound_tools == []
    assert result.messages[-1].content == "done"
    assert stream_turn.call_args.args == ("tool-model", state.messages)


def test_summarize_content_invokes_the_model_it_builds():
    config = {
        "config_id": "cfg-test",
        "symbol": "BTC/USDT",
        "model": "test-model",
        "summarizer": {
            "model": "summary-model",
            "strategy_prompt": "Summarize: {content}",
        },
    }

    with patch("backend.agent.agent_graph.build_chat_model", return_value=_SummaryModel()):
        assert summarize_content("market analysis", config) == "summary"


def test_finalize_node_updates_memory_after_strategy_summary():
    state = AgentState(
        symbol="BTC/USDT",
        messages=[AIMessage(content="hold position")],
        market_context={},
        account_context={},
        history_context=[],
    )
    config = {
        "configurable": {
            "config_id": "cfg-test",
            "agent_config": {"model": "test-model", "mode": "STRATEGY"},
        }
    }

    with patch("backend.agent.agent_graph.summarize_content", return_value="summary") as summarize, patch(
        "backend.agent.agent_graph.database.save_summary"
    ), patch("backend.agent.agent_graph.update_turn_memory") as update_memory:
        finalize_node(state, config)

    summarize.assert_called_once_with("hold position", config["configurable"]["agent_config"])
    update_memory.assert_called_once_with("cfg-test", config["configurable"]["agent_config"], "summary", state.messages)


def test_collect_agent_reasoning_preserves_tool_call_stages():
    messages = [
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "check the account first"},
            tool_calls=[{"id": "call-1", "name": "get_account", "args": {}}],
        ),
        ToolMessage(tool_call_id="call-1", content="ok"),
        AIMessage(content="done", additional_kwargs={"reasoning_content": "account is healthy"}),
    ]

    reasoning = _collect_agent_reasoning(messages)

    assert "推理阶段 1 · 调用 get_account" in reasoning
    assert "check the account first" in reasoning
    assert "推理阶段 2" in reasoning
    assert "account is healthy" in reasoning


def test_collect_agent_reasoning_reports_hidden_reasoning_usage():
    message = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 8,
            "total_tokens": 11,
            "output_token_details": {"reasoning": 5},
        },
    )

    reasoning = _collect_agent_reasoning([message])

    assert _collect_agent_reasoning_token_count([message]) == 5
    assert "5 个推理 token" in reasoning
    assert "没有返回可展示的思考摘要" in reasoning


def test_empty_and_exhausted_responses_are_not_successful():
    import pytest
    from backend.utils.llm_utils import LLMInvocationError
    for chunks in [[], [AIMessageChunk(content="")], [AIMessageChunk(content="partial", response_metadata={"finish_reason": "length"})]]:
        class Model:
            def stream(self, *args, **kwargs):
                yield from chunks
        with pytest.raises(LLMInvocationError):
            _stream_agent_response(Model(), [HumanMessage(content="analyze")], configurable={})


def test_failure_is_saved_without_summary_request_or_success_progress():
    import pytest
    progress = []
    state = AgentState(symbol="BTC/USDT", messages=[AIMessage(content="Error: disconnected", additional_kwargs={"invocation_failed": True})], market_context={}, account_context={}, history_context=[])
    with patch("backend.agent.agent_graph.database.save_summary") as save, patch("backend.agent.agent_graph.summarize_content") as summarize, patch("backend.agent.agent_graph.update_turn_memory") as memory:
        with pytest.raises(RuntimeError, match="disconnected"):
            finalize_node(state, {"configurable": {"progress_callback": progress.append}})
    assert "disconnected" in save.call_args.args[2]
    summarize.assert_not_called()
    memory.assert_not_called()
    assert progress[-1]["phase"] == "failed"


def test_agent_retries_partial_disconnection_without_replaying_tools():
    import httpx
    class FlakyModel(_BindableModel):
        calls = 0
        def bind_tools(self, tools):
            return self
        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(content="", tool_call_chunks=[{"id": "discard", "name": "trade_tool", "args": "{}", "index": 0}])
                raise httpx.RemoteProtocolError("peer closed connection")
            yield AIMessageChunk(content="recovered")
    model = FlakyModel()
    progress = []
    state = AgentState(symbol="BTC/USDT", messages=[HumanMessage(content="analyze")], market_context={}, account_context={}, history_context=[])
    with patch("backend.agent.agent_graph.refresh_decision_context", side_effect=lambda state, config: state), patch("backend.agent.agent_graph.build_chat_model", return_value=model), patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), patch("backend.agent.agent_graph.run_trade_tool") as trade, patch("backend.utils.llm_utils.get_llm_max_retries", return_value=2), patch("backend.utils.llm_utils.time.sleep"):
        result = agent_node(state, {"configurable": {"agent_config": {"model": "test"}, "progress_callback": progress.append}})
    assert model.calls == 2
    assert result.messages[-1].content == "recovered"
    assert not result.messages[-1].tool_calls
    trade.assert_not_called()
    assert any(event.get("retry_attempt") == 2 for event in progress)


def test_empty_message_retries_until_budget_exhausted():
    import pytest
    from unittest.mock import Mock
    from backend.utils.llm_utils import invoke_with_retry, LLMInvocationError
    class EmptyModel:
        calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            yield AIMessageChunk(content="")
    model = EmptyModel()
    with patch("backend.utils.llm_utils.get_llm_max_retries", return_value=2), patch("backend.utils.llm_utils.time.sleep") as sleep:
        with pytest.raises(LLMInvocationError) as caught:
            invoke_with_retry(lambda: _stream_agent_response(model, [], configurable={}), logger=Mock(), context="test")
    assert model.calls == 3
    assert caught.value.attempts == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_output_limit_does_not_retry_identical_request():
    import pytest
    from unittest.mock import Mock
    from backend.utils.llm_utils import invoke_with_retry, LLMInvocationError
    class LimitedModel:
        calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            yield AIMessageChunk(content="", response_metadata={"finish_reason": "length"})
    model = LimitedModel()
    with patch("backend.utils.llm_utils.get_llm_max_retries", return_value=2), patch("backend.utils.llm_utils.time.sleep") as sleep:
        with pytest.raises(LLMInvocationError):
            invoke_with_retry(lambda: _stream_agent_response(model, [], configurable={}), logger=Mock(), context="test")
    assert model.calls == 1
    sleep.assert_not_called()


def test_length_with_complete_tool_call_continues_and_saves_warning():
    import json
    from backend.agent.agent_graph import should_continue, tools_node
    args = {"orders": [{"action": "BUY_LIMIT", "amount": 0.3, "entry_price": 2476, "reason": "test", "stop_loss": 2462, "take_profit": 2506}]}
    class Model:
        calls = 0
        def bind_tools(self, tools):
            return self
        def stream(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                raw = json.dumps(args)
                yield AIMessageChunk(content="行情解析", tool_call_chunks=[{"id": "call-trace", "name": "open_position_real", "args": raw[:30], "index": 0}])
                yield AIMessageChunk(content="\n交易决策：当", tool_call_chunks=[{"id": None, "name": None, "args": raw[30:], "index": 0}], response_metadata={"finish_reason": "length"})
            else:
                yield AIMessageChunk(content="工具执行结果已确认", response_metadata={"finish_reason": "stop"})
    model = Model()
    state = AgentState(symbol="ETH/USDT", messages=[HumanMessage(content="analyze")], market_context={}, account_context={}, history_context=[])
    progress = []
    config = {"configurable": {"agent_config": {"mode": "REAL", "model": "test"}, "progress_callback": progress.append}}
    with patch("backend.agent.agent_graph.build_chat_model", return_value=model), patch("backend.agent.agent_graph.run_trade_tool", return_value="test execution result") as trade, patch("backend.agent.agent_graph.database.save_summary") as save, patch("backend.agent.agent_graph.summarize_content", return_value="summary"), patch("backend.agent.agent_graph.update_turn_memory"), patch("backend.utils.llm_utils.time.sleep") as sleep:
        state = agent_node(state, config)
        assert should_continue(state) == "tools"
        assert state.messages[-1].response_metadata['finish_reason'] == 'length'
        assert state.messages[-1].additional_kwargs['output_warning']
        assert model.calls == 1
        state = tools_node(state, config)
        state = agent_node(state, config)
        assert should_continue(state) == "finalize"
        finalize_node(state, config)
    trade.assert_called_once_with("open_position_real", args, "unknown", "ETH/USDT")
    sleep.assert_not_called()
    assert model.calls == 2
    assert "[输出提示]" in save.call_args.args[2]
    assert "工具执行结果已确认" in save.call_args.args[2]
    assert progress[-1]['phase'] == 'completed'


def test_length_rejects_auto_repaired_or_partially_complete_tool_batch():
    import pytest
    from backend.utils.llm_utils import LLMInvocationError
    for raw in ['{"amount":0.3', '{"reason":"unfinished', '{"amount":0.3} trailing']:
        for extra_complete_call in [False, True]:
            class Model:
                def stream(self, *args, **kwargs):
                    calls = [{"id": "broken", "name": "open_position_real", "args": raw, "index": 0}]
                    if extra_complete_call:
                        calls.insert(0, {"id": "complete", "name": "close_position_real", "args": '{}', "index": 1})
                    yield AIMessageChunk(content="partial", tool_call_chunks=calls, response_metadata={"finish_reason": "length"})
            with pytest.raises(LLMInvocationError):
                _stream_agent_response(Model(), [], configurable={})
