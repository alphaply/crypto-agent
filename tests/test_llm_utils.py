import os
import unittest
from unittest.mock import patch

import httpx
from openai import PermissionDeniedError

from backend.utils.llm_utils import (
    build_chat_openai,
    classify_llm_error,
    format_llm_error_message,
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
                with patch("backend.utils.llm_utils.ChatOpenAI") as chat_openai:
                    build_chat_openai(
                        model="claude-sonnet-4.6",
                        api_key="unit-key",
                        base_url="https://api.b.ai/v1",
                    )

        self.assertEqual(
            chat_openai.call_args.kwargs["default_headers"],
            {"User-Agent": "crypto-agent/0.1.0"},
        )

    def test_other_providers_keep_sdk_default_headers(self):
        with patch("backend.utils.llm_utils.sync_langsmith_environment"):
            with patch("backend.utils.llm_utils.get_llm_timeout_seconds", return_value=120):
                with patch("backend.utils.llm_utils.ChatOpenAI") as chat_openai:
                    build_chat_openai(
                        model="gpt-test",
                        api_key="unit-key",
                        base_url="https://example.com/v1",
                    )

        self.assertNotIn("default_headers", chat_openai.call_args.kwargs)

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
