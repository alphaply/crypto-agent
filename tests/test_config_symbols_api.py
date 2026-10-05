from copy import deepcopy
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.api import config as routes
from backend.app.core.deps import get_current_user
from backend.app.services import config_service
from backend.app.services import market_catalog_service
from backend.utils import spot_config_guard


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-admin"}
    return TestClient(app)


@pytest.fixture
def snapshot(monkeypatch):
    state = {
        "globals": {"market_timeframes": ["4h", "1d", "1w"], "secrets": {"global_binance_api_key": {"configured": True, "masked_value": "***"}}},
        "agents": [{"config_id": "spot", "mode": "SPOT_DCA", "symbol": "BTC/USDT", "symbols": ["BTC/USDT"], "exchange": "binance", "exchange_profile_id": "p1", "llm_provider_id": "llm", "title": "My basket", "enabled": False, "dca_amount": 100}],
        "exchange_profiles": [{"profile_id": "p1", "exchange": "binance", "market_type": "spot", "secrets": {"api_key": {"configured": True, "masked_value": "***"}}}],
        "llm_providers": [{"provider_id": "llm", "model": "test", "secrets": {"api_key": {"configured": True, "masked_value": "***"}}}],
    }
    monkeypatch.setattr(config_service, "load_management_snapshot", lambda: deepcopy(state))
    monkeypatch.setattr(config_service.global_config, "get_config_by_id", lambda cid: deepcopy(state["agents"][0]) if cid == "spot" else None)
    monkeypatch.setattr(config_service.global_config, "symbol_configs", deepcopy(state["agents"]))
    return state


def test_config_symbol_endpoints_require_authentication():
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    assert client.get("/api/config/market-symbols", params={"exchange": "binance"}).status_code == 401
    assert client.patch("/api/config/spot/symbols", json={"symbols": ["BTC/USDT"]}).status_code == 401


def test_config_market_catalog_route_forwards_all_filters(client, monkeypatch):
    catalog = Mock(return_value={"symbols": [], "total": 0, "has_more": False})
    monkeypatch.setattr(routes, "list_market_symbols_payload", catalog)
    response = client.get("/api/config/market-symbols", params={"exchange": "okx", "quote": "USDT", "keyword": "BTC", "offset": 100, "limit": 50, "symbols": "BTC/USDT,ETH/USDT"})
    assert response.status_code == 200
    assert catalog.call_args.kwargs == {"exchange": "okx", "quote": "USDT", "limit": 50, "offset": 100, "symbols": ["BTC/USDT", "ETH/USDT"], "require_profile_market": True}


@pytest.mark.parametrize("params", [{"limit": 201}, {"limit": 0}, {"offset": -1}, {"market_type": "future"}])
def test_catalog_query_limits_rejected(client, params):
    assert client.get("/api/config/market-symbols", params={"exchange": "binance", **params}).status_code == 422


@pytest.mark.parametrize("symbols", [[], [f"COIN{i}/USDT" for i in range(11)]])
def test_patch_rejects_empty_or_oversized_symbol_lists(client, symbols):
    assert client.patch("/api/config/spot/symbols", json={"symbols": symbols}).status_code == 422


def test_patch_preserves_other_task_config_and_secret_metadata(client, monkeypatch, snapshot):
    save = Mock()
    monkeypatch.setattr(config_service, "save_config_payload", save)
    response = client.patch("/api/config/spot/symbols", json={"symbols": ["eth/usdt", "btc/usdt"], "expected_symbols": ["btc/usdt"]})
    assert response.status_code == 200
    assert response.json()["symbols"] == ["ETH/USDT", "BTC/USDT"]
    saved_globals, saved_agents, saved_providers, saved_profiles = save.call_args.args
    assert saved_globals == snapshot["globals"]
    assert saved_providers == snapshot["llm_providers"]
    assert saved_profiles == snapshot["exchange_profiles"]
    assert saved_agents[0] == {**snapshot["agents"][0], "symbol": "ETH/USDT", "symbols": ["ETH/USDT", "BTC/USDT"]}


def test_patch_expected_symbols_conflict_does_not_save(client, monkeypatch, snapshot):
    save = Mock()
    monkeypatch.setattr(config_service, "save_config_payload", save)
    response = client.patch("/api/config/spot/symbols", json={"symbols": ["ETH/USDT"], "expected_symbols": ["SOL/USDT"]})
    assert response.status_code == 409
    save.assert_not_called()


def test_patch_mixed_quote_and_unknown_task_rejected(client, snapshot):
    assert client.patch("/api/config/spot/symbols", json={"symbols": ["ETH/USDT", "BTC/USDC"]}).status_code == 400
    assert client.patch("/api/config/missing/symbols", json={"symbols": ["BTC/USDT"]}).status_code == 404


def test_patch_checks_exchange_catalog_and_preserves_db_on_rejection(client, monkeypatch, snapshot):
    save_db = Mock()
    monkeypatch.setattr(config_service, "save_runtime_snapshot", save_db)
    monkeypatch.setattr(market_catalog_service, "get_market_catalog", lambda *_: ([{"symbol": "BTC/USDT"}], False))
    response = client.patch("/api/config/spot/symbols", json={"symbols": ["FAKE/USDT"]})
    assert response.status_code == 400 and "不可交易" in response.json()["detail"]
    save_db.assert_not_called()


def test_patch_upstream_failure_returns_retryable_error_without_save(client, monkeypatch, snapshot):
    save_db = Mock()
    monkeypatch.setattr(config_service, "save_runtime_snapshot", save_db)
    monkeypatch.setattr(market_catalog_service, "get_market_catalog", Mock(side_effect=market_catalog_service.MarketCatalogUnavailable("catalog unavailable")))
    assert client.patch("/api/config/spot/symbols", json={"symbols": ["ETH/USDT"]}).status_code == 502
    save_db.assert_not_called()


def test_save_and_import_both_apply_lifecycle_guard_before_writing(monkeypatch, snapshot):
    save_db = Mock()
    importer = Mock()
    guard = Mock(side_effect=spot_config_guard.SpotConfigConflict("pending orders"))
    monkeypatch.setattr(config_service, "save_runtime_snapshot", save_db)
    monkeypatch.setattr(config_service, "import_full_snapshot", importer)
    monkeypatch.setattr(spot_config_guard, "assert_spot_config_change_allowed", guard)
    agent = {**snapshot["agents"][0], "symbols": ["ETH/USDT"]}
    with pytest.raises(spot_config_guard.SpotConfigConflict):
        config_service.save_config_payload(snapshot["globals"], [agent], [], snapshot["exchange_profiles"])
    with pytest.raises(spot_config_guard.SpotConfigConflict):
        config_service.full_import_payload({"agents": [agent], "exchange_profiles": snapshot["exchange_profiles"]})
    assert guard.call_count == 2
    save_db.assert_not_called()
    importer.assert_not_called()


def test_omitting_task_from_full_save_cannot_bypass_lifecycle_guard(monkeypatch, snapshot):
    save_db = Mock()
    monkeypatch.setattr(config_service, "save_runtime_snapshot", save_db)
    guard = Mock(side_effect=spot_config_guard.SpotConfigConflict("pending orders"))
    monkeypatch.setattr(spot_config_guard, "assert_spot_config_change_allowed", guard)
    with pytest.raises(spot_config_guard.SpotConfigConflict):
        config_service.save_config_payload({}, [], [], [])
    assert guard.call_args.args[1]["mode"] == "DELETED"
    save_db.assert_not_called()


@pytest.mark.parametrize("secret_field", ["secrets", "_secrets"])
def test_profile_secret_rotation_is_detected_even_when_profile_id_stays_the_same(secret_field):
    previous = {"exchange_profile_id": "p1", "exchange": "binance", "api_key": "old-key"}
    profile = {"profile_id": "p1", "exchange": "binance", secret_field: {"api_key": {"value": "new-key"} if secret_field == "secrets" else "new-key"}}
    assert config_service._spot_account_changed(previous, previous, [profile], {})


def test_exchange_key_is_not_compared_with_llm_key_on_import():
    previous = {"exchange_profile_id": "p1", "exchange": "binance", "api_key": "llm-key", "binance_api_key": "exchange-key"}
    profile = {"profile_id": "p1", "exchange": "binance", "_secrets": {"api_key": "exchange-key"}}
    agent = {**previous, "_secrets": {"api_key": "new-llm-key"}}
    assert not config_service._spot_account_changed(previous, agent, [profile], {})


def test_legacy_import_upgrades_profile_before_catalog_validation(monkeypatch):
    monkeypatch.setattr(config_service.global_config, "symbol_configs", [])
    monkeypatch.setattr(config_service.global_config, "get_config_by_id", lambda _: None)
    validate = Mock()
    importer = Mock(return_value={})
    monkeypatch.setattr(config_service, "validate_spot_market_symbols", validate)
    monkeypatch.setattr(config_service, "import_full_snapshot", importer)
    config_service.full_import_payload({
        "agents": [{"config_id": "old", "mode": "SPOT_DCA", "symbol": "BTC/USDT", "exchange_profile_id": "shared"}],
        "exchange_profiles": [{"profile_id": "shared", "exchange": "binance", "market_type": "swap"}],
    })
    agent, profiles, _ = validate.call_args.args
    assert agent["symbols"] == ["BTC/USDT"]
    assert agent["exchange_profile_id"] != "shared"
    assert next(profile for profile in profiles if profile["profile_id"] == agent["exchange_profile_id"])["market_type"] == "spot"
