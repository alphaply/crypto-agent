from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

from backend.utils.prompts import PROMPT_MAP


def _bundled_prompt_directory(project_root: Path) -> Path:
    root = project_root.resolve()
    if root.name == "backend":
        return root / "agent" / "prompts"
    return root / "backend" / "agent" / "prompts"


def _prompt_directories(project_root: Path) -> list[Path]:
    directories = []
    if os_prompt_dir := __import__("os").getenv("PROMPT_DIR"):
        directories.append(Path(os_prompt_dir))
    if data_dir := __import__("os").getenv("DATA_DIR"):
        directories.append(Path(data_dir) / "prompts")
    directories.append(_bundled_prompt_directory(project_root))

    deduped = []
    seen = set()
    for directory in directories:
        try:
            resolved = directory.resolve(strict=False)
        except Exception:
            resolved = directory
        marker = str(resolved)
        if marker not in seen:
            seen.add(marker)
            deduped.append(resolved)
    return deduped


def resolve_prompt_path(prompt_file: str | None, project_root: Path) -> Path | None:
    raw_value = str(prompt_file or "").strip()
    if not raw_value:
        return None

    prompt_directories = _prompt_directories(project_root)
    raw_path = Path(raw_value)
    candidates: list[Path] = []

    if raw_path.is_absolute():
        candidates.append(raw_path)
        if raw_path.name:
            candidates.extend(directory / raw_path.name for directory in prompt_directories)
    else:
        candidates.append(project_root / raw_path)
        for directory in prompt_directories:
            candidates.append(directory / raw_path)
            candidates.append(directory / raw_path.name)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            resolved = candidate
        marker = str(resolved)
        if marker in seen:
            continue
        seen.add(marker)
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def normalize_prompt_reference(prompt_file: str | None, project_root: Path) -> str | None:
    raw_value = str(prompt_file or "").strip()
    if not raw_value:
        return None

    resolved = resolve_prompt_path(raw_value, project_root)
    if resolved is None:
        return raw_value

    for prompt_directory in _prompt_directories(project_root):
        try:
            if resolved.parent.resolve() == prompt_directory.resolve():
                return resolved.name
        except Exception:
            pass
    return str(resolved)


def resolve_prompt_template(
    agent_config: Dict[str, Any],
    trade_mode: str,
    project_root: Path,
    logger,
) -> str:
    prompt_file = agent_config.get("prompt_file")

    if isinstance(prompt_file, str) and prompt_file.strip():
        try:
            file_path = resolve_prompt_path(prompt_file, project_root)

            if file_path and file_path.exists():
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    logger.info(f"Using custom prompt file: {file_path}")
                    return content
                logger.warning(f"Prompt file is empty, fallback to default template: {file_path}")
            else:
                logger.warning(
                    f"Prompt file does not exist, fallback to default template: "
                    f"raw={prompt_file!r}, resolved={file_path}, project_root={project_root}"
                )
        except Exception as e:
            logger.warning(f"Failed to load prompt file, fallback to default template: raw={prompt_file!r}, error={e}")

    return PROMPT_MAP.get(trade_mode) or PROMPT_MAP.get("STRATEGY", "")


def render_prompt(template: str, **kwargs) -> str:
    return template.format_map(defaultdict(str, kwargs))


def resolve_prompt_file_content(prompt_file: str | None, project_root: Path, logger, fallback: str = "") -> str:
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        return fallback

    try:
        file_path = resolve_prompt_path(prompt_file, project_root)

        if file_path and file_path.exists():
            content = file_path.read_text(encoding="utf-8").strip()
            if content:
                return content
            logger.warning(f"Prompt file is empty, fallback to default template: {file_path}")
        else:
            logger.warning(
                f"Prompt file does not exist, fallback to default template: "
                f"raw={prompt_file!r}, resolved={file_path}, project_root={project_root}"
            )
    except Exception as e:
        logger.warning(f"Failed to load prompt file, fallback to default template: raw={prompt_file!r}, error={e}")
    return fallback
