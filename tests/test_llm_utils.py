import os
import unittest
from unittest.mock import patch

from backend.utils.llm_utils import sync_langsmith_environment


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


if __name__ == "__main__":
    unittest.main()