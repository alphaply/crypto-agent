import json
import os
import re
from datetime import datetime

from backend.config import config as global_config
from backend.database import get_config_dependency_counts, purge_config_all_data

from backend.app.services.common import logger, prompt_dir


BLOCKED_PROMPT_FILES = set()


def _write_symbol_configs_to_env(new_configs):
    with open(".env", "r", encoding="utf-8") as file:
        content = file.read()

    val = json.dumps(new_configs, ensure_ascii=False)
    pattern = re.compile(r"^SYMBOL_CONFIGS=.*?(?=\n\w+=|\n#|$)", re.MULTILINE | re.DOTALL)
    new_entry = f"SYMBOL_CONFIGS='{val}'"
    if pattern.search(content):
        content = pattern.sub(new_entry, content)
    else:
        content += f"\n{new_entry}\n"

    with open(".env", "w", encoding="utf-8") as file:
        file.write(content.strip() + "\n")


def get_raw_config_payload():
    return {
        "configs": global_config.get_all_symbol_configs(),
        "global": {
            "leverage": global_config.leverage,
            "enable_scheduler": os.getenv("ENABLE_SCHEDULER", "true").lower() == "true",
            "trading_mode": getattr(global_config, "trading_mode", "MIXED"),
        },
    }


def save_config_payload(new_configs: list[dict], global_settings: dict):
    with open(".env", "r", encoding="utf-8") as file:
        content = file.read()

    updates = {
        "SYMBOL_CONFIGS": json.dumps(new_configs, ensure_ascii=False),
        "LEVERAGE": str(global_settings.get("leverage", global_config.leverage)),
        "ENABLE_SCHEDULER": "true" if global_settings.get("enable_scheduler", True) else "false",
    }

    for key, value in updates.items():
        pattern = re.compile(rf"^{key}=.*?(?=\n\w+=|\n#|$)", re.MULTILINE | re.DOTALL)
        new_entry = f"{key}='{value}'"
        if pattern.search(content):
            content = pattern.sub(new_entry, content)
        else:
            content += f"\n{new_entry}\n"

    with open(".env", "w", encoding="utf-8") as file:
        file.write(content.strip() + "\n")

    global_config.reload_config()
    return {"message": "Configuration saved."}


def export_config_payload():
    content = json.dumps(global_config.get_all_symbol_configs(), indent=2, ensure_ascii=False)
    filename = f"crypto_configs_{datetime.now().strftime('%Y%m%d')}.json"
    return content, filename


def list_prompts_payload():
    directory = prompt_dir()
    os.makedirs(directory, exist_ok=True)
    files = [f for f in os.listdir(directory) if f.endswith(".txt") and f not in BLOCKED_PROMPT_FILES]
    files.sort()
    return {"files": files}


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
    configs = global_config.get_all_symbol_configs()
    target = None
    remaining = []
    for cfg in configs:
        if cfg.get("config_id") == config_id:
            target = cfg
        else:
            remaining.append(cfg)

    if not target:
        raise FileNotFoundError(f"Config not found: {config_id}")

    dependencies_before = get_config_dependency_counts(config_id)
    cleanup_result = purge_config_all_data(config_id)
    _write_symbol_configs_to_env(remaining)
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
