from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.agent import chat_graph
from backend.app.services import chat_service


def test_restore_reads_history_and_approval_from_one_checkpoint(monkeypatch):
    calls = []
    messages = [HumanMessage(content="question"), AIMessage(content="saved answer")]
    approval = SimpleNamespace(id="approval-a", value={"tool_name": "place_order"})

    def read(config):
        calls.append(config)
        assert len(calls) == 1
        return SimpleNamespace(values={"messages": messages}, interrupts=[approval])

    monkeypatch.setattr(chat_graph, "chat_app", SimpleNamespace(get_state=read))
    monkeypatch.setattr(chat_service, "get_chat_session", lambda _: {
        "session_id": "chat-a", "config_id": "deleted-config", "session_type": "temporary",
        "runtime_json": '{"llm_provider_id":"deleted-provider"}',
    })
    monkeypatch.setattr(chat_service, "_resolve_temporary_runtime", lambda _: pytest.fail("History must not resolve live credentials"))
    monkeypatch.setattr(chat_service, "stream_chat", lambda *a, **kw: pytest.fail("Recovery must never start a model"))

    restored = chat_service.get_chat_session_payload("chat-a")

    assert [row["content"] for row in restored["messages"]] == ["question", "saved answer"]
    assert restored["pending_approval"] == {"id": "approval-a", "value": {"tool_name": "place_order"}}
    assert len(calls) == 1


def test_missing_session_does_not_restore_another_session(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_session", lambda _: None)
    monkeypatch.setattr(chat_service, "get_chat_snapshot", lambda *a, **kw: pytest.fail("Missing session must not read a checkpoint"))
    with pytest.raises(FileNotFoundError, match="Chat session not found"):
        chat_service.get_chat_session_payload("deleted")


def test_failed_checkpoint_read_is_not_reported_as_empty_history(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_session", lambda _: {"session_id": "a", "config_id": "cfg"})
    def fail(*args, **kwargs):
        raise RuntimeError("checkpoint unavailable")
    monkeypatch.setattr(chat_service, "get_chat_snapshot", fail)
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        chat_service.get_chat_session_payload("a")
