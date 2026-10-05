from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services import market_catalog_service as service


def market(symbol, *, active=True, spot=True):
    base, quote = symbol.split("/")
    return {"symbol": symbol, "base": base, "quote": quote, "spot": spot, "swap": not spot, "active": active}


@pytest.fixture(autouse=True)
def clean_catalog_cache():
    service._market_symbol_cache.clear()
    yield
    service._market_symbol_cache.clear()


def fake_exchange(monkeypatch, markets):
    factory = Mock(return_value=SimpleNamespace(load_markets=Mock(return_value={str(i): item for i, item in enumerate(markets)})))
    monkeypatch.setattr(service.ccxt, "binance", factory)
    return factory


def test_public_catalog_filters_active_spot_and_never_passes_credentials(monkeypatch):
    factory = fake_exchange(monkeypatch, [market("BTC/USDT"), market("DELISTED/USDT", active=False), market("UNKNOWN/USDT", active=None), market("ETH/USDT", spot=False)])
    monkeypatch.setattr(service, "load_management_snapshot", lambda: {"exchange_profiles": [
        {"profile_id": "private", "exchange": "binance", "market_type": "spot", "api_key": "private-key", "secret": "private-secret"},
    ]})
    result = service.list_market_symbols_payload("private", require_profile_market=True)
    assert [item["symbol"] for item in result["symbols"]] == ["BTC/USDT"]
    config = factory.call_args.args[0]
    assert "apiKey" not in config and "secret" not in config
    assert config["options"]["fetchCurrencies"] is False
    assert config["options"]["fetchMarkets"] == {"types": ["spot"]}
    assert "private-key" not in str(result)


def test_pagination_search_quote_and_selected_validation_are_independent(monkeypatch):
    factory = fake_exchange(monkeypatch, [market(f"COIN{i:03}/USDT") for i in range(205)] + [market("BTC/USDC")])
    first = service.list_market_symbols_payload(exchange="binance", limit=100)
    second = service.list_market_symbols_payload(exchange="binance", offset=100, limit=100)
    last = service.list_market_symbols_payload(exchange="binance", offset=200, limit=100)
    assert first["total"] == 206 and first["has_more"]
    assert len({item["symbol"] for item in first["symbols"] + second["symbols"] + last["symbols"]}) == 206
    assert not last["has_more"] and last["cached"]
    filtered = service.list_market_symbols_payload(exchange="binance", keyword="coin204", quote="usdt", symbols=["BTC/USDC", "DEAD/USDT"])
    assert [item["symbol"] for item in filtered["symbols"]] == ["COIN204/USDT"]
    assert [item["symbol"] for item in filtered["selected_symbols"]] == ["BTC/USDC"]
    assert filtered["invalid_symbols"] == ["DEAD/USDT"]
    assert filtered["quote_currencies"] == ["USDC", "USDT"]
    factory.assert_called_once()


def test_catalog_failure_not_cached_and_transport_details_not_exposed(monkeypatch):
    client = SimpleNamespace(load_markets=Mock(side_effect=RuntimeError("sensitive transport details")))
    monkeypatch.setattr(service, "_public_exchange", lambda *_: client)
    with pytest.raises(service.MarketCatalogUnavailable) as error:
        service.get_market_catalog("okx")
    assert "sensitive" not in str(error.value)
    assert not service._market_symbol_cache
    client.load_markets.side_effect = None
    client.load_markets.return_value = {"BTC/USDT": market("BTC/USDT")}
    catalog, cached = service.get_market_catalog("okx")
    assert catalog and not cached


def test_expired_catalog_is_refreshed_and_not_used_on_error(monkeypatch):
    service._market_symbol_cache[("binance", "spot")] = (-1000, [{"symbol": "STALE/USDT"}])
    fake_exchange(monkeypatch, [])
    with pytest.raises(service.MarketCatalogUnavailable):
        service.get_market_catalog("binance")


@pytest.mark.parametrize("kwargs", [{}, {"exchange": "unsupported"}, {"exchange": "binance", "market_type": "future"}, {"exchange": "binance", "offset": -1}, {"exchange": "binance", "limit": 201}])
def test_catalog_rejects_invalid_requests_without_network(monkeypatch, kwargs):
    factory = fake_exchange(monkeypatch, [market("BTC/USDT")])
    with pytest.raises(ValueError):
        service.list_market_symbols_payload(**kwargs)
    factory.assert_not_called()


def test_profile_market_and_missing_profile_are_rejected(monkeypatch):
    monkeypatch.setattr(service, "load_management_snapshot", lambda: {"exchange_profiles": [{"profile_id": "swap", "exchange": "binance", "market_type": "swap"}]})
    with pytest.raises(ValueError, match="市场类型"):
        service.list_market_symbols_payload("swap", require_profile_market=True)
    with pytest.raises(FileNotFoundError):
        service.list_market_symbols_payload("missing")


def test_changed_symbols_checked_but_unchanged_config_can_save_offline(monkeypatch):
    get_catalog = Mock(return_value=([{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}], False))
    monkeypatch.setattr(service, "get_market_catalog", get_catalog)
    previous = {"config_id": "spot", "mode": "SPOT_DCA", "symbols": ["BTC/USDT"], "exchange": "binance"}
    service.validate_spot_market_symbols({**previous, "title": "renamed"}, [], previous)
    get_catalog.assert_not_called()
    service.validate_spot_market_symbols({**previous, "symbols": ["BTC/USDT", "ETH/USDT"]}, [], previous)
    assert get_catalog.call_count == 1
    with pytest.raises(ValueError, match="不可交易"):
        service.validate_spot_market_symbols({**previous, "symbols": ["FAKE/USDT"]}, [], previous)


def test_reenabling_task_or_switching_exchange_revalidates(monkeypatch):
    get_catalog = Mock(return_value=([{"symbol": "BTC/USDT"}], False))
    monkeypatch.setattr(service, "get_market_catalog", get_catalog)
    previous = {"config_id": "spot", "mode": "SPOT_DCA", "symbols": ["BTC/USDT"], "exchange": "binance", "enabled": False}
    service.validate_spot_market_symbols({**previous, "enabled": True}, [], previous)
    service.validate_spot_market_symbols({**previous, "exchange": "okx"}, [], previous)
    assert get_catalog.call_count == 2


def test_spot_validation_rejects_missing_or_futures_profile(monkeypatch):
    get_catalog = Mock()
    monkeypatch.setattr(service, "get_market_catalog", get_catalog)
    agent = {"config_id": "spot", "mode": "SPOT_DCA", "symbols": ["BTC/USDT"], "exchange_profile_id": "profile"}
    with pytest.raises(ValueError, match="不存在"):
        service.validate_spot_market_symbols(agent, [])
    with pytest.raises(ValueError, match="现货交易所"):
        service.validate_spot_market_symbols(agent, [{"profile_id": "profile", "market_type": "swap"}])
    get_catalog.assert_not_called()
