import os
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_WEAK_VALUES = {
    "ADMIN_PASSWORD": {"", "123456", "change-this-password"},
    "JWT_SECRET": {"", "change-this-jwt-secret", "dev-secret"},
    "CONFIG_MASTER_KEY": {"", "change-this-config-master-key"},
}


def _read_env_values() -> dict[str, str]:
    values = {key: os.getenv(key, "") for key in DEFAULT_WEAK_VALUES}
    if ENV_PATH.exists():
        file_values = dotenv_values(ENV_PATH)
        for key in DEFAULT_WEAK_VALUES:
            if file_values.get(key) is not None:
                values[key] = str(file_values.get(key) or "")
    return values


def _is_docker_runtime() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("DATA_DIR", "").startswith("/app/")


def get_setup_status_payload() -> dict:
    values = _read_env_values()
    weak_keys = [key for key, weak_values in DEFAULT_WEAK_VALUES.items() if values.get(key, "") in weak_values]
    env_exists = ENV_PATH.exists()
    docker_runtime = _is_docker_runtime()
    return {
        "required": bool(weak_keys or (not env_exists and not docker_runtime)),
        "env_exists": env_exists,
        "weak_keys": weak_keys,
        "docker_runtime": docker_runtime,
        "env_path": str(ENV_PATH),
    }


def _upsert_env(keys: dict[str, str]) -> list[str]:
    existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    updated_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in keys:
            updated_lines.append(f"{key}={keys[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)
    for key, value in keys.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return list(keys.keys())


def apply_setup_payload(payload: dict) -> dict:
    required = ["admin_password", "jwt_secret", "config_master_key"]
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if missing:
        raise ValueError(f"Missing setup fields: {', '.join(missing)}")

    keys = {
        "ADMIN_PASSWORD": str(payload["admin_password"]),
        "JWT_SECRET": str(payload["jwt_secret"]),
        "CONFIG_MASTER_KEY": str(payload["config_master_key"]),
        "JWT_EXPIRE_HOURS": str(int(payload.get("jwt_expire_hours") or 8)),
        "PORT": str(int(payload.get("port") or 7860)),
        "TIMEZONE": str(payload.get("timezone") or "Asia/Shanghai"),
        "RUN_SCHEDULER_IN_WEB": "true" if payload.get("run_scheduler_in_web", True) else "false",
    }
    written = _upsert_env(keys)
    return {
        "message": "Bootstrap .env updated. Restart the backend/container for security settings to take effect.",
        "env_written": written,
        "restart_required": True,
        "docker_runtime": _is_docker_runtime(),
    }
