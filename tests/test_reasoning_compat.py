from types import SimpleNamespace

from langchain_core.messages import AIMessage

from backend.agent.agent_graph import _collect_agent_reasoning
from backend.utils.llm_utils import extract_reasoning_content


def test_extracts_summary_blocks_with_boundaries():
    message = SimpleNamespace(
        content_blocks=[
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspect trend"}],
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Control risk"}],
            },
        ],
        additional_kwargs={},
        response_metadata={},
        reasoning_content=None,
        content=[],
    )

    assert extract_reasoning_content(message) == "Inspect trend\n\nControl risk"


def test_extracts_analysis_from_openai_compatible_gateway():
    message = AIMessage(content="Answer", additional_kwargs={"analysis": "Internal summary"})

    assert extract_reasoning_content(message) == "Internal summary"


def test_single_agent_reasoning_has_no_synthetic_stage_one():
    messages = [AIMessage(content="Answer", additional_kwargs={"reasoning_content": "Inspect trend"})]

    assert _collect_agent_reasoning(messages) == "Inspect trend"


def test_multi_turn_agent_reasoning_keeps_real_stage_boundaries():
    messages = [
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "Inspect trend"},
            tool_calls=[{"id": "call-1", "name": "market_tool", "args": {}}],
        ),
        AIMessage(content="Answer", additional_kwargs={"reasoning_content": "Control risk"}),
    ]

    reasoning = _collect_agent_reasoning(messages)
    assert "### 推理阶段 1 · 调用 market_tool\n\nInspect trend" in reasoning
    assert "### 推理阶段 2\n\nControl risk" in reasoning
