from langchain_core.messages import AIMessage

from backend.app.services.chat_service import _serialize_chat_messages


def test_legacy_model_signature_is_hidden_from_serialized_messages():
    messages = _serialize_chat_messages(
        [AIMessage(content="Analysis body\n\n---\n> **Some model** completed this response.")]
    )

    assert messages[0]["content"] == "Analysis body"
