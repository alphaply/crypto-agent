"""Public exchange instrument discovery shared by configuration and chat."""

import os
import threading
import time
from typing import Any

import ccxt

from backend.config_store import load_management_snapshot
from backend.utils.spot_portfolio import normalize_spot_symbols


SUPPORTED_MARKETS = {"binance": ("spot", "swap"), "okx": ("spot", "swap")}
MARKET_SYMBOL_CACHE_TTL_SECONDS = 5 * 60
_market_symbol_cache: dict[tuple[str, str], tuple[float, list[dict[str, str]]]] = {}
_catalog_locks = {
    (exchange, market): threading.Lock()
    for exchange, markets in SUPPORTED_MARKETS.items()
    for market in markets
}


class MarketCatalogUnavailable(RuntimeError):
    """A public market catalogue could not be verified against the exchange."""


def _validate_exchange_market(exchange: str, market_type: str) -> None:
    if exchange not in SUPPORTED_MARKETS or market_type not in SUPPORTED_MARKETS[exchange]:
        raise ValueError("Unsupported exchange market")


def _public_exchange(exchange: str, market_type: str):
    try:
        timeout = max(1000, int(os.getenv("EXCHANGE_TIMEOUT_MS", "15000")))
    except ValueError:
        timeout = 15000
    # Never pass profile credentials. In particular, Binance currencies use a
    # private endpoint; only public instruments are needed by this catalogue.
    fetch_type = "linear" if exchange == "binance" and market_type == "swap" else market_type
    options = {
        "enableRateLimit": True,
        "timeout": timeout,
        "options": {
            "defaultType": market_type,
            "fetchCurrencies": False,
            "fetchMarkets": {"types": [fetch_type]},
            "adjustForTimeDifference": False,
        },
    }
    factory = ccxt.okx if exchange == "okx" else ccxt.binance
    return factory(options)


def get_market_catalog(exchange: str, market_type: str = "spot") -> tuple[list[dict[str, str]], bool]:
    exchange = str(exchange or "").strip().lower()
    market_type = str(market_type or "spot").strip().lower()
    _validate_exchange_market(exchange, market_type)
    key = (exchange, market_type)
    # Serialize cache misses per public exchange market, not per private profile.
    with _catalog_locks[key]:
        cached = _market_symbol_cache.get(key)
        if cached and time.monotonic() - cached[0] < MARKET_SYMBOL_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]], True
        try:
            markets = _public_exchange(exchange, market_type).load_markets()
            records = {}
            for market in markets.values():
                if market.get("active") is not True or not market.get(market_type):
                    continue
                symbol = str(market.get("symbol") or "")
                base = str(market.get("base") or "")
                quote = str(market.get("quote") or "")
                if not symbol or not base or not quote:
                    continue
                if market_type == "spot":
                    # Keep the same canonical BASE/QUOTE contract as saved tasks.
                    try:
                        if normalize_spot_symbols([symbol])[0] != symbol:
                            continue
                    except ValueError:
                        continue
                records[symbol] = {
                    "symbol": symbol,
                    "base": base,
                    "quote": quote,
                    "market_type": market_type,
                    "linear": market.get("linear") is True,
                    "display_name": f"{symbol} · {market_type.upper()}",
                }
            preferred_quotes = {"USDT": 0, "USDC": 1, "FDUSD": 2, "USD": 3}
            catalog = sorted(records.values(), key=lambda item: (preferred_quotes.get(item["quote"], 9), item["symbol"]))
            if not catalog:
                raise ValueError("Empty active instrument catalogue")
        except Exception as exc:
            # Upstream exceptions may contain URLs or other transport internals.
            # Expose a useful, stable message and do not save unverified targets.
            raise MarketCatalogUnavailable(
                f"无法获取 {exchange.upper()} {market_type} 可交易标的，请稍后重试"
            ) from exc
        _market_symbol_cache[key] = (time.monotonic(), catalog)
        return [dict(item) for item in catalog], False


def list_market_symbols_payload(
    exchange_profile_id: str | None = None,
    market_type: str = "spot",
    keyword: str = "",
    *,
    exchange: str | None = None,
    quote: str = "",
    limit: int = 100,
    offset: int = 0,
    symbols: list[str] | None = None,
    require_profile_market: bool = False,
    linear_only: bool = False,
) -> dict[str, Any]:
    profile_id = str(exchange_profile_id or "").strip()
    requested_market = str(market_type or "spot").strip().lower()
    requested_exchange = str(exchange or "").strip().lower()
    if profile_id:
        profiles = load_management_snapshot().get("exchange_profiles", [])
        profile = next((item for item in profiles if item.get("profile_id") == profile_id), None)
        if not profile:
            raise FileNotFoundError("Exchange profile not found")
        requested_exchange = str(profile.get("exchange") or "").lower()
        if require_profile_market and str(profile.get("market_type") or "swap").lower() != requested_market:
            raise ValueError("交易所配置的市场类型与请求不一致，请选择现货配置")
    elif not requested_exchange:
        raise ValueError("Exchange profile or exchange is required")
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1–200 and offset must be nonnegative")
    catalog, cached = get_market_catalog(requested_exchange, requested_market)
    if linear_only and requested_market == 'swap':
        catalog = [item for item in catalog if item.get('linear') is True]
    by_symbol = {item["symbol"]: item for item in catalog}
    selected = list(dict.fromkeys(str(item).strip().upper() for item in symbols or [] if str(item).strip()))
    needle = str(keyword or "").strip().upper()
    requested_quote = str(quote or "").strip().upper()
    filtered = [
        item for item in catalog
        if (not needle or needle in item["symbol"].upper() or needle in item["base"].upper())
        and (not requested_quote or requested_quote == item["quote"].upper())
    ]
    return {
        "exchange_profile_id": profile_id or None,
        "exchange": requested_exchange,
        "market_type": requested_market,
        "symbols": filtered[offset:offset + limit],
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(filtered),
        "quote_currencies": sorted({item["quote"] for item in catalog}),
        "selected_symbols": [by_symbol[symbol] for symbol in selected if symbol in by_symbol],
        "invalid_symbols": [symbol for symbol in selected if symbol not in by_symbol],
        "cached": cached,
    }


def validate_spot_market_symbols(agent: dict, profiles: list[dict], previous: dict | None = None) -> None:
    """Verify changed targets without making unrelated config edits depend on the exchange."""
    if str(agent.get("mode") or "").upper() != "SPOT_DCA":
        return
    config_id = str(agent.get("config_id") or "agent")
    profile_id = str(agent.get("exchange_profile_id") or "")
    profile = next((item for item in profiles if item.get("profile_id") == profile_id), None) if profile_id else None
    if profile_id and not profile:
        raise ValueError(f"现货任务 {config_id} 的交易所配置不存在: {profile_id}")
    if profile and str(profile.get("market_type") or "swap").lower() != "spot":
        raise ValueError(f"现货任务 {config_id} 必须选择现货交易所配置")
    exchange = str((profile or {}).get("exchange") or agent.get("exchange") or "binance").lower()
    _validate_exchange_market(exchange, "spot")
    symbols = normalize_spot_symbols(agent.get("symbols"), agent.get("symbol"))
    previous = previous or {}
    if (
        str(previous.get("mode") or "").upper() == "SPOT_DCA"
        and str(previous.get("exchange") or "binance").lower() == exchange
        and str(previous.get("exchange_profile_id") or "") == profile_id
        and set(normalize_spot_symbols(previous.get("symbols"), previous.get("symbol"))) == set(symbols)
        and not (agent.get("enabled", True) and not previous.get("enabled", True))
    ):
        return
    catalog, _ = get_market_catalog(exchange, "spot")
    allowed = {item["symbol"] for item in catalog}
    invalid = [symbol for symbol in symbols if symbol not in allowed]
    if invalid:
        raise ValueError(f"现货任务 {config_id} 包含 {exchange.upper()} 不可交易的标的: {', '.join(invalid)}")
