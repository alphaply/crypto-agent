import json
import os
from datetime import datetime
from pathlib import Path

from backend.config import config as global_config
from backend.config_store import (
    export_agent_configs,
    export_full_snapshot,
    import_full_snapshot,
    load_management_snapshot,
    runtime_options_payload,
    save_runtime_snapshot,
)
from backend.database import get_all_pricing, get_config_dependency_counts, purge_config_all_data, update_model_pricing
from backend.utils.prompt_utils import normalize_prompt_reference, resolve_prompt_path

from backend.app.services.common import logger, prompt_dir


BLOCKED_PROMPT_FILES = set()
ALLOWED_MARKET_TIMEFRAMES = {"15m", "30m", "1h", "4h", "1d", "1w", "1M"}
PROMPT_REFERENCE_FIELDS = ("prompt_file",)
SUMMARIZER_PROMPT_FIELDS = ("strategy_prompt_file", "daily_prompt_file", "short_memory_prompt_file")


def _prompt_project_root() -> Path:
    return Path(prompt_dir()).resolve().parents[2]


def _normalize_prompt_field(raw_value, *, field_name: str, project_root: Path):
    normalized = normalize_prompt_reference(raw_value, project_root)
    if not normalized:
        return None
    if resolve_prompt_path(normalized, project_root) is None:
        raise ValueError(f"{field_name} prompt file not found: {normalized}")
    return normalized


def _normalize_agent_prompt_files(agent_payload: dict) -> dict:
    project_root = _prompt_project_root()
    payload = dict(agent_payload or {})
    config_id = str(payload.get("config_id") or "agent")

    for field_name in PROMPT_REFERENCE_FIELDS:
        payload[field_name] = _normalize_prompt_field(
            payload.get(field_name),
            field_name=f"agents[{config_id}].{field_name}",
            project_root=project_root,
        )

    summarizer = dict(payload.get("summarizer") or {})
    for field_name in SUMMARIZER_PROMPT_FIELDS:
        summarizer[field_name] = _normalize_prompt_field(
            summarizer.get(field_name),
            field_name=f"agents[{config_id}].summarizer.{field_name}",
            project_root=project_root,
        )
    payload["summarizer"] = summarizer
    return payload


def _validate_market_timeframes(raw_timeframes, *, field_name: str) -> None:
    if raw_timeframes in (None, []):
        return
    if not isinstance(raw_timeframes, list):
        raise ValueError(f"{field_name} must be a list")

    invalid_timeframes = [item for item in raw_timeframes if item not in ALLOWED_MARKET_TIMEFRAMES]
    if invalid_timeframes:
        allowed = ", ".join(["15m", "30m", "1h", "4h", "1d", "1w", "1M"])
        raise ValueError(f"Unsupported {field_name}: {', '.join(invalid_timeframes)}. Allowed: {allowed}")


def _pricing_items():
    pricing = get_all_pricing()
    items = []
    for model, row in pricing.items():
        items.append(
            {
                "model": model,
                "input_price_per_m": row.get("input_price_per_m", 0),
                "output_price_per_m": row.get("output_price_per_m", 0),
                "currency": row.get("currency", "USD"),
            }
        )
    items.sort(key=lambda item: item["model"])
    return items


def _prompt_files():
    directory = prompt_dir()
    os.makedirs(directory, exist_ok=True)
    files = [f for f in os.listdir(directory) if f.endswith(".txt") and f not in BLOCKED_PROMPT_FILES]
    files.sort()
    return files


def get_raw_config_payload():
    snapshot = load_management_snapshot()
    pricing = get_all_pricing()
    llm_providers = []
    for provider in snapshot.get("llm_providers", []):
        provider_copy = dict(provider)
        model_price = pricing.get(provider_copy.get("model") or "", {})
        provider_copy["input_price_per_m"] = model_price.get("input_price_per_m", provider_copy.get("input_price_per_m", 0))
        provider_copy["output_price_per_m"] = model_price.get("output_price_per_m", provider_copy.get("output_price_per_m", 0))
        provider_copy["pricing_currency"] = model_price.get("currency", provider_copy.get("pricing_currency", "USD"))
        llm_providers.append(provider_copy)
    return {
        "globals": snapshot["globals"],
        "agents": snapshot["agents"],
        "llm_providers": llm_providers,
        "exchange_profiles": snapshot.get("exchange_profiles", []),
        "pricing": _pricing_items(),
        "prompts": {"files": _prompt_files()},
        "options": {**runtime_options_payload(), "prompt_files": _prompt_files()},
        "source": snapshot["source"],
    }


def save_config_payload(
    globals_payload: dict,
    agents_payload: list[dict],
    llm_providers_payload: list[dict] | None = None,
    exchange_profiles_payload: list[dict] | None = None,
):
    _validate_market_timeframes(globals_payload.get("market_timeframes") or [], field_name="market_timeframes")
    normalized_agents_payload = []
    for agent_payload in agents_payload or []:
        config_id = str(agent_payload.get("config_id") or "agent")
        _validate_market_timeframes(
            agent_payload.get("market_timeframes"),
            field_name=f"agents[{config_id}].market_timeframes",
        )
        normalized_agents_payload.append(_normalize_agent_prompt_files(agent_payload))

    save_runtime_snapshot(
        globals_payload,
        normalized_agents_payload,
        llm_providers_payload or [],
        exchange_profiles_payload or [],
    )
    for provider in llm_providers_payload or []:
        model = str(provider.get("model") or "").strip()
        if not model:
            continue
        update_model_pricing(
            model,
            float(provider.get("input_price_per_m") or 0),
            float(provider.get("output_price_per_m") or 0),
            provider.get("pricing_currency") or "USD",
    )
    global_config.reload_config()
    return {
        "message": "Configuration saved.",
        "langsmith": {
            "tracing": bool(getattr(global_config, "langchain_tracing", False)),
            "project": getattr(global_config, "langchain_project", ""),
            "api_key_configured": bool(getattr(global_config, "langchain_api_key", "")),
        },
    }


def export_config_payload():
    content = json.dumps(export_agent_configs(), indent=2, ensure_ascii=False)
    filename = f"crypto_configs_{datetime.now().strftime('%Y%m%d')}.json"
    return content, filename


def full_export_payload(include_secrets: bool = True) -> tuple[str, str]:
    """构建包含所有配置（含 prompts 和 pricing）的完整导出包并序列化为 JSON。"""
    snapshot = export_full_snapshot(include_secrets=include_secrets)

    # 附加 prompts 内容
    directory = prompt_dir()
    prompts: dict[str, str] = {}
    if os.path.isdir(directory):
        for fname in os.listdir(directory):
            if not fname.endswith(".txt") or fname in BLOCKED_PROMPT_FILES:
                continue
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as f:
                    prompts[fname] = f.read()
            except Exception:
                pass
    snapshot["prompts"] = prompts

    # 附加 model_pricing
    snapshot["model_pricing"] = _pricing_items()

    content = json.dumps(snapshot, indent=2, ensure_ascii=False)
    filename = f"crypto_full_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return content, filename


def full_import_payload(data: dict, write_env: bool = False) -> dict:
    """导入完整配置包，包括 prompts 和 model_pricing。"""
    prompt_files: dict[str, str] = data.pop("prompts", None) or {}
    model_pricing: list[dict] = data.pop("model_pricing", None) or []
    result = import_full_snapshot(
        data=data,
        write_env=write_env,
        prompt_files=prompt_files,
        model_pricing=model_pricing,
    )
    return result


def list_prompts_payload():
    return {"files": _prompt_files(), "directory": prompt_dir()}


def read_prompt_payload(name: str):
    directory = prompt_dir()
    path = os.path.join(directory, name)
    with open(path, "r", encoding="utf-8") as file:
        return {"content": file.read()}


def save_prompt_payload(name: str, content: str):
    directory = prompt_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    global_config.reload_config()
    return {"message": "Prompt saved."}


def delete_prompt_payload(name: str):
    path = os.path.join(prompt_dir(), name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt file not found: {name}")
    os.remove(path)
    return {"message": "Prompt deleted."}


def get_config_dependencies_payload(config_id: str):
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError(f"Config not found: {config_id}")
    return {"config_id": config_id, "counts": get_config_dependency_counts(config_id)}


def delete_config_payload(config_id: str):
    snapshot = load_management_snapshot()
    target = None
    remaining = []
    for agent in snapshot["agents"]:
        if agent.get("config_id") == config_id:
            target = agent
        else:
            remaining.append(agent)

    if not target:
        raise FileNotFoundError(f"Config not found: {config_id}")

    dependencies_before = get_config_dependency_counts(config_id)
    cleanup_result = purge_config_all_data(config_id)
    save_runtime_snapshot(
        snapshot["globals"],
        remaining,
        snapshot.get("llm_providers", []),
        snapshot.get("exchange_profiles", []),
    )
    global_config.reload_config()
    return {
        "message": f"Deleted config {config_id} and cleaned linked runtime/history data.",
        "removed_config": {
            "config_id": target.get("config_id"),
            "symbol": target.get("symbol"),
            "mode": target.get("mode"),
        },
        "dependencies_before": dependencies_before,
        "cleanup": cleanup_result,
    }
