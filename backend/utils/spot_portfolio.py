"""Shared spot task symbol contract, independent of runtime/database imports."""

import re
from typing import Any

SPOT_MARKET_TIMEFRAMES = ["4h", "1d", "1w"]
MAX_SPOT_SYMBOLS = 10


def normalize_spot_symbols(raw_symbols: Any, fallback_symbol: str | None = None) -> list[str]:
    if raw_symbols is None or raw_symbols == []:
        raw_symbols = [fallback_symbol] if fallback_symbol else []
    if not isinstance(raw_symbols, list):
        raise ValueError("现货标的 symbols 必须为数组")
    symbols = []
    for raw in raw_symbols:
        if not isinstance(raw, str):
            raise ValueError("现货标的必须为 BASE/QUOTE 字符串")
        symbol = raw.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*/[A-Z0-9][A-Z0-9._-]*", symbol):
            raise ValueError(f"无效现货标的 {raw!r}，请使用 BASE/QUOTE 格式")
        if symbol.split('/')[0] == symbol.split('/')[1]:
            raise ValueError("现货基础币与计价币不能相同")
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols or len(symbols) > MAX_SPOT_SYMBOLS:
        raise ValueError(f"现货任务需配置 1–{MAX_SPOT_SYMBOLS} 个标的")
    if len({symbol.split('/')[1] for symbol in symbols}) != 1:
        raise ValueError("同一现货任务的标的必须使用相同计价币，以共用预算")
    return symbols


def get_config_symbols(config: dict | None) -> list[str]:
    config = config or {}
    if str(config.get("mode") or "").upper() == "SPOT_DCA":
        return normalize_spot_symbols(config.get("symbols"), config.get("symbol"))
    return [config["symbol"]] if config.get("symbol") else []
