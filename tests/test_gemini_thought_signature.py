from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage, message_chunk_to_message

from backend.utils.llm_utils import DeepSeekChatOpenAI, ReasoningChatOpenAI, build_chat_model


def test_streaming_preserves_gemini_thought_signatures_across_multiple_tool_calls():
    llm = ReasoningChatOpenAI(model="gemini-4-neon", openai_api_key="test-key", base_url="https://api.example.com/v1")

    chunk1 = llm._convert_chunk_to_generation_chunk(
        {
            "model": "gemini-4-neon",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "get_orders", "arguments": '{"symbol":'},
                                "extra_content": {"google": {"thought_signature": "sig-call-0"}},
                            }
                        ],
                    },
                }
            ],
        },
        AIMessageChunk,
        {},
    )
    chunk2 = llm._convert_chunk_to_generation_chunk(
        {
            "model": "gemini-4-neon",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"ETH/USDT:USDT"}'},
                                "extra_content": {"google": {"thought_signature": "sig-call-0"}},
                            }
                        ]
                    },
                }
            ],
        },
        AIMessageChunk,
        {},
    )
    chunk3 = llm._convert_chunk_to_generation_chunk(
        {
            "model": "gemini-4-neon",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "edit_order", "arguments": '{"price": 2000}'},
                                "extra_content": {"google": {"thought_signature": "sig-call-1"}},
                            }
                        ]
                    },
                }
            ],
        },
        AIMessageChunk,
        {},
    )

    ai_msg = message_chunk_to_message(chunk1.message + chunk2.message + chunk3.message)
    follow_up = [
        HumanMessage(content="check and edit order"),
        ai_msg,
        ToolMessage(content="[]", tool_call_id="call_0"),
        ToolMessage(content="ok", tool_call_id="call_1"),
    ]

    for target_cls in (ReasoningChatOpenAI, DeepSeekChatOpenAI):
        target = target_cls(model="gemini-4-neon", openai_api_key="test-key", base_url="https://api.example.com/v1")
        payload = target._get_request_payload(follow_up)
        tool_calls = payload["messages"][1]["tool_calls"]
        assert len(tool_calls) == 2
        assert tool_calls[0]["extra_content"] == {"google": {"thought_signature": "sig-call-0"}}
        assert tool_calls[1]["extra_content"] == {"google": {"thought_signature": "sig-call-1"}}


def test_non_streaming_create_chat_result_preserves_both_signature_formats():
    llm = ReasoningChatOpenAI(model="gemini-3.8-flash", openai_api_key="test-key", base_url="https://api.example.com/v1")
    response = {
        "id": "chatcmpl-1",
        "model": "gemini-3.8-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "edit_order", "arguments": "{}"},
                            "extra_content": {"google": {"thought_signature": "sig-a"}},
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "cancel_order", "arguments": "{}"},
                            "thought_signature": "sig-b-flat",
                        },
                    ],
                },
            }
        ],
    }

    result = llm._create_chat_result(response)
    ai_msg = result.generations[0].message
    payload = llm._get_request_payload(
        [
            HumanMessage(content="go"),
            ai_msg,
            ToolMessage(content="ok", tool_call_id="call_a"),
            ToolMessage(content="ok", tool_call_id="call_b"),
        ]
    )
    tool_calls = payload["messages"][1]["tool_calls"]
    assert tool_calls[0]["extra_content"] == {"google": {"thought_signature": "sig-a"}}
    assert tool_calls[1]["thought_signature"] == "sig-b-flat"


def test_fallback_to_non_gemini_model_does_not_leak_thought_signature():
    gemini = ReasoningChatOpenAI(model="gemini-4-neon", openai_api_key="test-key", base_url="https://api.example.com/v1")
    result = gemini._create_chat_result(
        {
            "id": "chatcmpl-2",
            "model": "gemini-4-neon",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "extra_content": {"google": {"thought_signature": "msg-sig"}},
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "edit_order", "arguments": "{}"},
                                "extra_content": {"google": {"thought_signature": "sig-1"}},
                            }
                        ],
                    },
                }
            ],
        }
    )
    ai_msg = result.generations[0].message
    history = [HumanMessage(content="edit"), ai_msg, ToolMessage(content="ok", tool_call_id="call_1")]

    for fallback in (
        ReasoningChatOpenAI(model="gpt-5.6", openai_api_key="test-key", base_url="https://api.example.com/v1"),
        DeepSeekChatOpenAI(model="deepseek-v4-pro", openai_api_key="test-key", base_url="https://api.example.com/v1"),
    ):
        payload = fallback._get_request_payload(history)
        assistant_payload = payload["messages"][1]
        assert "extra_content" not in assistant_payload
        assert "thought_signature" not in assistant_payload
        assert "extra_content" not in assistant_payload["tool_calls"][0]
        assert "thought_signature" not in assistant_payload["tool_calls"][0]


def test_system_only_messages_convert_last_to_user_only_for_gemini():
    gemini = build_chat_model(
        model="gemini-4-neon",
        api_key="test-key",
        base_url="https://api.example.com/v1",
        compatibility_mode="deepseek",
    )
    openai_llm = build_chat_model(
        model="gpt-5.6",
        api_key="test-key",
        base_url="https://api.example.com/v1",
        compatibility_mode="openai",
    )
    deepseek_llm = build_chat_model(
        model="deepseek-v4-pro",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        compatibility_mode="deepseek",
    )

    system_only = [SystemMessage(content="Analyze ETH market and trade if appropriate.")]
    assert gemini._get_request_payload(system_only)["messages"][0]["role"] == "user"
    assert openai_llm._get_request_payload(system_only)["messages"][0]["role"] == "system"
    assert deepseek_llm._get_request_payload(system_only)["messages"][0]["role"] == "system"

    mixed = [SystemMessage(content="System rules"), HumanMessage(content="Trade ETH")]
    mixed_gemini = gemini._get_request_payload(mixed)["messages"]
    assert [m["role"] for m in mixed_gemini] == ["system", "user"]
