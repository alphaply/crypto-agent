import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.services import config_service
from backend.utils.prompt_utils import normalize_prompt_reference, resolve_prompt_file_content


class _Logger:
    def warning(self, *_args, **_kwargs):
        pass


class PromptResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        self.prompt_path = self.project_root / "backend" / "agent" / "prompts" / "custom.txt"
        self.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        self.prompt_path.write_text("custom prompt", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_prompt_file_content_accepts_nested_prompt_path(self):
        content = resolve_prompt_file_content(
            "backend\\agent\\prompts\\custom.txt",
            self.project_root,
            _Logger(),
            fallback="fallback",
        )

        self.assertEqual(content, "custom prompt")

    def test_resolve_prompt_file_content_accepts_backend_as_project_root(self):
        content = resolve_prompt_file_content(
            "custom.txt",
            self.project_root / "backend",
            _Logger(),
            fallback="fallback",
        )

        self.assertEqual(content, "custom prompt")

    def test_resolve_prompt_file_content_accepts_data_dir_prompt(self):
        data_prompt_dir = self.project_root / "data" / "prompts"
        data_prompt_dir.mkdir(parents=True, exist_ok=True)
        (data_prompt_dir / "server.txt").write_text("server prompt", encoding="utf-8")

        with patch.dict("os.environ", {"DATA_DIR": str(self.project_root / "data")}):
            content = resolve_prompt_file_content(
                "server.txt",
                self.project_root,
                _Logger(),
                fallback="fallback",
            )

        self.assertEqual(content, "server prompt")

    def test_normalize_prompt_reference_collapses_prompt_directory_path(self):
        normalized = normalize_prompt_reference(str(self.prompt_path), self.project_root)

        self.assertEqual(normalized, "custom.txt")

    def test_save_config_normalizes_prompt_reference_before_persisting(self):
        globals_payload = {"market_timeframes": ["15m", "1h"]}
        agent_payload = {
            "config_id": "cfg-a",
            "symbol": "BTC/USDT",
            "mode": "STRATEGY",
            "prompt_file": str(self.prompt_path),
            "summarizer": {},
        }

        with patch.object(config_service, "prompt_dir", return_value=str(self.prompt_path.parent)):
            with patch.object(config_service, "save_runtime_snapshot") as save_runtime_snapshot:
                with patch.object(config_service.global_config, "reload_config"):
                    config_service.save_config_payload(globals_payload, [agent_payload], [], [])

        saved_agents = save_runtime_snapshot.call_args.args[1]
        self.assertEqual(saved_agents[0]["prompt_file"], "custom.txt")


if __name__ == "__main__":
    unittest.main()
