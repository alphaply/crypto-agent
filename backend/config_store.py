from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from backend.utils.logger import setup_logger


logger = setup_logger("ConfigStore")

BASE_DIR = Path(__file__).resolve().parents[1]
DB_NAME = BASE_DIR / "trading_data.db"

DEFAULT_GLOBAL_SETTINGS: dict[str, Any] = {
    "leverage": 20,
    "enable_scheduler": True,
    "trading_mode": "REAL",
    "langchain_tracing": False,
    "langchain_project": "crypto-agent",
    "llm_timeout_seconds": 120.0,
    "llm_max_retries": 2,
    "global_summarizer_model": "",
    "global_summarizer_api_base": "",
}

GLOBAL_SECRET_ENV_MAP = {
    "global_binance_api_key": "BINANCE_API_KEY",
    "global_binance_secret": "BINANCE_SECRET",
    "global_okx_api_key": "OKX_API_KEY",
    "global_okx_secret": "OKX_SECRET",
    "global_okx_passphrase": "OKX_PASSPHRASE",
    "langchain_api_key": "LANGCHAIN_API_KEY",
    "global_summarizer_api_key": "GLOBAL_SUMMARIZER_API_KEY",
}

AGENT_SECRET_KEYS = {
    "api_key",
    "secret",
    "passphrase",
    "binance_api_key",
    "binance_secret",
    "summarizer_api_key",
}

SECRET_FIELD_DB_KEY = {
    "summarizer_api_key": "summarizer.api_key",
}

SECRET_FIELD_API_KEY = {value: key for key, value in SECRET_FIELD_DB_KEY.items()}

TABLE_NAMES = {"app_settings", "agent_configs", "secret_store"}


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
    summarizer = dict(payload.get("summarizer") or {})
    secret_values: dict[str, str] = {}

    for field in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret"):
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
    for field in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret"):
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


def _normalize_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, item in enumerate(agents):
        payload = deepcopy(item)
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
        normalized.append(payload)

    return normalized


def _env_global_settings() -> dict[str, Any]:
    settings = deepcopy(DEFAULT_GLOBAL_SETTINGS)
    settings["leverage"] = int(os.getenv("LEVERAGE", settings["leverage"]))
    settings["enable_scheduler"] = _normalize_bool(os.getenv("ENABLE_SCHEDULER", settings["enable_scheduler"]))
    settings["trading_mode"] = os.getenv("TRADING_MODE", settings["trading_mode"])
    settings["langchain_tracing"] = _normalize_bool(os.getenv("LANGCHAIN_TRACING_V2", settings["langchain_tracing"]))
    settings["langchain_project"] = os.getenv("LANGCHAIN_PROJECT", settings["langchain_project"])
    settings["llm_timeout_seconds"] = float(os.getenv("LLM_TIMEOUT_SECONDS", settings["llm_timeout_seconds"]))
    settings["llm_max_retries"] = int(os.getenv("LLM_MAX_RETRIES", settings["llm_max_retries"]))
    settings["global_summarizer_model"] = os.getenv("GLOBAL_SUMMARIZER_MODEL", settings["global_summarizer_model"])
    settings["global_summarizer_api_base"] = os.getenv(
        "GLOBAL_SUMMARIZER_API_BASE", settings["global_summarizer_api_base"]
    )
    return settings


def _env_global_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    for key, env_key in GLOBAL_SECRET_ENV_MAP.items():
        value = os.getenv(env_key)
        if value:
            secrets[key] = value
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

    with _get_conn() as conn:
        if not _has_runtime_storage(conn):
            return
        if _has_runtime_rows(conn):
            return

    snapshot = _env_snapshot()
    save_runtime_snapshot(
        {key: snapshot[key] for key in snapshot if key not in {"agents", "source"}},
        snapshot["agents"],
    )
    logger.info("Runtime configuration migrated from .env into SQLite.")


def load_runtime_snapshot() -> dict[str, Any] | None:
    if not DB_NAME.exists():
        return None

    with _get_conn() as conn:
        if not _has_runtime_storage(conn) or not _has_runtime_rows(conn):
            return None

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
        for row in secret_rows:
            field_name = SECRET_FIELD_API_KEY.get(row["secret_key"], row["secret_key"])
            decrypted = _decrypt_secret(row["encrypted_value"])
            if row["scope"] == "global":
                global_secrets[field_name] = decrypted
            elif row["scope"] == "agent":
                agent_secret_map.setdefault(row["scope_id"], {})[field_name] = decrypted

        agent_rows = conn.execute(
            """
            SELECT config_id, data_json
            FROM agent_configs
            ORDER BY symbol ASC, config_id ASC
            """
        ).fetchall()

        agents: list[dict[str, Any]] = []
        for row in agent_rows:
            data = json.loads(row["data_json"])
            if not isinstance(data, dict):
                continue
            payload = _restore_agent_payload(data, agent_secret_map.get(row["config_id"], {}))
            agents.append(payload)

        return {**settings, **global_secrets, "agents": agents, "source": "db"}


def load_effective_runtime_snapshot() -> dict[str, Any]:
    snapshot = load_runtime_snapshot()
    return snapshot or _env_snapshot()


def load_management_snapshot() -> dict[str, Any]:
    snapshot = load_effective_runtime_snapshot()
    globals_payload = {
        key: snapshot.get(key, default)
        for key, default in DEFAULT_GLOBAL_SETTINGS.items()
    }
    globals_payload["secrets"] = {
        key: _mask_secret(snapshot.get(key))
        for key in GLOBAL_SECRET_ENV_MAP
    }

    agents_payload: list[dict[str, Any]] = []
    for agent in _normalize_agents(snapshot.get("agents", [])):
        agent_copy = deepcopy(agent)
        secret_meta = {key: {"configured": False, "masked_value": ""} for key in AGENT_SECRET_KEYS}
        for key in ("api_key", "secret", "passphrase", "binance_api_key", "binance_secret"):
            secret_meta[key] = _mask_secret(agent_copy.pop(key, None))

        summarizer = dict(agent_copy.get("summarizer") or {})
        secret_meta["summarizer_api_key"] = _mask_secret(summarizer.pop("api_key", None))
        agent_copy["summarizer"] = summarizer
        agent_copy["secrets"] = secret_meta
        agents_payload.append(agent_copy)

    return {"globals": globals_payload, "agents": agents_payload, "source": snapshot.get("source", "env")}


def save_runtime_snapshot(globals_payload: dict[str, Any], agents_payload: list[dict[str, Any]]) -> None:
    load_dotenv(override=False)
    normalized_agents = _normalize_agents(agents_payload)

    settings = deepcopy(DEFAULT_GLOBAL_SETTINGS)
    for key, value in (globals_payload or {}).items():
        if key == "secrets" or key in GLOBAL_SECRET_ENV_MAP:
            continue
        settings[key] = _ensure_jsonable(value)

    global_secret_updates = dict(globals_payload.get("secrets") or {})
    for secret_key in GLOBAL_SECRET_ENV_MAP:
        raw_value = globals_payload.get(secret_key)
        if raw_value not in (None, ""):
            global_secret_updates.setdefault(secret_key, {"value": raw_value})

    with _get_conn() as conn:
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

            for agent in normalized_agents:
                config_id = agent["config_id"]
                non_secret_payload, raw_secret_values = _split_agent_payload(agent)
                secret_updates = dict(agent.get("secrets") or {})
                for secret_key, secret_value in raw_secret_values.items():
                    secret_updates.setdefault(secret_key, {"value": secret_value})

                serialized = json.dumps(_ensure_jsonable(non_secret_payload), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO agent_configs (config_id, symbol, enabled, mode, data_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(config_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        enabled = excluded.enabled,
                        mode = excluded.mode,
                        data_json = excluded.data_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        config_id,
                        str(non_secret_payload.get("symbol", "")),
                        1 if _normalize_bool(non_secret_payload.get("enabled", True)) else 0,
                        str(non_secret_payload.get("mode", "STRATEGY")).upper(),
                        serialized,
                        timestamp,
                    ),
                )
                _apply_secret_updates(conn, "agent", config_id, secret_updates)

            _apply_secret_updates(conn, "global", "global", global_secret_updates)
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
    }
