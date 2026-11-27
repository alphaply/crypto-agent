"""Tests for K-line backend caching, in-flight deduplication, isolation, and frontend helpers."""

from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import textwrap
import threading
import time
import pytest

from backend.app.services import stats_service


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
NODE = shutil.which("node")


def _sample_raw_ohlcv(count: int = 30, base_price: float = 100.0) -> list[list[float]]:
    start_ms = 1_700_000_000_000
    rows = []
    for idx in range(count):
        price = base_price + idx * 0.5
        rows.append([start_ms + idx * 3_600_000, price, price + 2.0, price - 1.0, price + 1.0, 50.0 + idx])
    return rows


class _FakeConn:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def execute(self, _sql, _params=()):
        return self

    def fetchall(self):
        return list(self._rows)


@pytest.fixture(autouse=True)
def _reset_kline_cache():
    stats_service.clear_kline_cache()
    yield
    stats_service.clear_kline_cache()


def _patch_strategy_env(monkeypatch, configs: dict[str, dict], ohlcv_fn, mock_rows: list[dict] | None = None):
    monkeypatch.setattr(stats_service.global_config, "get_config_by_id", lambda cid: configs.get(cid))
    monkeypatch.setattr(
        stats_service.global_config,
        "get_binance_credentials",
        lambda config_id=None, symbol=None: (configs.get(config_id, {}).get("api_key", ""), "secret"),
        raising=False,
    )
    monkeypatch.setattr(
        stats_service.global_config,
        "get_exchange_credentials",
        lambda config_id=None: (
            configs.get(config_id, {}).get("exchange", "binance"),
            configs.get(config_id, {}).get("api_key", ""),
            "secret",
            "pass",
        ),
        raising=False,
    )

    instantiations = []

    class FakeExchange:
        def __init__(self, config_id: str):
            self.config_id = config_id

        def fetch_ohlcv(self, symbol, timeframe, limit=500):
            return ohlcv_fn(self.config_id, symbol, timeframe, limit)

    class FakeMarketTool:
        def __init__(self, config_id=None, **_kwargs):
            instantiations.append(config_id)
            self.config_id = config_id
            self.exchange = FakeExchange(config_id)

    monkeypatch.setattr(stats_service, "MarketTool", FakeMarketTool)

    @contextmanager
    def fake_conn():
        yield _FakeConn(mock_rows or [])

    monkeypatch.setattr(stats_service.storage, "get_connection", fake_conn)
    return instantiations


def test_ohlcv_cache_hit_avoids_repeat_exchange_and_market_tool_calls(monkeypatch):
    calls = []
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }
    instantiations = _patch_strategy_env(
        monkeypatch,
        configs,
        lambda cid, sym, tf, limit: calls.append((cid, sym, tf, limit)) or _sample_raw_ohlcv(30, 60000.0),
    )

    first = stats_service.get_kline_payload("cfg-1", "1h")
    second = stats_service.get_kline_payload("cfg-1", "1h")

    assert len(calls) == 1
    assert len(instantiations) == 1
    assert first["candles"] == second["candles"]
    assert first["emas"]["20"] == second["emas"]["20"]
    assert len(first["candles"]) == 30


def test_ohlcv_cache_mutation_does_not_corrupt_cached_entry(monkeypatch):
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }
    _patch_strategy_env(
        monkeypatch,
        configs,
        lambda cid, sym, tf, limit: _sample_raw_ohlcv(25, 50000.0),
    )

    first = stats_service.get_kline_payload("cfg-1", "1h")
    original_close = first["candles"][0]["close"]
    first["candles"][0]["close"] = -999.0
    first["emas"]["20"].clear()

    second = stats_service.get_kline_payload("cfg-1", "1h")
    assert second["candles"][0]["close"] == original_close
    assert len(second["emas"]["20"]) > 0


def test_ohlcv_cache_ttl_expiry_refreshes_from_exchange(monkeypatch):
    calls = []
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }
    _patch_strategy_env(
        monkeypatch,
        configs,
        lambda cid, sym, tf, limit: calls.append(tf) or _sample_raw_ohlcv(25, 100.0 + len(calls)),
    )
    monkeypatch.setitem(stats_service._KLINE_OHLCV_TTL_SECONDS, "1m", 0.05)

    first = stats_service.get_kline_payload("cfg-1", "1m")
    time.sleep(0.08)
    second = stats_service.get_kline_payload("cfg-1", "1m")

    assert len(calls) == 2
    assert first["candles"][0]["open"] != second["candles"][0]["open"]


def test_inflight_request_deduplication_coalesces_concurrent_fetches(monkeypatch):
    calls = []
    started = threading.Event()
    release = threading.Event()
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "ETH/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }

    def slow_fetch(cid, sym, tf, limit):
        calls.append((cid, sym, tf, limit))
        started.set()
        release.wait(timeout=5)
        return _sample_raw_ohlcv(25, 3000.0)

    _patch_strategy_env(monkeypatch, configs, slow_fetch)

    results = []
    errors = []

    def worker():
        try:
            results.append(stats_service.get_kline_payload("cfg-1", "4h"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    assert started.wait(timeout=5)
    time.sleep(0.03)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 5
    assert len(calls) == 1
    assert all(len(item["candles"]) == 25 for item in results)


def test_cache_isolation_across_accounts_symbols_and_timeframes(monkeypatch):
    calls = []
    configs = {
        "acct-a": {"config_id": "acct-a", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
        "acct-a-eth": {"config_id": "acct-a-eth", "symbol": "ETH/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
        "acct-b": {"config_id": "acct-b", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-b"},
    }

    def fetch_by_identity(cid, sym, tf, limit):
        calls.append((cid, sym, tf))
        offset = {"acct-a": 100.0, "acct-a-eth": 100.0, "acct-b": 200.0}[cid] + (10.0 if sym == "ETH/USDT" else 0.0) + (1.0 if tf == "4h" else 0.0)
        return _sample_raw_ohlcv(25, offset)

    _patch_strategy_env(monkeypatch, configs, fetch_by_identity)

    a_btc_1h = stats_service.get_kline_payload("acct-a", "1h", symbol="BTC/USDT")
    b_btc_1h = stats_service.get_kline_payload("acct-b", "1h", symbol="BTC/USDT")
    a_eth_1h = stats_service.get_kline_payload("acct-a-eth", "1h", symbol="ETH/USDT")
    a_btc_4h = stats_service.get_kline_payload("acct-a", "4h", symbol="BTC/USDT")

    assert len(calls) == 4
    assert a_btc_1h["candles"][0]["open"] == 100.0
    assert b_btc_1h["candles"][0]["open"] == 200.0
    assert a_eth_1h["candles"][0]["open"] == 110.0
    assert a_btc_4h["candles"][0]["open"] == 101.0


def test_fail_closed_errors_are_not_cached_and_propagate_to_waiters(monkeypatch):
    attempts = []
    started = threading.Event()
    release = threading.Event()
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }

    def flaky_fetch(cid, sym, tf, limit):
        attempts.append(1)
        if len(attempts) == 1:
            started.set()
            release.wait(timeout=5)
            raise RuntimeError("exchange down")
        return _sample_raw_ohlcv(25, 42000.0)

    _patch_strategy_env(monkeypatch, configs, flaky_fetch)

    errors = []

    def worker():
        try:
            stats_service.get_kline_payload("cfg-1", "1h")
        except RuntimeError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    assert started.wait(timeout=5)
    time.sleep(0.03)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(attempts) == 1
    assert errors == ["exchange down", "exchange down", "exchange down"]

    # Next request retries fresh instead of reading a cached failure.
    recovered = stats_service.get_kline_payload("cfg-1", "1h")
    assert len(attempts) == 2
    assert len(recovered["candles"]) == 25


def test_empty_ohlcv_is_not_cached_and_preserves_schema(monkeypatch):
    attempts = []
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }

    def empty_then_ready(cid, sym, tf, limit):
        attempts.append(1)
        if len(attempts) == 1:
            return []
        return _sample_raw_ohlcv(25, 100.0)

    _patch_strategy_env(monkeypatch, configs, empty_then_ready)

    empty = stats_service.get_kline_payload("cfg-1", "1h")
    assert empty == {
        "symbol": "BTC/USDT",
        "candles": [],
        "volume": [],
        "emas": {},
        "orders": [],
        "position": None,
        "positions": [],
        "pending_orders": [],
        "risk_lines": [],
    }

    ready = stats_service.get_kline_payload("cfg-1", "1h")
    assert len(attempts) == 2
    assert len(ready["candles"]) == 25


def test_strategy_mode_splits_filled_positions_and_pending_orders_in_single_query(monkeypatch):
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY", "exchange": "binance", "api_key": "key-a"},
    }
    mock_rows = [
        {"side": "BUY", "price": 100.0, "amount": 2.0, "stop_loss": 95.0, "take_profit": 120.0, "order_id": "ord-1", "status": "OPEN", "is_filled": 1},
        {"side": "SELL", "price": 130.0, "amount": 1.0, "stop_loss": 0.0, "take_profit": 0.0, "order_id": "ord-2", "status": "OPEN", "is_filled": 0},
    ]
    _patch_strategy_env(
        monkeypatch,
        configs,
        lambda cid, sym, tf, limit: _sample_raw_ohlcv(25, 100.0),
        mock_rows=mock_rows,
    )

    payload = stats_service.get_kline_payload("cfg-1", "1h")
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["side"] == "LONG"
    assert payload["positions"][0]["order_id"] == "ord-1"
    assert len(payload["pending_orders"]) == 1
    assert payload["pending_orders"][0]["order_id"] == "ord-2"
    assert payload["pending_orders"][0]["type"] == "open_short"
    assert {line["type"] for line in payload["risk_lines"]} == {"take_profit", "stop_loss"}


def test_validation_errors_remain_fail_closed(monkeypatch):
    configs = {
        "cfg-1": {"config_id": "cfg-1", "symbol": "BTC/USDT", "mode": "STRATEGY"},
    }
    _patch_strategy_env(monkeypatch, configs, lambda *args: [])

    with pytest.raises(ValueError):
        stats_service.get_kline_payload("cfg-1", "2h")
    with pytest.raises(FileNotFoundError):
        stats_service.get_kline_payload("missing", "1h")
    with pytest.raises(ValueError):
        stats_service.get_kline_payload("cfg-1", "1h", symbol="SOL/USDT")


@pytest.mark.skipUnless(NODE, "Node.js is required for frontend kline helper tests")
def test_frontend_kline_cache_and_binary_search_helpers():
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            textwrap.dedent("""
                import assert from 'node:assert/strict';
                import { createKlineCache, findCandleByTime, formatKlineVolume, klineCacheKey } from './src/lib/kline.js';

                const bars = [10, 20, 30, 40, 50].map(time => ({ time, close: time * 2 }));
                assert.deepEqual(findCandleByTime(bars, 30), { time: 30, close: 60 });
                assert.equal(findCandleByTime(bars, 25), null);
                assert.equal(findCandleByTime([], 10), null);

                assert.equal(formatKlineVolume(1_500_000_000), '1.50B');
                assert.equal(formatKlineVolume(2_340_000), '2.34M');
                assert.equal(formatKlineVolume(12_500), '12.50K');
                assert.equal(formatKlineVolume(42.126), '42.13');
                assert.equal(formatKlineVolume(0), '0');

                const cache = createKlineCache({ maxEntries: 2, staleMs: 5000 });
                const k1 = klineCacheKey('cfg-1', 'BTC/USDT', '1h');
                const k2 = klineCacheKey('cfg-1', 'BTC/USDT', '4h');
                const k3 = klineCacheKey('cfg-1', 'ETH/USDT', '1h');
                cache.set(k1, { v: 1 }, 1000);
                cache.set(k2, { v: 2 }, 1500);
                assert.equal(cache.isFresh(cache.get(k1), 5999), true);
                assert.equal(cache.isFresh(cache.get(k1), 6000), false);
                // Accessing k1 made k2 the oldest entry, so adding k3 evicts k2.
                cache.set(k3, { v: 3 }, 2000);
                assert.equal(cache.size(), 2);
                assert.equal(cache.get(k2), null);
                assert.deepEqual(cache.get(k1).data, { v: 1 });
                assert.deepEqual(cache.get(k3).data, { v: 3 });
            """),
        ],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
