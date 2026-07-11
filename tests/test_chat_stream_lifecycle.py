import queue

from backend.agent import chat_graph
from backend.app.services import chat_service


def test_stream_error_before_completion_is_generation_failure():
    def run_callable():
        yield {"type": "token", "token": "partial"}
        raise RuntimeError("upstream closed")

    events = list(chat_graph._yield_stream_events(run_callable, queue.Queue(), "preparing"))

    assert events[-1]["type"] == "error"
    assert events[-1]["phase"] == "generation"
    assert events[-1]["partial_content"] is True


def test_stream_error_after_model_completion_is_persistence_failure():
    def run_callable():
        yield {"type": "token", "token": "complete"}
        yield {"type": "model_complete", "content": "complete", "reasoning_content": ""}
        raise RuntimeError("checkpoint failed")

    events = list(chat_graph._yield_stream_events(run_callable, queue.Queue(), "preparing"))

    assert events[-1]["type"] == "error"
    assert events[-1]["phase"] == "persistence"
    assert events[-1]["partial_content"] is True


def test_service_converts_post_completion_failure_to_unpersisted_done(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "get_chat_session",
        lambda _session_id: {"session_id": "session-a", "config_id": "cfg-a", "runtime_json": None},
    )
    monkeypatch.setattr(chat_service, "_effective_session_runtime", lambda _session: None)

    def fake_stream(*_args, **_kwargs):
        yield {"type": "token", "token": "answer"}
        yield {"type": "model_complete", "content": "answer", "reasoning_content": ""}
        yield {
            "type": "error",
            "message": "save failed",
            "phase": "persistence",
            "error_code": "persistence_error",
            "retryable": False,
            "partial_content": True,
        }

    monkeypatch.setattr(chat_service, "stream_chat", fake_stream)

    events = list(chat_service.stream_chat_events("session-a", user_input="hello"))

    assert [event["type"] for event in events] == ["token", "done"]
    assert events[-1]["persisted"] is False
    assert events[-1]["messages"] is None
    assert events[-1]["persistence_error"] == "save failed"


def test_terminal_finish_reason_is_explicit():
    accumulator = chat_graph._new_stream_accumulator()
    assert chat_graph._has_terminal_finish_reason(accumulator) is False
    accumulator["response_metadata"] = {"finish_reason": "stop"}
    assert chat_graph._has_terminal_finish_reason(accumulator) is True
