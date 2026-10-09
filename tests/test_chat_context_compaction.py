from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.agent import chat_graph


def test_trim_chat_messages_uses_rolling_memory_and_recent_raw_messages():
    history = [
        HumanMessage(content="old user requirement"),
        AIMessage(content="old assistant answer"),
        HumanMessage(content="recent user question"),
        AIMessage(content="recent assistant answer"),
    ]

    messages = chat_graph._trim_chat_messages(
        "live system context",
        history,
        conversation_summary="- Preserve the old user requirement",
        summary_cursor=2,
    )

    assert "Rolling conversation memory" in messages[0].content
    assert "Preserve the old user requirement" in messages[0].content
    contents = [message.content for message in messages[1:]]
    assert "recent user question" in contents
    assert "recent assistant answer" in contents
    assert "old user requirement" not in contents


def test_context_compaction_summarizes_old_messages_in_batches(monkeypatch):
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 2)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_COMPACTION_BATCH", 2)
    history = [HumanMessage(content=f"turn {index}") for index in range(5)]
    captured = {}

    class FakeLlm:
        def invoke(self, messages):
            captured["prompt"] = messages[0].content
            return AIMessage(content="- User needs a long-running market review")

    monkeypatch.setattr(chat_graph, "build_chat_model", lambda **_kwargs: FakeLlm())
    monkeypatch.setattr(chat_graph, "invoke_with_retry", lambda operation, **_kwargs: operation())

    summary, cursor = chat_graph._compact_chat_context(
        history,
        "",
        0,
        {"model": "test-model", "api_key": "test-key"},
        {"thread_id": "session-a"},
    )

    assert cursor == 3
    assert summary == "- User needs a long-running market review"
    assert "turn 0" in captured["prompt"]
    assert "turn 3" not in captured["prompt"]


def test_context_compaction_starts_early_for_very_long_messages(monkeypatch):
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 10)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MAX_CHARS", 100)
    history = [HumanMessage(content="x" * 80) for _ in range(3)]

    assert chat_graph._context_compaction_cutoff(history, 0) == 1


def test_compaction_preserves_long_source_previous_memory_and_generated_summary(monkeypatch):
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 2)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_COMPACTION_BATCH", 2)
    long_turn = "用户最初要求：" + "事实、数字与证据。" * 1200 + "用户末尾要求：不要截断。"
    previous = "旧记忆开头：" + "仍然有效的条件。" * 1600 + "旧记忆末尾。"
    generated = "新记忆开头：" + "完整结果与后续检查。" * 1600 + "新记忆末尾。"
    history = [HumanMessage(content=long_turn), AIMessage(content="Confirmed evidence"),
               HumanMessage(content="recent user question"), AIMessage(content="recent answer")]
    captured = {}

    class FakeLlm:
        def invoke(self, messages):
            captured["prompt"] = messages[0].content
            return AIMessage(content=generated)

    monkeypatch.setattr(chat_graph, "build_chat_model", lambda **_kwargs: FakeLlm())
    monkeypatch.setattr(chat_graph, "invoke_with_retry", lambda operation, **_kwargs: operation())
    summary, cursor = chat_graph._compact_chat_context(
        history, previous, 0, {"model": "test-model"}, {"thread_id": "full-memory"}, force=True,
    )
    assert summary == generated and cursor == 2
    assert long_turn in captured["prompt"] and previous in captured["prompt"]
    assert "[truncated]" not in captured["prompt"]


@pytest.mark.parametrize("response", [
    AIMessage(content="partial memory", response_metadata={"finish_reason": "length"}),
    AIMessage(content="partial memory", response_metadata={"stop_reason": "max_tokens"}),
    AIMessage(content="partial memory", response_metadata={"status": "incomplete"}),
    AIMessage(content="partial memory", additional_kwargs={"finish_reason": "max_output_tokens"}),
    AIMessage(content="partial memory", additional_kwargs={"refusal": "Cannot summarize"}),
    AIMessage(content="", tool_calls=[{"name": "unknown", "args": {}, "id": "call1"}]),
    AIMessage(content=[{"type": "reasoning", "reasoning": "Thinking without final text"}]),
])
def test_incomplete_compaction_preserves_prior_memory_and_cursor(monkeypatch, response):
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 2)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_COMPACTION_BATCH", 2)
    history = [HumanMessage(content=f"turn {index}") for index in range(7)]

    class FakeLlm:
        def invoke(self, _messages):
            return response

    monkeypatch.setattr(chat_graph, "build_chat_model", lambda **_kwargs: FakeLlm())
    monkeypatch.setattr(chat_graph, "invoke_with_retry", lambda operation, **_kwargs: operation())
    summary, cursor = chat_graph._compact_chat_context(
        history, "verified prior memory", 1, {"model": "test-model"}, {"thread_id": "incomplete"},
    )
    assert summary == "verified prior memory" and cursor == 1


@pytest.mark.parametrize("prompt_role", ["system", "user"])
def test_failed_compaction_does_not_drop_long_latest_request_or_unsummarized_turns(monkeypatch, prompt_role):
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 2)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_COMPACTION_BATCH", 2)
    history = [HumanMessage(content="already summarized")]
    for index in range(10):
        history += [HumanMessage(content=f"Keep user constraint {index}"),
                    AIMessage(content=f"Keep conclusion {index}")]
    latest = "Latest request starts here: " + "Complete evidence. " * 10000 + " Latest request ends here."
    history.append(HumanMessage(content=latest))

    class FailedLlm:
        def invoke(self, _messages):
            raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(chat_graph, "build_chat_model", lambda **_kwargs: FailedLlm())
    monkeypatch.setattr(chat_graph, "invoke_with_retry", lambda operation, **_kwargs: operation())
    summary, cursor = chat_graph._compact_chat_context(
        history, "verified prior memory", 1, {"model": "test-model"}, {"thread_id": "failed-long"},
    )
    messages = chat_graph._trim_chat_messages(
        "live context", history, summary, cursor, system_prompt_role=prompt_role,
    )
    serialized = "\n".join(message.content for message in messages)
    assert "verified prior memory" in serialized
    assert "already summarized" not in serialized
    for index in range(10):
        assert f"Keep user constraint {index}" in serialized
        assert f"Keep conclusion {index}" in serialized
    assert latest in serialized


def test_conversation_memory_payload_reports_summary_coverage():
    payload = chat_graph.conversation_memory_payload(
        {
            "messages": [HumanMessage(content="one"), AIMessage(content="two"), HumanMessage(content="three")],
            "conversation_summary": "- Earlier conclusion",
            "conversation_summary_cursor": 2,
            "conversation_summary_updated_at": "2026-07-10T12:00:00+00:00",
        }
    )

    assert payload == {
        "summary": "- Earlier conclusion",
        "summarized_message_count": 2,
        "recent_message_count": 1,
        "total_message_count": 3,
        "updated_at": "2026-07-10T12:00:00+00:00",
    }
