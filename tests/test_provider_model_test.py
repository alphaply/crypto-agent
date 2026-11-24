from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.config import router
from backend.app.core.deps import get_current_user
from backend.app.services import provider_model_service as svc


def _app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_provider_model_test_chat_success_and_secret_redaction(monkeypatch):
    fake_secret = "sk-test-secret-do-not-leak-123456"

    class FakeModel:
        def invoke(self, messages):
            assert len(messages) == 1
            return SimpleNamespace(
                content="OK",
                additional_kwargs={"reasoning_content": "checked prompt cleanly"},
                usage_metadata={"input_tokens": 11, "output_tokens": 2, "total_tokens": 13},
                response_metadata={},
            )

    monkeypatch.setattr(svc, "build_chat_model", lambda **kwargs: FakeModel())

    app = _app()
    client = TestClient(app)
    unauth = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://api.example.com/v1",
            "model": "gemini-4-neon",
            "credential": {"mode": "draft", "api_key": fake_secret},
        },
    )
    assert unauth.status_code == 401
    assert unauth.headers.get("Cache-Control") == "no-store"

    app.dependency_overrides[get_current_user] = lambda: {"role": "admin"}
    ok_resp = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://api.example.com/v1",
            "api_protocol": "chat",
            "compatibility_mode": "openai",
            "model": "gemini-4-neon",
            "thinking_enabled": True,
            "reasoning_effort": "medium",
            "prompt": "Reply with OK only.",
            "credential": {"mode": "draft", "api_key": fake_secret},
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.headers.get("Cache-Control") == "no-store"
    body = ok_resp.json()
    assert body["ok"] is True
    assert body["model"] == "gemini-4-neon"
    assert body["compatibility_mode"] == "openai"
    assert body["reply_preview"] == "OK"
    assert body["reasoning_preview"] == "checked prompt cleanly"
    assert body["usage"]["total_tokens"] == 13
    assert body["latency_ms"] >= 0

    class FailingModel:
        def invoke(self, messages):
            raise RuntimeError(f"401 Unauthorized using key={fake_secret}")

    monkeypatch.setattr(svc, "build_chat_model", lambda **kwargs: FailingModel())
    fail_resp = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://api.example.com/v1",
            "model": "gemini-4-neon",
            "credential": {"mode": "draft", "api_key": fake_secret},
        },
    )
    assert fail_resp.status_code == 200
    fail_body = fail_resp.json()
    assert fail_body["ok"] is False
    assert fake_secret not in fail_body["error"]
    assert "RuntimeError" in fail_body["error"]


def test_provider_model_test_decisions_and_saved_credential_validation(monkeypatch):
    from backend.utils import jev

    monkeypatch.setattr(
        svc,
        "load_effective_runtime_snapshot",
        lambda: {
            "llm_providers": [
                {
                    "provider_id": "jev-saved",
                    "api_base": "https://api.b.ai/v1",
                    "api_protocol": "decisions",
                    "decisions_api": "bai",
                    "compatibility_mode": "auto",
                    "model": "jev-1.13.0",
                    "api_key": "sk-saved-secret-7890",
                }
            ]
        },
    )
    monkeypatch.setattr(
        jev,
        "score_news",
        lambda item, provider, **kwargs: {"score": 87.5, "confidence": 0.91},
    )

    app = _app()
    app.dependency_overrides[get_current_user] = lambda: {"role": "admin"}
    client = TestClient(app)

    resp = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://api.b.ai/v1",
            "api_protocol": "decisions",
            "decisions_api": "bai",
            "model": "jev-1.13.0",
            "credential": {"mode": "saved", "provider_id": "jev-saved"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["compatibility_mode"] == "decisions"
    assert "score=87.5" in data["reply_preview"]

    changed = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://other.example.com/v1",
            "api_protocol": "decisions",
            "decisions_api": "bai",
            "model": "jev-1.13.0",
            "credential": {"mode": "saved", "provider_id": "jev-saved"},
        },
    )
    assert changed.status_code == 400
    assert changed.json()["detail"]["code"] == "connection_changed"

    missing = client.post(
        "/api/config/provider-models/test",
        json={
            "api_base": "https://api.example.com/v1",
            "model": "gemini-4-neon",
            "credential": {"mode": "none"},
        },
    )
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "missing_credentials"
