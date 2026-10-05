from copy import deepcopy
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import config_store
from backend.app.api import config as routes
from backend.app.core.deps import get_current_user
from backend.app.services import config_service, market_catalog_service
from backend.config import Config
from backend.database_schema import initialize_schema


@pytest.fixture
def isolated_config_api(tmp_path, monkeypatch):
    db_path = tmp_path / "config.sqlite"
    with sqlite3.connect(db_path) as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config_store, "DB_NAME", db_path)
    monkeypatch.setattr(config_store, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("CONFIG_MASTER_KEY", "atomicity-test-key")
    config_store.save_runtime_snapshot({"enable_scheduler": False, "langchain_project": "preserved"}, [])
    runtime = Config()
    monkeypatch.setattr(config_service, "global_config", runtime)
    monkeypatch.setattr(market_catalog_service, "get_market_catalog", lambda *_: ([{"symbol": "BTC/USDT"}], False))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-admin"}
    return TestClient(app), runtime, db_path


def database_contents(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize("enabled", [False, True])
def test_http_put_missing_credentials_rolls_back_disk_and_runtime(isolated_config_api, enabled):
    client, runtime, db_path = isolated_config_api
    before_db = database_contents(db_path)
    before_runtime = deepcopy(runtime.__dict__)
    response = client.put("/api/config", json={
        "globals": {"enable_scheduler": True, "langchain_project": "must-not-stick"},
        "agents": [{"config_id": "invalid", "mode": "SPOT_DCA", "enabled": enabled,
                    "symbols": ["BTC/USDT"], "exchange_profile_id": "public-only"}],
        "exchange_profiles": [{"profile_id": "public-only", "name": "Public only", "exchange": "binance", "market_type": "spot"}],
    })
    assert response.status_code == 400
    assert "missing API credentials" in response.json()["detail"]
    assert database_contents(db_path) == before_db
    assert runtime.__dict__ == before_runtime


def test_http_import_missing_credentials_rolls_back_disk_runtime_and_files(isolated_config_api, tmp_path):
    client, runtime, db_path = isolated_config_api
    before_db = database_contents(db_path)
    before_runtime = deepcopy(runtime.__dict__)
    response = client.post("/api/config/full-import", json={"data": {
        "version": 1,
        "app_settings": {"enable_scheduler": True, "langchain_project": "must-not-stick"},
        "agents": [{"config_id": "invalid", "mode": "SPOT_DCA", "enabled": False,
                    "symbols": ["BTC/USDT"], "exchange_profile_id": "public-only"}],
        "exchange_profiles": [{"profile_id": "public-only", "exchange": "binance", "market_type": "spot"}],
    }})
    assert response.status_code == 400
    assert database_contents(db_path) == before_db
    assert runtime.__dict__ == before_runtime


def test_transaction_validator_sees_actual_merged_secrets_and_rollback_keeps_ciphertext(isolated_config_api):
    _, runtime, db_path = isolated_config_api
    config_store.save_runtime_snapshot({
        "global_binance_api_key": "saved-key", "global_binance_secret": "saved-secret",
    }, [{"config_id": "spot", "mode": "SPOT_DCA", "symbol": "BTC/USDT"}], validate_snapshot=Config.validate_snapshot)
    runtime.reload_config()
    before_db = database_contents(db_path)
    before_runtime = deepcopy(runtime.__dict__)
    snapshot = config_store.load_management_snapshot()
    snapshot["globals"]["secrets"]["global_binance_secret"] = {"clear": True}
    # The derived profile also has its own saved secret: clear it and the
    # legacy per-agent copy to exercise the fully resolved transaction preview.
    for profile in snapshot["exchange_profiles"]:
        profile["secrets"]["secret"] = {"clear": True}
    for agent in snapshot["agents"]:
        agent["secrets"]["secret"] = {"clear": True}
        agent["secrets"]["binance_secret"] = {"clear": True}
    with pytest.raises(ValueError, match="missing API credentials"):
        config_store.save_runtime_snapshot(snapshot["globals"], snapshot["agents"], snapshot["llm_providers"],
                                           snapshot["exchange_profiles"], validate_snapshot=runtime.validate_snapshot)
    assert database_contents(db_path) == before_db
    assert runtime.__dict__ == before_runtime


def test_apply_snapshot_is_atomic_when_validation_or_conversion_fails():
    runtime = Config.__new__(Config)
    runtime._apply_snapshot({"agents": [], "enable_scheduler": False, "langchain_project": "preserved"})
    before = deepcopy(runtime.__dict__)
    for invalid in (
        {"agents": [{"config_id": "invalid", "mode": "SPOT_DCA", "symbol": "BTC/USDT"}], "enable_scheduler": True},
        {"agents": [], "enable_scheduler": True, "leverage": "bad-number"},
    ):
        with pytest.raises(ValueError):
            runtime._apply_snapshot(invalid)
        assert runtime.__dict__ == before


def test_other_exchange_global_credentials_do_not_validate_missing_okx_account():
    with pytest.raises(ValueError, match="OKX"):
        Config.validate_snapshot({
            "global_binance_api_key": "binance-key", "global_binance_secret": "binance-secret",
            "agents": [{"config_id": "okx", "mode": "SPOT_DCA", "symbol": "BTC/USDT", "exchange": "okx"}],
        })


def test_okx_global_passphrase_fallback_matches_execution_credentials():
    Config.validate_snapshot({
        "global_okx_passphrase": "global-passphrase",
        "agents": [{"config_id": "okx", "mode": "SPOT_DCA", "symbol": "BTC/USDT", "exchange": "okx",
                    "okx_api_key": "okx-key", "okx_secret": "okx-secret"}],
    })
