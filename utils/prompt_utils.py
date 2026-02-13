from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

from prompts import PROMPT_MAP


def resolve_prompt_template(
    agent_config: Dict[str, Any],
    trade_mode: str,
    project_root: Path,
    logger,
) -> str:
    prompt_file = agent_config.get("prompt_file")

    if isinstance(prompt_file, str) and prompt_file.strip():
        try:
            file_path = Path(prompt_file.strip())
            if not file_path.is_absolute():
                file_path = project_root / file_path
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    logger.info(f"Using custom prompt file: {file_path}")
                    return content
                logger.warning(f"Prompt file is empty, fallback to default template: {file_path}")
            else:
                logger.warning(f"Prompt file does not exist, fallback to default template: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to load prompt file, fallback to default template: {e}")

    return PROMPT_MAP.get(trade_mode) or PROMPT_MAP.get("STRATEGY", "")


def render_prompt(template: str, **kwargs) -> str:
    return template.format_map(defaultdict(str, kwargs))
