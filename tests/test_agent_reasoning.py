from langchain_core.messages import AIMessage, ToolMessage

from backend.agent.agent_graph import _collect_agent_reasoning, _collect_agent_reasoning_token_count


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
