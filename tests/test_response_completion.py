"""Provider completion status must gate any saved model-generated memory."""

import pytest
from langchain_core.messages import AIMessage

from backend.utils.llm_utils import require_complete_response


@pytest.mark.parametrize("container", ["response_metadata", "additional_kwargs"])
@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"},
    {"finish_reason": " MAX_OUTPUT_TOKENS "},
    {"stop_reason": "max_tokens"},
    {"status": "incomplete"},
    {"status": "failed"},
    {"status": "cancelled"},
    {"incomplete_details": {"reason": "max_output_tokens"}},
    {"finish_reason": "content_filter"},
])
def test_incomplete_provider_status_rejected_even_with_valid_json(container, metadata):
    response = AIMessage(content='{"summary":"Looks complete"}', **{container: metadata})
    with pytest.raises(ValueError, match="Memory response is incomplete"):
        require_complete_response(response, context="Memory")


@pytest.mark.parametrize("response", [
    AIMessage(content="", additional_kwargs={"refusal": "Unavailable"}),
    AIMessage(content=[{"type": "refusal", "refusal": "Unavailable"}]),
])
def test_refusals_rejected(response):
    with pytest.raises(ValueError, match="refused"):
        require_complete_response(response)


def test_malformed_tool_arguments_rejected():
    response = AIMessage(content="", invalid_tool_calls=[{
        "name": "manage_trading_rules", "args": '{"action":', "id": "bad", "error": "Incomplete JSON",
    }])
    with pytest.raises(ValueError, match="malformed"):
        require_complete_response(response)


@pytest.mark.parametrize("metadata", [
    {}, {"finish_reason": "stop"}, {"stop_reason": "end_turn"},
    {"finish_reason": "tool_calls"}, {"status": "completed", "incomplete_details": None},
])
def test_complete_status_or_legacy_absent_metadata_allowed(metadata):
    require_complete_response(AIMessage(content="Complete memory.", response_metadata=metadata))
