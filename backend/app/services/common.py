import os

import pytz
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

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
    return os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"


def get_symbol_specific_status(symbol: str):
    configs = [cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("symbol") == symbol]
    if not configs:
        return "Unknown", "N/A", False

    has_real = False
    has_strategy = False
    has_dca = False
    enabled = False

    for cfg in configs:
        if not cfg.get("enabled", True):
            continue
        enabled = True
        mode = str(cfg.get("mode", "STRATEGY")).upper()
        if mode == "REAL":
            has_real = True
        elif mode == "SPOT_DCA":
            has_dca = True
        else:
            has_strategy = True

    if not enabled:
        return "Disabled", "No active jobs", False

    status_parts = []
    freq_parts = []
    if has_real:
        status_parts.append("REAL")
        freq_parts.append("15m")
    if has_dca:
        status_parts.append("SPOT_DCA")
        freq_parts.append("Daily")
    if has_strategy:
        status_parts.append("STRATEGY")
        freq_parts.append("1h")

    status_text = " + ".join(status_parts)
    freq_text = " / ".join(freq_parts) if len(freq_parts) > 1 else (freq_parts[0] if freq_parts else "N/A")
    return status_text, freq_text, True


def serialize_message(msg):
    role = "assistant"
    if isinstance(msg, HumanMessage):
        role = "user"
    elif isinstance(msg, ToolMessage):
        role = "tool"
    elif isinstance(msg, SystemMessage):
        role = "system"

    payload = {"role": role, "content": msg.content}
    if isinstance(msg, AIMessage):
        payload["tool_calls"] = getattr(msg, "tool_calls", []) or []
        reasoning = msg.additional_kwargs.get("reasoning_content") or msg.response_metadata.get("reasoning_content") or ""
        if reasoning:
            payload["reasoning_content"] = reasoning
    return payload


def prompt_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "backend",
        "agent",
        "prompts",
    )
