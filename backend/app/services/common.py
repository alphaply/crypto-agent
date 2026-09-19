import os
import shutil

import pytz
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from backend.utils.llm_utils import extract_message_text, extract_reasoning_content, extract_reasoning_token_count

from backend.config import config as global_config
from backend.database import DB_NAME
from backend.utils.logger import setup_logger


load_dotenv(dotenv_path=".env", override=True)
logger = setup_logger("FastAPI")
TZ_CN = pytz.timezone(getattr(global_config, "timezone", "Asia/Shanghai"))


def list_symbols() -> list[str]:
    seen = set()
    symbols = []
    for cfg in global_config.get_all_symbol_configs():
        symbol = cfg.get("symbol")
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def get_scheduler_status() -> bool:
    return bool(getattr(global_config, "enable_scheduler", True))


def get_symbol_specific_status(symbol: str):
    configs = [cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("symbol") == symbol]
    if not configs:
        return "Unknown", "N/A", False

    from datetime import datetime
    from backend.utils.run_schedule import schedule_preview
    active = [cfg for cfg in configs if cfg.get("enabled", True)]
    if not active:
        return "Disabled", "No active jobs", False
    modes = list(dict.fromkeys(str(cfg.get("mode", "STRATEGY")).upper() for cfg in active))
    frequencies = list(dict.fromkeys(schedule_preview(cfg, datetime.now(TZ_CN))["frequency"] for cfg in active))
    return " + ".join(modes), " / ".join(frequencies), True


def serialize_message(msg):
    role = "assistant"
    if isinstance(msg, HumanMessage):
        role = "user"
    elif isinstance(msg, ToolMessage):
        role = "tool"
    elif isinstance(msg, SystemMessage):
        role = "system"

    payload = {
        "id": str(getattr(msg, "id", "") or ""),
        "role": role,
        "content": extract_message_text(msg),
    }
    if isinstance(msg, AIMessage):
        payload["tool_calls"] = getattr(msg, "tool_calls", []) or []
        reasoning = extract_reasoning_content(msg)
        if reasoning:
            payload["reasoning_content"] = reasoning
        reasoning_tokens = extract_reasoning_token_count(msg)
        if reasoning_tokens:
            payload["reasoning_tokens"] = reasoning_tokens
            payload["reasoning_visible"] = bool(reasoning)
    return payload


def prompt_dir() -> str:
    configured = os.getenv("PROMPT_DIR")
    if configured:
        directory = configured
    elif os.getenv("DATA_DIR"):
        directory = os.path.join(os.getenv("DATA_DIR"), "prompts")
    else:
        directory = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "backend",
            "agent",
            "prompts",
        )

    is_new_dir = not os.path.isdir(directory)
    os.makedirs(directory, exist_ok=True)
    bundled = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "backend",
        "agent",
        "prompts",
    )
    # 只在目录首次创建时（新卷/新部署）才从镜像内复制默认 prompt 文件，
    # 避免用户删除后被反复恢复。
    if is_new_dir and os.path.isdir(bundled) and os.path.abspath(directory) != os.path.abspath(bundled):
        for filename in os.listdir(bundled):
            if not filename.endswith(".txt"):
                continue
            source = os.path.join(bundled, filename)
            target = os.path.join(directory, filename)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copyfile(source, target)
    return directory
