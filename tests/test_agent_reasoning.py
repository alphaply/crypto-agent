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
