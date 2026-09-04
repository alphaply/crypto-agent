from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.app.services import chat_service


def test_fork_chat_session_preserves_source_and_records_branch_metadata(monkeypatch):
    source_messages = [
        HumanMessage(content="first"),
        AIMessage(content="first answer"),
        HumanMessage(content="original question"),
        AIMessage(content="original answer"),
    ]
    source = {
        "session_id": "source",
        "config_id": "config-a",
        "symbol": "BTC/USDT",
        "title": "Research",
        "session_type": "task",
        "runtime_json": "{}",
        "root_session_id": None,
    }
    sessions = {"source": source}
    created = {}

    monkeypatch.setattr(chat_service, "get_chat_session", lambda session_id: sessions.get(session_id))
    monkeypatch.setattr(chat_service, "get_chat_state", lambda *args, **kwargs: {"messages": source_messages})
    monkeypatch.setattr(chat_service, "get_chat_sessions", lambda limit=500: list(sessions.values()))

    def fake_create(session_id, config_id, symbol, title, **kwargs):
        created.update({
            "session_id": session_id,
            "config_id": config_id,
            "symbol": symbol,
            "title": title,
            **kwargs,
        })
        sessions[session_id] = created.copy()

    monkeypatch.setattr(chat_service, "create_chat_session", fake_create)

    result = chat_service.fork_chat_session_payload("source", 2, "edited question")

    assert result["content"] == "edited question"
    assert created["parent_session_id"] == "source"
    assert created["root_session_id"] == "source"
    assert created["fork_message_index"] == 2
    assert [message.content for message in source_messages] == [
        "first",
        "first answer",
        "original question",
        "original answer",
    ]


def test_branch_seed_stops_before_edited_user_message(monkeypatch):
    parent = {
        "session_id": "parent",
        "config_id": "config-a",
        "session_type": "task",
    }
    branch = {
        "session_id": "branch",
        "config_id": "config-a",
        "parent_session_id": "parent",
        "fork_message_index": 2,
    }
    parent_messages = [
        HumanMessage(content="first"),
        AIMessage(content="first answer"),
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
    ]
    monkeypatch.setattr(chat_service, "get_chat_session", lambda session_id: parent if session_id == "parent" else None)
    monkeypatch.setattr(chat_service, "get_chat_state", lambda *args, **kwargs: {"messages": parent_messages})

    seed = chat_service._branch_seed_messages(branch)

    assert [message.content for message in seed] == ["first", "first answer"]


def test_serialized_messages_keep_checkpoint_indexes_across_tool_turns():
    messages = [
        HumanMessage(content="first"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "market_tool", "args": {}}],
        ),
        ToolMessage(content="tool result", tool_call_id="call-1"),
        AIMessage(content="answer"),
        HumanMessage(content="edit me"),
    ]

    payloads = chat_service._serialize_chat_messages(messages)

    assert payloads[-1]["message_index"] == 4
