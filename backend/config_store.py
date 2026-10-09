from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from backend.utils.logger import setup_logger
from backend.utils.run_schedule import DcaSchedule, validate_run_schedule
from backend.utils.spot_portfolio import SPOT_MARKET_TIMEFRAMES, normalize_spot_symbols
from backend.storage_paths import DATA_DIR, PROJECT_ROOT, data_file
from backend.app.schemas.news import NewsSettings, default_news_settings, default_pricing_sync_settings


logger = setup_logger("ConfigStore")
LAST_RUNTIME_CONFIG_ERROR: str | None = None

BASE_DIR = PROJECT_ROOT
DB_NAME = data_file("TRADING_DB_PATH", "trading_data.db")

DEFAULT_GLOBAL_SETTINGS: dict[str, Any] = {
    "news": default_news_settings(),
    "pricing_sync": default_pricing_sync_settings(),
    "polymarket": {"enabled": False, "events": [], "refresh_seconds": 300},
    "leverage": 20,
    "enable_scheduler": True,
    "trading_mode": "REAL",
    "langchain_tracing": False,
    "langchain_project": "crypto-agent",
    "llm_timeout_seconds": 120.0,
    "llm_max_retries": 2,
    "global_summarizer_model": "",
    "global_summarizer_api_base": "",
    "market_timeframes": ["15m", "1h", "4h", "1d", "1w"],
}

LANGSMITH_TRACING_ENV_KEYS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
LANGSMITH_PROJECT_ENV_KEYS = ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT")
LANGSMITH_API_KEY_ENV_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")

GLOBAL_SECRET_ENV_MAP = {
    "global_blockbeats_api_key": "BLOCKBEATS_API_KEY",
    "global_binance_api_key": "BINANCE_API_KEY",
    "global_binance_secret": "BINANCE_SECRET",
    "global_okx_api_key": "OKX_API_KEY",
    "global_okx_secret": "OKX_SECRET",
    "global_okx_passphrase": "OKX_PASSPHRASE",
    "langchain_api_key": "LANGSMITH_API_KEY",
    "global_summarizer_api_key": "GLOBAL_SUMMARIZER_API_KEY",
}

AGENT_SECRET_KEYS = {
    "api_key",
    "secret",
    "passphrase",
    "binance_api_key",
    "binance_secret",
    "okx_api_key",
    "okx_secret",
    "summarizer_api_key",
}

LLM_PROVIDER_SECRET_KEYS = {"api_key"}
PROVIDER_OPTION_KEYS = (
    "api_protocol", "models_dev_provider_id", "models_dev_model_id", "pricing_mode",
    "input_price_per_m", "output_price_per_m", "cache_read_price_per_m",
    "cache_write_price_per_m", "pricing_currency", "report_output_mode",
)

EXCHANGE_PROFILE_SECRET_KEYS = {"api_key", "secret", "passphrase"}

SECRET_FIELD_DB_KEY = {
    "summarizer_api_key": "summarizer.api_key",
}

SECRET_FIELD_API_KEY = {value: key for key, value in SECRET_FIELD_DB_KEY.items()}

TABLE_NAMES = {"app_settings", "agent_configs", "secret_store"}
PROVIDER_TABLE_NAMES = {"llm_providers", "exchange_profiles"}
MODE_SORT_ORDER = {
    "REAL": 0,
    "STRATEGY": 1,
    "SPOT_DCA": 2,
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value not in (None, ""):
            return value
    return None


def _mask_secret(value: str | None) -> dict[str, Any]:
    if not value:
        return {"configured": False, "masked_value": ""}
    if len(value) <= 6:
        masked = "*" * len(value)
    else:
        masked = f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
    return {"configured": True, "masked_value": masked}


def _ensure_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _ensure_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_ensure_jsonable(item) for item in value]
    return value


def _master_key_bytes() -> bytes:
    load_dotenv()
    raw_key = (
        os.getenv("CONFIG_MASTER_KEY")
        or os.getenv("JWT_SECRET")
        or os.getenv("ADMIN_PASSWORD")
        or "dev-config-master-key"
    ).strip()
    if len(raw_key) == 44 and raw_key.endswith("="):
        try:
            base64.urlsafe_b64decode(raw_key.encode("utf-8"))
            return raw_key.encode("utf-8")
        except Exception:
            pass
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_master_key_bytes())


def _encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def _split_agent_payload(agent: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    payload = deepcopy(agent)
    payload.pop("secrets", None)
    if payload.get("fallback_llm_provider_ids"):
        payload.pop("fallback_models", None)
    else:
        payload["fallback_models"] = [
            {key: value for key, value in item.items() if key not in {"api_key", "secrets", "_secrets"}}
            for item in payload.get("fallback_models", []) if isinstance(item, dict)
        ]
    summarizer = dict(payload.get("summarizer") or {})
    secret_values: dict[str, str] = {}

    for field in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret", "okx_api_key", "okx_secret"):
        raw = payload.pop(field, None)
        if raw not in (None, ""):
            secret_values[field] = str(raw)

    summary_api_key = summarizer.pop("api_key", None)
    if summary_api_key not in (None, ""):
        secret_values["summarizer_api_key"] = str(summary_api_key)

    if summarizer:
        payload["summarizer"] = summarizer
    elif "summarizer" in payload:
        payload["summarizer"] = {}

    return payload, secret_values


def _restore_agent_payload(agent: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    payload = deepcopy(agent)
    for field in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret", "okx_api_key", "okx_secret"):
        if secrets.get(field):
            payload[field] = secrets[field]

    if secrets.get("summarizer_api_key"):
        summarizer = dict(payload.get("summarizer") or {})
        summarizer["api_key"] = secrets["summarizer_api_key"]
        payload["summarizer"] = summarizer
    return payload


def _parse_secret_update(update: Any) -> tuple[str | None, str | None]:
    if update is None:
        return None, None
    if isinstance(update, dict):
        if update.get("clear"):
            return "clear", None
        value = update.get("value")
    else:
        value = update
    if value is None:
        return None, None
    text = str(value)
    if not text.strip():
        return None, None
    return "set", text


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_agent_config_sort_order(conn: sqlite3.Connection) -> None:
    if "agent_configs" not in _table_names(conn):
        return
    if "sort_order" in _column_names(conn, "agent_configs"):
        return
    conn.execute("ALTER TABLE agent_configs ADD COLUMN sort_order INTEGER")


def _ensure_provider_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_providers (
            provider_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT NOT NULL,
            api_base TEXT,
            temperature REAL,
            role TEXT NOT NULL DEFAULT 'agent',
            extra_body TEXT NOT NULL DEFAULT '{}',
            compatibility_mode TEXT NOT NULL DEFAULT 'auto',
            thinking_enabled INTEGER,
            reasoning_effort TEXT,
            system_prompt_role TEXT NOT NULL DEFAULT 'system',
            updated_at TEXT NOT NULL
        )
        """
    )
    if "compatibility_mode" not in _column_names(conn, "llm_providers"):
        conn.execute("ALTER TABLE llm_providers ADD COLUMN compatibility_mode TEXT NOT NULL DEFAULT 'auto'")
    if "thinking_enabled" not in _column_names(conn, "llm_providers"):
        conn.execute("ALTER TABLE llm_providers ADD COLUMN thinking_enabled INTEGER")
    if "reasoning_effort" not in _column_names(conn, "llm_providers"):
        conn.execute("ALTER TABLE llm_providers ADD COLUMN reasoning_effort TEXT")
    if "system_prompt_role" not in _column_names(conn, "llm_providers"):
        conn.execute("ALTER TABLE llm_providers ADD COLUMN system_prompt_role TEXT NOT NULL DEFAULT 'system'")
    if "settings_json" not in _column_names(conn, "llm_providers"):
        conn.execute("ALTER TABLE llm_providers ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_profiles (
            profile_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            market_type TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _has_runtime_storage(conn: sqlite3.Connection) -> bool:
    return TABLE_NAMES.issubset(_table_names(conn))


def _has_runtime_rows(conn: sqlite3.Connection) -> bool:
    if not _has_runtime_storage(conn):
        return False
    settings_count = conn.execute("SELECT COUNT(*) AS count FROM app_settings").fetchone()["count"]
    agent_count = conn.execute("SELECT COUNT(*) AS count FROM agent_configs").fetchone()["count"]
    secret_count = conn.execute("SELECT COUNT(*) AS count FROM secret_store").fetchone()["count"]
    return bool(settings_count or agent_count or secret_count)


def _load_env_symbol_configs() -> list[dict[str, Any]]:
    raw = os.getenv("SYMBOL_CONFIGS", "[]")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse SYMBOL_CONFIGS during migration: {exc}")
    return []


def normalize_agent_market_settings(agent: dict[str, Any]) -> dict[str, Any]:
    """Normalize portfolio fields at every config entry point, including imports."""
    payload = deepcopy(agent)
    mode = str(payload.get("mode") or "STRATEGY").upper()
    payload["mode"] = mode
    if payload.get("dca_schedule") is not None:
        payload["dca_schedule"] = DcaSchedule.model_validate(payload["dca_schedule"]).model_dump()
    if mode == "SPOT_DCA":
        symbols = normalize_spot_symbols(payload.get("symbols"), payload.get("symbol"))
        payload["symbols"] = symbols
        payload["symbol"] = symbols[0]
        payload["market_type"] = "spot"
        if not payload.get("market_timeframes"):
            payload["market_timeframes"] = list(SPOT_MARKET_TIMEFRAMES)
        if len(symbols) > 1 and any(float(payload.get(field) or 0) != 0 for field in ("initial_qty", "initial_cost")):
            raise ValueError("Multi-symbol spot tasks require initial_qty and initial_cost to be zero; legacy starting holdings apply to one symbol only")
    elif payload.get("symbols"):
        symbols = payload["symbols"]
        if not isinstance(symbols, list) or len(symbols) != 1:
            raise ValueError("Multiple symbols are supported only for SPOT_DCA tasks")
        if str(symbols[0]).strip().upper() != str(payload.get("symbol") or "").strip().upper():
            raise ValueError("Non-spot task symbols must match symbol")
        payload.pop("symbols", None)
    return payload


def _normalize_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, item in enumerate(agents):
        payload = normalize_agent_market_settings(item)
        raw_config_id = str(payload.get("config_id") or "").strip()
        if not raw_config_id:
            symbol_slug = str(payload.get("symbol") or "unknown").replace("/", "-").lower()
            model_slug = str(payload.get("model") or "default").split("-")[0]
            raw_config_id = f"{symbol_slug}-{model_slug}-{index}"
            payload["config_id"] = raw_config_id

        if raw_config_id in seen:
            raise ValueError(f"Duplicate config_id: {raw_config_id}")
        seen.add(raw_config_id)

        payload["config_id"] = raw_config_id
        payload["enabled"] = _normalize_bool(payload.get("enabled", True))
        payload["mode"] = str(payload.get("mode", "STRATEGY")).upper()
        payload['run_schedule'] = validate_run_schedule(payload.get('run_schedule'))
        payload.pop('futures_risk', None)
        fallback_pids = payload.get("fallback_llm_provider_ids") or []
        if isinstance(fallback_pids, list):
            payload["fallback_llm_provider_ids"] = [
                str(pid).strip() for pid in fallback_pids if str(pid).strip()
            ]
        else:
            payload["fallback_llm_provider_ids"] = []
        normalized.append(payload)

    return normalized


def _default_agent_sort_key(agent: dict[str, Any]) -> tuple[str, int, str]:
    symbol = str(agent.get("symbol") or "").upper()
    mode = str(agent.get("mode") or "STRATEGY").upper()
    config_id = str(agent.get("config_id") or "")
    return symbol, MODE_SORT_ORDER.get(mode, 99), config_id


def _upgrade_legacy_spot_profiles(agents: list[dict], profiles: list[dict]) -> tuple[list[dict], list[dict]]:
    """Give old single-spot tasks their own spot profile without changing futures users."""
    agents = deepcopy(agents)
    profiles = deepcopy(profiles)
    profile_map = {str(profile.get("profile_id")): profile for profile in profiles}
    for agent in agents:
        if str(agent.get("mode") or "").upper() != "SPOT_DCA" or agent.get("symbols"):
            continue
        profile = profile_map.get(str(agent.get("exchange_profile_id") or ""))
        if not profile or str(profile.get("market_type") or "swap").lower() == "spot":
            continue
        spot_id = _stable_id("exchange", [profile["profile_id"], "legacy-spot"])
        if spot_id not in profile_map:
            spot_profile = {**profile, "profile_id": spot_id, "name": f"{profile.get('name') or profile['profile_id']} (spot)", "market_type": "spot"}
            profiles.append(spot_profile)
            profile_map[spot_id] = spot_profile
        agent["exchange_profile_id"] = spot_id
        agent["market_type"] = "spot"
    return agents, profiles


def _slug(value: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or fallback))
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or fallback


def _stable_id(prefix: str, parts: list[Any]) -> str:
    raw = json.dumps([str(item or "") for item in parts], sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    label = _slug(next((str(item) for item in parts if item), prefix), prefix)[:28]
    return f"{prefix}-{label}-{digest}"


def _provider_payload(
    *,
    provider_id: str,
    name: str,
    model: str,
    api_base: str | None = "",
    temperature: float | None = None,
    role: str = "agent",
    extra_body: dict[str, Any] | None = None,
    compatibility_mode: str | None = "auto",
    thinking_enabled: bool | None = None,
    reasoning_effort: str | None = None,
    system_prompt_role: str | None = "system",
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "name": name or provider_id,
        "model": model or "",
        "api_base": api_base or "",
        "temperature": temperature,
        "role": role or "agent",
        "extra_body": extra_body or {},
        "compatibility_mode": str(compatibility_mode or "auto").lower(),
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": reasoning_effort or "",
        "system_prompt_role": "user" if str(system_prompt_role).lower() == "user" else "system",
    }


def _profile_payload(
    *,
    profile_id: str,
    name: str,
    exchange: str,
    market_type: str | None = "swap",
) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "name": name or profile_id,
        "exchange": (exchange or "binance").lower(),
        "market_type": market_type or "swap",
    }


def _derive_provider_from_agent(agent: dict[str, Any], role: str) -> dict[str, Any] | None:
    if role == "summarizer":
        summarizer = dict(agent.get("summarizer") or {})
        model = summarizer.get("model")
        api_base = summarizer.get("api_base")
        temperature = summarizer.get("temperature")
        api_key = summarizer.get("api_key") or agent.get("summarizer_api_key")
    else:
        model = agent.get("model")
        api_base = agent.get("api_base")
        temperature = agent.get("temperature")
        api_key = agent.get("api_key")

    if not any(value not in (None, "", {}) for value in (model, api_base, temperature, api_key)):
        return None

    provider_id = _stable_id("llm", [role, model, api_base, temperature, api_key])
    provider = _provider_payload(
        provider_id=provider_id,
        name=f"{model or 'LLM'} ({role})",
        model=str(model or ""),
        api_base=str(api_base or ""),
        temperature=temperature,
        role=role,
        extra_body=agent.get("extra_body") if role == "agent" and isinstance(agent.get("extra_body"), dict) else {},
        system_prompt_role=agent.get("system_prompt_role", "system") if role == "agent" else "system",
    )
    if api_key:
        provider["api_key"] = api_key
    return provider


def _derive_exchange_profile_from_agent(
    agent: dict[str, Any],
    globals_payload: dict[str, Any],
) -> dict[str, Any]:
    exchange = str(agent.get("exchange") or "binance").lower()
    market_type = str(agent.get("market_type") or "swap")
    if exchange == "okx":
        api_key = agent.get("okx_api_key") or agent.get("api_key") or globals_payload.get("global_okx_api_key")
        secret = agent.get("okx_secret") or agent.get("secret") or globals_payload.get("global_okx_secret")
        passphrase = agent.get("passphrase") or globals_payload.get("global_okx_passphrase")
    else:
        api_key = agent.get("binance_api_key") or agent.get("api_key") or globals_payload.get("global_binance_api_key")
        secret = agent.get("binance_secret") or agent.get("secret") or globals_payload.get("global_binance_secret")
        passphrase = agent.get("passphrase")

    profile_id = _stable_id("exchange", [exchange, market_type, api_key, secret, passphrase])
    profile = _profile_payload(
        profile_id=profile_id,
        name=f"{exchange.upper()} {market_type}",
        exchange=exchange,
        market_type=market_type,
    )
    if api_key:
        profile["api_key"] = api_key
    if secret:
        profile["secret"] = secret
    if passphrase:
        profile["passphrase"] = passphrase
    return profile


def _dedupe_by_id(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get(key) or "").strip()
        if not item_id:
            continue
        existing = deduped.get(item_id, {})
        merged = {**existing, **item}
        deduped[item_id] = merged
    return list(deduped.values())


def _derive_referenced_configs(
    globals_payload: dict[str, Any],
    agents: list[dict[str, Any]],
    llm_providers: list[dict[str, Any]] | None = None,
    exchange_profiles: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    providers = [deepcopy(item) for item in (llm_providers or [])]
    profiles = [deepcopy(item) for item in (exchange_profiles or [])]
    next_agents: list[dict[str, Any]] = []

    for agent in agents:
        next_agent = deepcopy(agent)
        if not next_agent.get("llm_provider_id"):
            provider = _derive_provider_from_agent(next_agent, "agent")
            if provider:
                providers.append(provider)
                next_agent["llm_provider_id"] = provider["provider_id"]
        if next_agent.get("fallback_models") and not next_agent.get("fallback_llm_provider_ids"):
            fb_pids = []
            for fb in next_agent.get("fallback_models", []):
                if isinstance(fb, dict) and fb.get("model"):
                    fb_prov_id = fb.get("provider_id") or _stable_id("llm", ["fallback", fb.get("model"), fb.get("api_base"), fb.get("temperature"), fb.get("api_key")])
                    fb_prov = _provider_payload(
                        provider_id=fb_prov_id,
                        name=fb.get("name") or f"{fb.get('model')} (fallback)",
                        model=str(fb.get("model") or ""),
                        api_base=str(fb.get("api_base") or ""),
                        temperature=fb.get("temperature"),
                        role="agent",
                        extra_body=fb.get("extra_body") if isinstance(fb.get("extra_body"), dict) else {},
                        compatibility_mode=fb.get("compatibility_mode", "auto"),
                        thinking_enabled=fb.get("thinking_enabled"),
                        reasoning_effort=fb.get("reasoning_effort", ""),
                        system_prompt_role=fb.get("system_prompt_role", "system"),
                    )
                    if fb.get("api_key"):
                        fb_prov["api_key"] = fb["api_key"]
                    providers.append(fb_prov)
                    fb_pids.append(fb_prov_id)
            if fb_pids:
                next_agent["fallback_llm_provider_ids"] = fb_pids
        # The resolved fallback list contains credentials. Persist/export only references.
        next_agent.pop("fallback_models", None)
        if not next_agent.get("summarizer_provider_id"):
            provider = _derive_provider_from_agent(next_agent, "summarizer")
            if provider:
                providers.append(provider)
                next_agent["summarizer_provider_id"] = provider["provider_id"]
        if not next_agent.get("exchange_profile_id"):
            profile = _derive_exchange_profile_from_agent(next_agent, globals_payload)
            profiles.append(profile)
            next_agent["exchange_profile_id"] = profile["profile_id"]
        next_agents.append(next_agent)

    return _dedupe_by_id(providers, "provider_id"), _dedupe_by_id(profiles, "profile_id"), next_agents


def _env_global_settings() -> dict[str, Any]:
    settings = deepcopy(DEFAULT_GLOBAL_SETTINGS)
    settings["leverage"] = int(os.getenv("LEVERAGE", settings["leverage"]))
    settings["enable_scheduler"] = _normalize_bool(os.getenv("ENABLE_SCHEDULER", settings["enable_scheduler"]))
    settings["trading_mode"] = os.getenv("TRADING_MODE", settings["trading_mode"])
    tracing_value = _first_env(*LANGSMITH_TRACING_ENV_KEYS)
    if tracing_value is not None:
        settings["langchain_tracing"] = _normalize_bool(tracing_value)
    project_value = _first_env(*LANGSMITH_PROJECT_ENV_KEYS)
    if project_value is not None:
        settings["langchain_project"] = project_value
    settings["llm_timeout_seconds"] = float(os.getenv("LLM_TIMEOUT_SECONDS", settings["llm_timeout_seconds"]))
    settings["llm_max_retries"] = int(os.getenv("LLM_MAX_RETRIES", settings["llm_max_retries"]))
    settings["global_summarizer_model"] = os.getenv("GLOBAL_SUMMARIZER_MODEL", settings["global_summarizer_model"])
    settings["global_summarizer_api_base"] = os.getenv(
        "GLOBAL_SUMMARIZER_API_BASE", settings["global_summarizer_api_base"]
    )
    raw_timeframes = os.getenv("MARKET_TIMEFRAMES")
    if raw_timeframes:
        settings["market_timeframes"] = [item.strip() for item in raw_timeframes.split(",") if item.strip()]
    return settings


def _env_global_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    for key, env_key in GLOBAL_SECRET_ENV_MAP.items():
        value = os.getenv(env_key)
        if value:
            secrets[key] = value
    langsmith_api_key = _first_env(*LANGSMITH_API_KEY_ENV_KEYS)
    if langsmith_api_key:
        secrets["langchain_api_key"] = langsmith_api_key
    return secrets


def _env_snapshot() -> dict[str, Any]:
    load_dotenv(override=False)
    return {
        **_env_global_settings(),
        **_env_global_secrets(),
        "agents": _normalize_agents(_load_env_symbol_configs()),
        "source": "env",
    }


def ensure_runtime_config_initialized() -> None:
    load_dotenv(override=False)
    if not DB_NAME.exists():
        return

    migrate_existing = False
    with _get_conn() as conn:
        if not _has_runtime_storage(conn):
            return
        _ensure_agent_config_sort_order(conn)
        _ensure_provider_tables(conn)
        if _has_runtime_rows(conn):
            provider_count = conn.execute("SELECT COUNT(*) AS count FROM llm_providers").fetchone()["count"]
            profile_count = conn.execute("SELECT COUNT(*) AS count FROM exchange_profiles").fetchone()["count"]
            migrate_existing = provider_count == 0 or profile_count == 0
        else:
            migrate_existing = None

    if migrate_existing:
        snapshot = load_runtime_snapshot()
        if snapshot:
            save_runtime_snapshot(snapshot, snapshot.get("agents", []))
        return
    if migrate_existing is False:
        return

    snapshot = _env_snapshot()
    save_runtime_snapshot(
        {key: snapshot[key] for key in snapshot if key not in {"agents", "source"}},
        snapshot["agents"],
    )
    logger.info("Runtime configuration migrated from .env into SQLite.")


def load_runtime_snapshot(*, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    global LAST_RUNTIME_CONFIG_ERROR
    LAST_RUNTIME_CONFIG_ERROR = None

    if connection is None and not DB_NAME.exists():
        return None

    # A save can read its own uncommitted candidate without opening a second
    # connection (which would see only the previous committed configuration).
    with (nullcontext(connection) if connection is not None else _get_conn()) as conn:
        if not _has_runtime_storage(conn) or not _has_runtime_rows(conn):
            return None
        _ensure_agent_config_sort_order(conn)
        _ensure_provider_tables(conn)

        settings = deepcopy(DEFAULT_GLOBAL_SETTINGS)
        setting_rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
        for row in setting_rows:
            settings[row["key"]] = json.loads(row["value_json"])

        secret_rows = conn.execute(
            """
            SELECT scope, scope_id, secret_key, encrypted_value
            FROM secret_store
            ORDER BY scope, scope_id, secret_key
            """
        ).fetchall()
        global_secrets: dict[str, str] = {}
        agent_secret_map: dict[str, dict[str, str]] = {}
        provider_secret_map: dict[str, dict[str, str]] = {}
        profile_secret_map: dict[str, dict[str, str]] = {}
        for row in secret_rows:
            field_name = SECRET_FIELD_API_KEY.get(row["secret_key"], row["secret_key"])
            try:
                decrypted = _decrypt_secret(row["encrypted_value"])
            except InvalidToken:
                LAST_RUNTIME_CONFIG_ERROR = (
                    "Runtime secrets cannot be decrypted with the current CONFIG_MASTER_KEY. "
                    "Restore the matching CONFIG_MASTER_KEY for this database or rotate saved secrets from setup/config."
                )
                logger.error(LAST_RUNTIME_CONFIG_ERROR)
                return None
            if row["scope"] == "global":
                global_secrets[field_name] = decrypted
            elif row["scope"] == "agent":
                agent_secret_map.setdefault(row["scope_id"], {})[field_name] = decrypted
            elif row["scope"] == "llm_provider":
                provider_secret_map.setdefault(row["scope_id"], {})[field_name] = decrypted
            elif row["scope"] == "exchange_profile":
                profile_secret_map.setdefault(row["scope_id"], {})[field_name] = decrypted

        provider_rows = conn.execute(
            """
            SELECT provider_id, name, model, api_base, temperature, role, extra_body, compatibility_mode, thinking_enabled, reasoning_effort, system_prompt_role, settings_json
            FROM llm_providers
            ORDER BY name ASC, provider_id ASC
            """
        ).fetchall()
        providers: list[dict[str, Any]] = []
        provider_map: dict[str, dict[str, Any]] = {}
        for row in provider_rows:
            provider = {
                "provider_id": row["provider_id"],
                "name": row["name"],
                "model": row["model"],
                "api_base": row["api_base"] or "",
                "temperature": row["temperature"],
                "role": row["role"] or "llm",
                "extra_body": json.loads(row["extra_body"] or "{}"),
                "compatibility_mode": row["compatibility_mode"] or "auto",
                "thinking_enabled": None if row["thinking_enabled"] is None else bool(row["thinking_enabled"]),
                "reasoning_effort": row["reasoning_effort"] or "",
                "system_prompt_role": "user" if row["system_prompt_role"] == "user" else "system",
            }
            options = json.loads(row["settings_json"] or "{}")
            provider.update({key: options[key] for key in PROVIDER_OPTION_KEYS if key in options})
            provider.update(provider_secret_map.get(row["provider_id"], {}))
            providers.append(provider)
            provider_map[row["provider_id"]] = provider

        profile_rows = conn.execute(
            """
            SELECT profile_id, name, exchange, market_type
            FROM exchange_profiles
            ORDER BY exchange ASC, name ASC, profile_id ASC
            """
        ).fetchall()
        profiles: list[dict[str, Any]] = []
        profile_map: dict[str, dict[str, Any]] = {}
        for row in profile_rows:
            profile = {
                "profile_id": row["profile_id"],
                "name": row["name"],
                "exchange": row["exchange"] or "binance",
                "market_type": row["market_type"] or "swap",
            }
            profile.update(profile_secret_map.get(row["profile_id"], {}))
            profiles.append(profile)
            profile_map[row["profile_id"]] = profile

        agent_rows = conn.execute(
            """
            SELECT config_id, data_json, sort_order
            FROM agent_configs
            ORDER BY
                CASE WHEN sort_order IS NULL THEN 1 ELSE 0 END ASC,
                sort_order ASC,
                symbol ASC,
                CASE UPPER(mode)
                    WHEN 'REAL' THEN 0
                    WHEN 'STRATEGY' THEN 1
                    WHEN 'SPOT_DCA' THEN 2
                    ELSE 99
                END ASC,
                config_id ASC
            """
        ).fetchall()

        agents: list[dict[str, Any]] = []
        has_explicit_order = False
        for row in agent_rows:
            data = json.loads(row["data_json"])
            if not isinstance(data, dict):
                continue
            if row["sort_order"] is not None:
                has_explicit_order = True
            payload = _restore_agent_payload(data, agent_secret_map.get(row["config_id"], {}))
            provider = provider_map.get(str(payload.get("llm_provider_id") or ""))
            if provider:
                payload["model"] = provider.get("model") or payload.get("model") or ""
                payload["api_base"] = provider.get("api_base") or payload.get("api_base") or ""
                payload["temperature"] = provider.get("temperature", payload.get("temperature"))
                if provider.get("extra_body"):
                    payload["extra_body"] = provider.get("extra_body")
                payload["compatibility_mode"] = provider.get("compatibility_mode") or "auto"
                if provider.get("thinking_enabled") is not None:
                    payload["thinking_enabled"] = provider.get("thinking_enabled")
                if provider.get("reasoning_effort"):
                    payload["reasoning_effort"] = provider.get("reasoning_effort")
                payload["report_output_mode"] = provider.get("report_output_mode", "json")
                payload["api_protocol"] = provider.get("api_protocol", "chat")
                if payload.get("system_prompt_role") not in {"system", "user"}:
                    payload["system_prompt_role"] = provider.get("system_prompt_role", "system")
                if provider.get("api_key"):
                    payload["api_key"] = provider.get("api_key")

            fallback_models = []
            for fb_pid in payload.get("fallback_llm_provider_ids") or []:
                fb_provider = provider_map.get(str(fb_pid).strip())
                if fb_provider:
                    fallback_models.append({
                        "provider_id": fb_provider.get("provider_id"),
                        "name": fb_provider.get("name") or fb_provider.get("model") or fb_pid,
                        "model": fb_provider.get("model") or "",
                        "api_base": fb_provider.get("api_base") or "",
                        "api_key": fb_provider.get("api_key") or "",
                        "api_protocol": fb_provider.get("api_protocol", "chat"),
                        "temperature": fb_provider.get("temperature", payload.get("temperature", 0.5)),
                        "extra_body": fb_provider.get("extra_body") or {},
                        "compatibility_mode": fb_provider.get("compatibility_mode") or "auto",
                        "thinking_enabled": fb_provider.get("thinking_enabled"),
                        "reasoning_effort": fb_provider.get("reasoning_effort") or "",
                        "system_prompt_role": fb_provider.get("system_prompt_role") if fb_provider.get("system_prompt_role") in {"system", "user"} else "system",
                    })
            if fallback_models:
                payload["fallback_models"] = fallback_models
            elif "fallback_models" not in payload:
                payload["fallback_models"] = []

            summary_provider = provider_map.get(str(payload.get("summarizer_provider_id") or ""))
            if summary_provider:
                summarizer = dict(payload.get("summarizer") or {})
                summarizer["model"] = summary_provider.get("model") or summarizer.get("model") or ""
                summarizer["api_base"] = summary_provider.get("api_base") or summarizer.get("api_base") or ""
                summarizer["temperature"] = summary_provider.get("temperature", summarizer.get("temperature"))
                if summary_provider.get("extra_body"):
                    summarizer["extra_body"] = summary_provider.get("extra_body")
                summarizer["compatibility_mode"] = summary_provider.get("compatibility_mode") or "auto"
                summarizer["report_output_mode"] = summary_provider.get("report_output_mode", "json")
                summarizer["api_protocol"] = summary_provider.get("api_protocol", "chat")
                if summarizer.get("system_prompt_role") not in {"system", "user"}:
                    summarizer["system_prompt_role"] = summary_provider.get("system_prompt_role", "system")
                if summary_provider.get("thinking_enabled") is not None:
                    summarizer["thinking_enabled"] = summary_provider.get("thinking_enabled")
                if summary_provider.get("reasoning_effort"):
                    summarizer["reasoning_effort"] = summary_provider.get("reasoning_effort")
                if summary_provider.get("api_key"):
                    summarizer["api_key"] = summary_provider.get("api_key")
                payload["summarizer"] = summarizer

            profile = profile_map.get(str(payload.get("exchange_profile_id") or ""))
            if profile:
                exchange = str(profile.get("exchange") or payload.get("exchange") or "binance").lower()
                payload["exchange"] = exchange
                payload["market_type"] = profile.get("market_type") or payload.get("market_type") or "swap"
                if profile.get("api_key"):
                    if not payload.get("api_key"):
                        payload["api_key"] = profile.get("api_key")
                    if exchange == "okx":
                        payload["okx_api_key"] = profile.get("api_key")
                    else:
                        payload["binance_api_key"] = profile.get("api_key")
                if profile.get("secret"):
                    if not payload.get("secret"):
                        payload["secret"] = profile.get("secret")
                    if exchange == "okx":
                        payload["okx_secret"] = profile.get("secret")
                    else:
                        payload["binance_secret"] = profile.get("secret")
                if profile.get("passphrase"):
                    payload["passphrase"] = profile.get("passphrase")
            agents.append(payload)

        if not has_explicit_order:
            agents.sort(key=_default_agent_sort_key)

        agents, profiles = _upgrade_legacy_spot_profiles(agents, profiles)

        return {
            **settings,
            **global_secrets,
            "agents": _normalize_agents(agents),
            "llm_providers": providers,
            "exchange_profiles": profiles,
            "source": "db",
        }


def load_effective_runtime_snapshot() -> dict[str, Any]:
    snapshot = load_runtime_snapshot()
    return snapshot or _env_snapshot()


def load_management_snapshot(*, include_secrets: bool = False) -> dict[str, Any]:
    snapshot = load_effective_runtime_snapshot()
    providers, profiles, agents_with_refs = _derive_referenced_configs(
        {**snapshot},
        snapshot.get("agents", []),
        snapshot.get("llm_providers") or [],
        snapshot.get("exchange_profiles") or [],
    )
    globals_payload = {
        key: snapshot.get(key, default)
        for key, default in DEFAULT_GLOBAL_SETTINGS.items()
    }
    globals_payload['news'] = NewsSettings.model_validate(globals_payload['news']).model_dump()
    def secret_view(value):
        metadata = _mask_secret(value)
        if include_secrets:
            metadata["value"] = value or ""
        return metadata

    globals_payload["secrets"] = {
        key: secret_view(snapshot.get(key))
        for key in GLOBAL_SECRET_ENV_MAP
    }

    agents_payload: list[dict[str, Any]] = []
    for agent in _normalize_agents(agents_with_refs):
        agent_copy = deepcopy(agent)
        secret_meta = {key: {"configured": False, "masked_value": ""} for key in AGENT_SECRET_KEYS}
        for key in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret", "okx_api_key", "okx_secret"):
            secret_meta[key] = secret_view(agent_copy.pop(key, None))

        summarizer = dict(agent_copy.get("summarizer") or {})
        secret_meta["summarizer_api_key"] = secret_view(summarizer.pop("api_key", None))
        agent_copy["summarizer"] = summarizer
        agent_copy["secrets"] = secret_meta
        agents_payload.append(agent_copy)

    providers_payload: list[dict[str, Any]] = []
    for provider in providers:
        provider_copy = deepcopy(provider)
        provider_copy["secrets"] = {
            key: secret_view(provider_copy.pop(key, None))
            for key in LLM_PROVIDER_SECRET_KEYS
        }
        providers_payload.append(provider_copy)

    profiles_payload: list[dict[str, Any]] = []
    for profile in profiles:
        profile_copy = deepcopy(profile)
        profile_copy["secrets"] = {
            key: secret_view(profile_copy.pop(key, None))
            for key in EXCHANGE_PROFILE_SECRET_KEYS
        }
        profiles_payload.append(profile_copy)

    return {
        "globals": globals_payload,
        "agents": agents_payload,
        "llm_providers": providers_payload,
        "exchange_profiles": profiles_payload,
        "source": snapshot.get("source", "env"),
    }


def save_runtime_snapshot(
    globals_payload: dict[str, Any],
    agents_payload: list[dict[str, Any]],
    llm_providers_payload: list[dict[str, Any]] | None = None,
    exchange_profiles_payload: list[dict[str, Any]] | None = None,
    *,
    validate_snapshot: Callable[[dict[str, Any]], None] | None = None,
    transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    load_dotenv(override=False)
    normalized_agents = _normalize_agents(agents_payload)

    settings = deepcopy(DEFAULT_GLOBAL_SETTINGS)
    for key, value in (globals_payload or {}).items():
        if key == "secrets" or key in GLOBAL_SECRET_ENV_MAP:
            continue
        settings[key] = _ensure_jsonable(value)

    from backend.utils.polymarket import PolymarketSettings

    settings['polymarket'] = PolymarketSettings.model_validate(settings['polymarket']).model_dump()
    from backend.app.schemas.news import NewsSettings, PricingSyncSettings
    settings['news'] = NewsSettings.model_validate(settings['news']).model_dump()
    settings['pricing_sync'] = PricingSyncSettings.model_validate(settings['pricing_sync']).model_dump()

    global_secret_updates = dict(globals_payload.get("secrets") or {})
    for secret_key in GLOBAL_SECRET_ENV_MAP:
        raw_value = globals_payload.get(secret_key)
        if raw_value not in (None, ""):
            global_secret_updates.setdefault(secret_key, {"value": raw_value})

    llm_providers, exchange_profiles, normalized_agents = _derive_referenced_configs(
        globals_payload,
        normalized_agents,
        llm_providers_payload,
        exchange_profiles_payload,
    )
    profiles_by_id = {str(profile.get("profile_id")): profile for profile in exchange_profiles}
    for agent in normalized_agents:
        if agent.get("mode") != "SPOT_DCA":
            continue
        profile = profiles_by_id.get(str(agent.get("exchange_profile_id") or ""))
        if profile and str(profile.get("market_type") or "swap").lower() != "spot":
            raise ValueError(f"SPOT_DCA task {agent['config_id']} requires a spot exchange profile")

    with _get_conn() as conn:
        _ensure_agent_config_sort_order(conn)
        _ensure_provider_tables(conn)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM app_settings")
            timestamp = _now()
            conn.executemany(
                "INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                [
                    (key, json.dumps(_ensure_jsonable(value), ensure_ascii=False), timestamp)
                    for key, value in settings.items()
                ],
            )

            conn.execute("DELETE FROM llm_providers")
            conn.execute("DELETE FROM exchange_profiles")
            active_provider_ids = {str(item.get("provider_id") or "") for item in llm_providers}
            active_profile_ids = {str(item.get("profile_id") or "") for item in exchange_profiles}
            if active_provider_ids:
                placeholders = ",".join("?" for _ in active_provider_ids)
                conn.execute(
                    f"DELETE FROM secret_store WHERE scope = 'llm_provider' AND scope_id NOT IN ({placeholders})",
                    tuple(active_provider_ids),
                )
            else:
                conn.execute("DELETE FROM secret_store WHERE scope = 'llm_provider'")
            if active_profile_ids:
                placeholders = ",".join("?" for _ in active_profile_ids)
                conn.execute(
                    f"DELETE FROM secret_store WHERE scope = 'exchange_profile' AND scope_id NOT IN ({placeholders})",
                    tuple(active_profile_ids),
                )
            else:
                conn.execute("DELETE FROM secret_store WHERE scope = 'exchange_profile'")

            for provider in llm_providers:
                provider_id = str(provider.get("provider_id") or "").strip()
                if not provider_id:
                    continue
                extra_body = provider.get("extra_body") if isinstance(provider.get("extra_body"), dict) else {}
                conn.execute(
                    """
                    INSERT INTO llm_providers (
                        provider_id, name, model, api_base, temperature, role, extra_body, compatibility_mode, thinking_enabled, reasoning_effort, system_prompt_role, updated_at, settings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider_id,
                        str(provider.get("name") or provider_id),
                        str(provider.get("model") or ""),
                        str(provider.get("api_base") or ""),
                        provider.get("temperature"),
                        str(provider.get("role") or "llm"),
                        json.dumps(_ensure_jsonable(extra_body), ensure_ascii=False),
                        str(provider.get("compatibility_mode") or "auto").lower(),
                        None if provider.get("thinking_enabled") is None else int(bool(provider.get("thinking_enabled"))),
                        str(provider.get("reasoning_effort") or ""),
                        "user" if str(provider.get("system_prompt_role") or "system").lower() == "user" else "system",
                        timestamp,
                        json.dumps({key: provider[key] for key in PROVIDER_OPTION_KEYS if key in provider}, ensure_ascii=False),
                    ),
                )
                secret_updates = dict(provider.get("secrets") or {})
                for secret_key in LLM_PROVIDER_SECRET_KEYS:
                    raw_value = provider.get(secret_key)
                    if raw_value not in (None, ""):
                        secret_updates.setdefault(secret_key, {"value": raw_value})
                _apply_secret_updates(conn, "llm_provider", provider_id, secret_updates)

            for profile in exchange_profiles:
                profile_id = str(profile.get("profile_id") or "").strip()
                if not profile_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO exchange_profiles (
                        profile_id, name, exchange, market_type, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        str(profile.get("name") or profile_id),
                        str(profile.get("exchange") or "binance").lower(),
                        str(profile.get("market_type") or "swap"),
                        timestamp,
                    ),
                )
                secret_updates = dict(profile.get("secrets") or {})
                for secret_key in EXCHANGE_PROFILE_SECRET_KEYS:
                    raw_value = profile.get(secret_key)
                    if raw_value not in (None, ""):
                        secret_updates.setdefault(secret_key, {"value": raw_value})
                _apply_secret_updates(conn, "exchange_profile", profile_id, secret_updates)

            existing_ids = {
                row["config_id"]
                for row in conn.execute("SELECT config_id FROM agent_configs").fetchall()
            }
            next_ids = {item["config_id"] for item in normalized_agents}
            removed_ids = existing_ids - next_ids
            for removed_id in removed_ids:
                conn.execute("DELETE FROM agent_configs WHERE config_id = ?", (removed_id,))
                conn.execute(
                    "DELETE FROM secret_store WHERE scope = 'agent' AND scope_id = ?",
                    (removed_id,),
                )

            for sort_order, agent in enumerate(normalized_agents):
                config_id = agent["config_id"]
                non_secret_payload, raw_secret_values = _split_agent_payload(agent)
                secret_updates = dict(agent.get("secrets") or {})
                for secret_key, secret_value in raw_secret_values.items():
                    secret_updates.setdefault(secret_key, {"value": secret_value})

                serialized = json.dumps(_ensure_jsonable(non_secret_payload), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO agent_configs (config_id, symbol, enabled, mode, data_json, updated_at, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(config_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        enabled = excluded.enabled,
                        mode = excluded.mode,
                        data_json = excluded.data_json,
                        updated_at = excluded.updated_at,
                        sort_order = excluded.sort_order
                    """,
                    (
                        config_id,
                        str(non_secret_payload.get("symbol", "")),
                        1 if _normalize_bool(non_secret_payload.get("enabled", True)) else 0,
                        str(non_secret_payload.get("mode", "STRATEGY")).upper(),
                        serialized,
                        timestamp,
                        sort_order,
                    ),
                )
                _apply_secret_updates(conn, "agent", config_id, secret_updates)

            _apply_secret_updates(conn, "global", "global", global_secret_updates)
            if validate_snapshot is not None:
                candidate = load_runtime_snapshot(connection=conn)
                if candidate is None:
                    raise ValueError(LAST_RUNTIME_CONFIG_ERROR or "Unable to validate candidate runtime configuration")
                validate_snapshot(candidate)
            if transaction_hook is not None:
                transaction_hook(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _apply_secret_updates(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    updates: dict[str, Any],
) -> None:
    timestamp = _now()
    for field_name, update in updates.items():
        action, value = _parse_secret_update(update)
        if action is None:
            continue

        secret_key = SECRET_FIELD_DB_KEY.get(field_name, field_name)
        if action == "clear":
            conn.execute(
                """
                DELETE FROM secret_store
                WHERE scope = ? AND scope_id = ? AND secret_key = ?
                """,
                (scope, scope_id, secret_key),
            )
            continue

        conn.execute(
            """
            INSERT INTO secret_store (scope, scope_id, secret_key, encrypted_value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, scope_id, secret_key) DO UPDATE SET
                encrypted_value = excluded.encrypted_value,
                updated_at = excluded.updated_at
            """,
            (scope, scope_id, secret_key, _encrypt_secret(value or ""), timestamp),
        )


def export_agent_configs() -> list[dict[str, Any]]:
    snapshot = load_effective_runtime_snapshot()
    exported: list[dict[str, Any]] = []
    for agent in snapshot.get("agents", []):
        clean_agent, _ = _split_agent_payload(agent)
        exported.append(clean_agent)
    return exported


def runtime_options_payload() -> dict[str, Any]:
    return {
        "modes": ["REAL", "STRATEGY", "SPOT_DCA"],
        "exchanges": ["binance", "okx"],
        "market_types": ["swap", "spot"],
        "dca_freqs": ["1d", "1w"],
        "market_timeframes": ["15m", "30m", "1h", "4h", "1d", "1w", "1M"],
        "spot_market_timeframes": list(SPOT_MARKET_TIMEFRAMES),
        "max_spot_symbols": 10,
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "compatibility_modes": ["auto", "openai", "anthropic", "deepseek"],
    }


# --- 全量导出 / 导入 ---

_EXPORT_ENV_KEYS = [
    "ADMIN_PASSWORD",
    "JWT_SECRET",
    "JWT_EXPIRE_HOURS",
    "CONFIG_MASTER_KEY",
    "PORT",
    "RUN_SCHEDULER_IN_WEB",
    "TIMEZONE",
    "TRADING_DB_PATH",
    "DATA_DIR",
]


def export_full_snapshot(include_secrets: bool = True) -> dict[str, Any]:
    """导出完整配置快照，包含 agents/providers/profiles/global_secrets/prompts/pricing/env 引导变量。"""
    from datetime import datetime

    snapshot = load_runtime_snapshot()
    if snapshot is None:
        snapshot = _env_snapshot()

    providers, profiles, agents = _derive_referenced_configs(
        snapshot, snapshot.get("agents", []), snapshot.get("llm_providers") or [],
        snapshot.get("exchange_profiles") or [],
    )
    snapshot = {**snapshot, "agents": agents, "llm_providers": providers, "exchange_profiles": profiles}

    # 全局设置
    app_settings = {
        key: snapshot.get(key, default)
        for key, default in DEFAULT_GLOBAL_SETTINGS.items()
    }

    # agents — 将明文密钥收入 _secrets，去掉 secrets（masked 元数据）
    agents_out: list[dict[str, Any]] = []
    for agent in snapshot.get("agents", []):
        agent_copy = deepcopy(agent)
        agent_copy.pop("secrets", None)
        _, raw_secrets = _split_agent_payload(agent_copy)
        for k in list(raw_secrets.keys()):
            agent_copy.pop(k, None)
        # summarizer.api_key already popped by _split_agent_payload
        summarizer = dict(agent_copy.get("summarizer") or {})
        summarizer.pop("api_key", None)
        agent_copy["summarizer"] = summarizer
        if include_secrets:
            agent_copy["_secrets"] = raw_secrets
        else:
            agent_copy["_secrets"] = {}
        agents_out.append(agent_copy)

    # llm_providers
    providers_out: list[dict[str, Any]] = []
    for provider in snapshot.get("llm_providers", []):
        p = deepcopy(provider)
        p.pop("secrets", None)
        if include_secrets:
            p["_secrets"] = {k: p.pop(k) for k in list(LLM_PROVIDER_SECRET_KEYS) if p.get(k) not in (None, "")}
        else:
            for k in LLM_PROVIDER_SECRET_KEYS:
                p.pop(k, None)
            p["_secrets"] = {}
        providers_out.append(p)

    # exchange_profiles
    profiles_out: list[dict[str, Any]] = []
    for profile in snapshot.get("exchange_profiles", []):
        pf = deepcopy(profile)
        pf.pop("secrets", None)
        if include_secrets:
            pf["_secrets"] = {k: pf.pop(k) for k in list(EXCHANGE_PROFILE_SECRET_KEYS) if pf.get(k) not in (None, "")}
        else:
            for k in EXCHANGE_PROFILE_SECRET_KEYS:
                pf.pop(k, None)
            pf["_secrets"] = {}
        profiles_out.append(pf)

    # global_secrets
    global_secrets_out: dict[str, str] = {}
    if include_secrets:
        for key in GLOBAL_SECRET_ENV_MAP:
            val = snapshot.get(key)
            if val:
                global_secrets_out[key] = val

    # .env 引导变量
    load_dotenv(override=False)
    env_out: dict[str, str] = {}
    for env_key in _EXPORT_ENV_KEYS:
        if not include_secrets and env_key in {'ADMIN_PASSWORD', 'JWT_SECRET', 'CONFIG_MASTER_KEY'}:
            continue
        val = os.getenv(env_key)
        if val is not None:
            env_out[env_key] = val

    return {
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "include_secrets": include_secrets,
        "env": env_out,
        "app_settings": app_settings,
        "agents": agents_out,
        "llm_providers": providers_out,
        "exchange_profiles": profiles_out,
        "global_secrets": global_secrets_out,
    }


def import_full_snapshot(
    data: dict[str, Any],
    write_env: bool = False,
    prompt_files: dict[str, str] | None = None,
    model_pricing: list[dict[str, Any]] | None = None,
    validate_snapshot: Callable[[dict[str, Any]], None] | None = None,
    transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    """
    将完整配置快照还原到数据库，可选写入 .env 引导变量。
    prompt_files: {filename: content}
    model_pricing: [{"model": ..., "input_price_per_m": ..., "output_price_per_m": ..., "currency": ...}]
    """
    from backend.database import update_model_pricing
    from pathlib import Path

    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported export version: {version}")

    app_settings: dict[str, Any] = data.get("app_settings") or {}
    agents_in: list[dict[str, Any]] = data.get("agents") or []
    providers_in: list[dict[str, Any]] = data.get("llm_providers") or []
    profiles_in: list[dict[str, Any]] = data.get("exchange_profiles") or []
    global_secrets_in: dict[str, str] = data.get("global_secrets") or {}

    # 将 _secrets 合并回 agent payload 供 save_runtime_snapshot 处理
    def _merge_secrets_back(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in items:
            merged = deepcopy(item)
            raw_secrets = merged.pop("_secrets", None) or {}
            merged.update({k: v for k, v in raw_secrets.items() if v not in (None, "")})
            result.append(merged)
        return result

    agents_merged = _merge_secrets_back(agents_in)
    providers_merged = _merge_secrets_back(providers_in)
    profiles_merged = _merge_secrets_back(profiles_in)
    agents_merged, profiles_merged = _upgrade_legacy_spot_profiles(agents_merged, profiles_merged)

    # 将 global_secrets 合并进 globals_payload（使用明文值，save_runtime_snapshot 内会加密）
    globals_payload: dict[str, Any] = deepcopy(app_settings)
    globals_payload.update(global_secrets_in)

    save_runtime_snapshot(
        globals_payload, agents_merged, providers_merged, profiles_merged,
        validate_snapshot=validate_snapshot,
        transaction_hook=transaction_hook,
    )

    # 还原 prompt 文件
    prompts_restored: list[str] = []
    if prompt_files:
        import os as _os
        prompt_directory = Path(__file__).resolve().parent / "agent" / "prompts"
        prompt_directory.mkdir(parents=True, exist_ok=True)
        for filename, content in prompt_files.items():
            # 基本安全校验
            if not filename or ".." in filename or not filename.endswith(".txt"):
                continue
            dest = prompt_directory / filename
            dest.write_text(content, encoding="utf-8")
            prompts_restored.append(filename)

    # 还原模型计价
    pricing_restored = 0
    if model_pricing:
        for row in model_pricing:
            model_name = row.get("model")
            if not model_name:
                continue
            update_model_pricing(
                model_name,
                row.get("input_price_per_m", 0),
                row.get("output_price_per_m", 0),
                row.get("currency", "USD"),
            )
            pricing_restored += 1

    # 可选写入 .env 文件
    env_written: list[str] = []
    if write_env:
        env_data: dict[str, str] = data.get("env") or {}
        if env_data:
            env_path = BASE_DIR / ".env"
            # 读取现有内容
            existing_lines: list[str] = []
            if env_path.exists():
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()

            existing_keys: dict[str, int] = {}
            for idx, line in enumerate(existing_lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    existing_keys[k] = idx

            updated_lines = list(existing_lines)
            for k, v in env_data.items():
                if not v:
                    continue
                new_line = f"{k}={v}"
                if k in existing_keys:
                    updated_lines[existing_keys[k]] = new_line
                else:
                    updated_lines.append(new_line)
                env_written.append(k)

            env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    # 重载运行配置
    try:
        from backend.config import config as runtime_config
        runtime_config.reload_config()
    except Exception as e:
        logger.warning(f"⚠️ 导入后重载配置失败: {e}")

    return {
        "agents_imported": len(agents_merged),
        "providers_imported": len(providers_merged),
        "profiles_imported": len(profiles_merged),
        "prompts_restored": prompts_restored,
        "pricing_restored": pricing_restored,
        "env_written": env_written,
    }
