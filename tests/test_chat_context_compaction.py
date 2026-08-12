from types import SimpleNamespace

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
