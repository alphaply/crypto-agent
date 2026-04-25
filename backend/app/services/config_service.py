import json
import os
from datetime import datetime

from backend.config import config as global_config
from backend.config_store import (
    export_agent_configs,
    load_management_snapshot,
    runtime_options_payload,
    save_runtime_snapshot,
)
from backend.database import get_all_pricing, get_config_dependency_counts, purge_config_all_data

from backend.app.services.common import logger, prompt_dir


BLOCKED_PROMPT_FILES = set()


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
    return {
        "globals": snapshot["globals"],
        "agents": snapshot["agents"],
        "pricing": _pricing_items(),
        "prompts": {"files": _prompt_files()},
        "options": {**runtime_options_payload(), "prompt_files": _prompt_files()},
        "source": snapshot["source"],
    }


def save_config_payload(globals_payload: dict, agents_payload: list[dict]):
    save_runtime_snapshot(globals_payload, agents_payload)
    global_config.reload_config()
    return {"message": "Configuration saved."}


def export_config_payload():
    content = json.dumps(export_agent_configs(), indent=2, ensure_ascii=False)
    filename = f"crypto_configs_{datetime.now().strftime('%Y%m%d')}.json"
    return content, filename


def list_prompts_payload():
    return {"files": _prompt_files()}


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
    return {"message": "Prompt saved."}


def delete_prompt_payload(name: str):
    path = os.path.join(prompt_dir(), name)
    if os.path.exists(path):
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
    save_runtime_snapshot(snapshot["globals"], remaining)
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
