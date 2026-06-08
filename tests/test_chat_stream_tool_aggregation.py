from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk

from backend.agent import chat_graph


class CapturingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


def _message_from_chunks(chunks):
    accumulator = chat_graph._new_stream_accumulator()
    for chunk in chunks:
        accumulator["content"] += chat_graph._chunk_to_text(chunk)
        accumulator["reasoning_content"] += chat_graph._chunk_reasoning_text(chunk)
        chat_graph._accumulate_tool_call_chunks(accumulator, chunk)
        chat_graph._capture_stream_metadata(accumulator, chunk)
    return chat_graph._stream_accumulator_to_message(accumulator, logger=CapturingLogger())


def test_stream_accumulator_returns_plain_text_message():
    message = _message_from_chunks(
        [
            AIMessageChunk(content="hello "),
            AIMessageChunk(content="world"),
        ]
    )

    assert isinstance(message, AIMessage)
    assert message.content == "hello world"
    assert message.tool_calls == []


def test_stream_accumulator_merges_tool_call_chunks_by_index():
    message = _message_from_chunks(
        [
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"index": 0, "id": "call-1", "name": "open_position_strategy", "args": "{"}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"index": 0, "id": None, "name": None, "args": '"orders": []}'}
                ],
            ),
        ]
    )

    assert message.content == ""
    assert message.tool_calls == [
        {
            "name": "open_position_strategy",
            "args": {"orders": []},
            "id": "call-1",
            "type": "tool_call",
        }
    ]


def test_stream_accumulator_accepts_object_tool_call_chunks_with_empty_followup_fields():
    chunk = SimpleNamespace(
        content="",
        tool_call_chunks=[
            SimpleNamespace(index=0, id="call-1", name="cancel_orders_real", args="{"),
            SimpleNamespace(index=0, id=None, name=None, args='"order_id": "abc", "reason": "stale"}'),
        ],
        response_metadata={},
        usage_metadata=None,
        id=None,
    )

    message = _message_from_chunks([chunk])

    assert message.tool_calls[0]["name"] == "cancel_orders_real"
    assert message.tool_calls[0]["id"] == "call-1"
    assert message.tool_calls[0]["args"] == {"order_id": "abc", "reason": "stale"}


def test_stream_accumulator_invalid_tool_args_returns_assistant_error_message():
    logger = CapturingLogger()
    accumulator = chat_graph._new_stream_accumulator()
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"index": 0, "id": "call-1", "name": "open_position_strategy", "args": "not-json"}
        ],
    )

    chat_graph._accumulate_tool_call_chunks(accumulator, chunk)
    message = chat_graph._stream_accumulator_to_message(accumulator, logger=logger)

    assert message.tool_calls == []
    assert "工具调用" in message.content
    assert logger.warnings


def test_extract_tool_calls_ignores_empty_partial_chunks_and_omits_args():
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"index": 0, "id": None, "name": None, "args": '{"orders"'},
            {"index": 1, "id": "call-2", "name": "close_position_strategy", "args": "{"},
        ],
    )

    assert chat_graph._extract_tool_calls(chunk) == [
        {"index": 1, "id": "call-2", "name": "close_position_strategy"}
    ]
