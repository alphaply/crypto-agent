import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import chat as chat_api
from backend.app.core.deps import get_current_user


def test_post_stream_keeps_message_out_of_query_string(monkeypatch):
    captured = {}

    def fake_stream(session_id, **kwargs):
        captured.update({"session_id": session_id, **kwargs})
        yield {"type": "done", "messages": []}

    monkeypatch.setattr(chat_api, "stream_chat_events", fake_stream)
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test"}
    client = TestClient(app)

    response = client.post(
        "/api/chat/sessions/session-a/stream",
        json={"message": "private prompt", "retry": True, "replace_last": True},
    )

    assert response.status_code == 200
    payload = json.loads(response.text.removeprefix("data: ").strip())
    assert payload["type"] == "done"
    assert captured == {
        "session_id": "session-a",
        "user_input": "private prompt",
        "approval": None,
        "retry": True,
        "replace_last": True,
    }
