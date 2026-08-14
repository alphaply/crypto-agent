from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from backend.agent.agent_graph import (
    _collect_agent_reasoning,
    _collect_agent_reasoning_token_count,
    _stream_agent_response,
    _stream_agent_turn,
)
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
    def stream(self, messages, config=None):
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


class _ReasoningOnlyStream:
    def stream(self, messages, config=None):
        yield AIMessageChunk(
            content="discarded answer",
            additional_kwargs={"reasoning_content": "visible fallback analysis"},
        )


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


def test_stream_agent_turn_recovers_reasoning_omitted_by_tool_gateway():
    response = _stream_agent_turn(
        _ToolStreamWithoutReasoning(),
        _ReasoningOnlyStream(),
        [HumanMessage(content="analyze")],
        configurable={},
        run_config={},
        agent_config={"thinking_enabled": True, "reasoning_effort": "high"},
    )

    assert extract_reasoning_content(response) == "visible fallback analysis"
    assert response.content == ""
    assert response.tool_calls[0]["name"] == "trade_tool"


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
