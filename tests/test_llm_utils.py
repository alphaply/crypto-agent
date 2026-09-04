import os
import unittest
from unittest.mock import patch

import httpx
from openai import PermissionDeniedError

from backend.utils.llm_utils import (
    DeepSeekChatOpenAI,
    ReasoningChatOpenAI,
    extract_message_text,
    extract_reasoning_content,
    build_chat_model,
    classify_llm_error,
    format_llm_error_message,
    resolve_compatibility_mode,
    sync_langsmith_environment,
)


class _DummyConfig:
    langchain_tracing = True
    langchain_project = "unit-project"
    langchain_api_key = "unit-key"


class _DisabledDummyConfig:
    langchain_tracing = False
    langchain_project = ""
    langchain_api_key = ""


class LangSmithEnvironmentTests(unittest.TestCase):
    def test_sync_langsmith_environment_sets_new_and_legacy_vars(self):
        with patch("backend.config.config", _DummyConfig()):
            with patch.dict(os.environ, {}, clear=False):
                snapshot = sync_langsmith_environment()

        self.assertEqual(snapshot["LANGSMITH_TRACING"], "true")
        self.assertEqual(snapshot["LANGCHAIN_TRACING_V2"], "true")
        self.assertEqual(snapshot["LANGSMITH_PROJECT"], "unit-project")
        self.assertEqual(snapshot["LANGCHAIN_PROJECT"], "unit-project")
        self.assertEqual(snapshot["LANGSMITH_API_KEY"], "unit-key")
        self.assertEqual(snapshot["LANGCHAIN_API_KEY"], "unit-key")

    def test_sync_langsmith_environment_clears_project_and_key_when_disabled(self):
        with patch("backend.config.config", _DisabledDummyConfig()):
            with patch.dict(
                os.environ,
                {
                    "LANGSMITH_PROJECT": "old-project",
                    "LANGCHAIN_PROJECT": "old-project",
                    "LANGSMITH_API_KEY": "old-key",
                    "LANGCHAIN_API_KEY": "old-key",
                },
                clear=False,
            ):
                snapshot = sync_langsmith_environment()

        self.assertEqual(snapshot["LANGSMITH_TRACING"], "false")
        self.assertEqual(snapshot["LANGCHAIN_TRACING_V2"], "false")
        self.assertEqual(snapshot["LANGSMITH_PROJECT"], "crypto-agent")
        self.assertEqual(snapshot["LANGCHAIN_PROJECT"], "crypto-agent")
        self.assertEqual(snapshot["LANGSMITH_API_KEY"], "")
        self.assertEqual(snapshot["LANGCHAIN_API_KEY"], "")


class ProviderCompatibilityTests(unittest.TestCase):
    def test_bai_uses_stable_application_user_agent(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(ReasoningChatOpenAI, "__init__", return_value=None) as chat_openai:
                    build_chat_model(
                        model="claude-sonnet-4.6",
                        api_key="unit-key",
                        base_url="https://api.b.ai/v1",
                    )

        self.assertEqual(
            chat_openai.call_args.kwargs["default_headers"],
            {"User-Agent": "crypto-agent/0.1.0"},
        )
        self.assertEqual(chat_openai.call_args.kwargs["base_url"], "https://api.b.ai/v1")

    def test_bankofai_domain_uses_stable_application_user_agent(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(ReasoningChatOpenAI, "__init__", return_value=None) as chat_openai:
                    build_chat_model(
                        model="claude-sonnet-5",
                        api_key="unit-key",
                        base_url="https://api.bankofai.io/v1",
                        compatibility_mode="openai",
                        reasoning_effort="high",
                    )

        kwargs = chat_openai.call_args.kwargs
        self.assertEqual(kwargs["default_headers"], {"User-Agent": "crypto-agent/0.1.0"})
        self.assertEqual(kwargs["reasoning_effort"], "high")

    def test_claude_5_uses_native_adaptive_thinking_with_visible_summary(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch("backend.utils.llm_utils.ChatAnthropic") as chat_anthropic:
                    build_chat_model(
                        model="claude-sonnet-5",
                        api_key="unit-key",
                        base_url="https://api.bankofai.io/v1",
                        compatibility_mode="anthropic",
                        thinking_enabled=True,
                        reasoning_effort="high",
                    )

        kwargs = chat_anthropic.call_args.kwargs
        self.assertEqual(kwargs["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(kwargs["output_config"], {"effort": "high"})
        self.assertNotIn("temperature", kwargs)

    def test_claude_45_maps_effort_to_manual_budget(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch("backend.utils.llm_utils.ChatAnthropic") as chat_anthropic:
                    build_chat_model(
                        model="claude-sonnet-4-5",
                        api_key="unit-key",
                        base_url="https://api.anthropic.com",
                        compatibility_mode="anthropic",
                        thinking_enabled=True,
                        reasoning_effort="medium",
                    )

        kwargs = chat_anthropic.call_args.kwargs
        self.assertEqual(
            kwargs["thinking"],
            {"type": "enabled", "budget_tokens": 4096, "display": "summarized"},
        )

    def test_standard_content_blocks_separate_reasoning_and_answer(self):
        from langchain_core.messages import AIMessage

        message = AIMessage(
            content=[
                {"type": "thinking", "thinking": "check risk", "signature": "sig"},
                {"type": "text", "text": "hold"},
            ],
            response_metadata={"model_provider": "anthropic"},
        )
        self.assertEqual(extract_reasoning_content(message), "check risk")
        self.assertEqual(extract_message_text(message), "hold")

    def test_openai_compatible_stream_preserves_reasoning_delta(self):
        from langchain_core.messages import AIMessageChunk

        model = ReasoningChatOpenAI.model_construct(output_version=None)
        generation = model._convert_chunk_to_generation_chunk(
            {
                "choices": [
                    {
                        "delta": {"role": "assistant", "content": "", "reasoning_content": "step"},
                        "finish_reason": None,
                    }
                ]
            },
            AIMessageChunk,
            None,
        )
        self.assertEqual(extract_reasoning_content(generation.message), "step")

    def test_reasoning_token_usage_is_available_without_visible_summary(self):
        from langchain_core.messages import AIMessage
        from backend.utils.llm_utils import extract_reasoning_token_count

        message = AIMessage(
            content="answer",
            usage_metadata={
                "input_tokens": 5,
                "output_tokens": 10,
                "total_tokens": 15,
                "output_token_details": {"reasoning": 7},
            },
        )
        self.assertEqual(extract_reasoning_content(message), "")
        self.assertEqual(extract_reasoning_token_count(message), 7)

    def test_explicit_deepseek_mode_handles_aliases_and_none_effort(self):
        self.assertEqual(
            resolve_compatibility_mode("deepseek", model="private-alias", base_url="https://gateway.example/v1"),
            "deepseek",
        )
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(DeepSeekChatOpenAI, "__init__", return_value=None) as deepseek_init:
                    build_chat_model(
                        model="private-alias",
                        api_key="unit-key",
                        base_url="https://gateway.example/v1",
                        compatibility_mode="deepseek",
                        thinking_enabled=True,
                        reasoning_effort="none",
                    )

        request_kwargs = deepseek_init.call_args.kwargs
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", request_kwargs)

    def test_other_providers_keep_sdk_default_headers(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(ReasoningChatOpenAI, "__init__", return_value=None) as chat_openai:
                    build_chat_model(
                        model="gpt-test",
                        api_key="unit-key",
                        base_url="https://example.com/v1",
                    )

        self.assertNotIn("default_headers", chat_openai.call_args.kwargs)

    def test_gemini_38_maps_unsupported_reasoning_efforts(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(ReasoningChatOpenAI, "__init__", return_value=None) as chat_openai:
                    build_chat_model(
                        model="gemini-3.8-flash",
                        api_key="unit-key",
                        base_url="https://api.b.ai/v1",
                        compatibility_mode="openai",
                        thinking_enabled=True,
                        reasoning_effort="max",
                    )

        self.assertEqual(chat_openai.call_args.kwargs["reasoning_effort"], "high")

    def test_gemini_38_disabled_thinking_uses_lowest_supported_effort(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch.object(ReasoningChatOpenAI, "__init__", return_value=None) as chat_openai:
                    build_chat_model(
                        model="gemini-3.8-flash",
                        api_key="unit-key",
                        base_url="https://api.b.ai/v1",
                        compatibility_mode="openai",
                        thinking_enabled=False,
                    )

        self.assertEqual(chat_openai.call_args.kwargs["reasoning_effort"], "low")

    def test_http_403_is_reported_as_permission_error(self):
        request = httpx.Request("POST", "https://api.b.ai/v1/chat/completions")
        response = httpx.Response(403, request=request)
        error = PermissionDeniedError(
            "Your request was blocked.",
            response=response,
            body={"error": {"message": "Your request was blocked."}},
        )

        self.assertEqual(classify_llm_error(error), "permission_error")
        self.assertIn("HTTP 403", format_llm_error_message("permission_error"))
        self.assertIn("WAF", format_llm_error_message("permission_error"))


if __name__ == "__main__":
    unittest.main()
