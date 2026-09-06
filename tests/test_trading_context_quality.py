from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.utils.indicators import _pivot_points, _structure_events, calc_vwap, calc_bollinger_bands
from backend.utils.market_data import MarketTool
from backend.utils.formatters import format_market_data_to_text


def test_structure_events_do_not_use_future_pivot_confirmation():
    prices = 100 + np.sin(np.arange(90) * .49) * 6 + np.arange(90) * .08
    frame = pd.DataFrame(dict(open=prices, high=prices + 1, low=prices - 1, close=prices, volume=100))
    def events(df):
        return _structure_events(df, _pivot_points(df.high, 3, 'high'), _pivot_points(df.low, 3, 'low'), 'internal')
    complete = events(frame)
    assert complete
    for stop in range(15, 90):
        # Future bars must never rewrite events before their confirmation time.
        assert events(frame.iloc[:stop]) == [e for e in complete if e['index'] < stop]


def test_vwap_resets_at_utc_midnight():
    df = pd.DataFrame(dict(time=pd.to_datetime(['2026-09-01 23:00Z', '2026-09-02 00:00Z', '2026-09-02 01:00Z']),
                           high=[100, 200, 220], low=[100, 200, 220], close=[100, 200, 220], volume=[1000, 1, 1]))
    assert calc_vwap(df).tolist() == [100, 200, 210]


def test_bollinger_uses_population_deviation():
    values = pd.Series(range(1, 21), dtype=float)
    upper, middle, lower, _ = calc_bollinger_bands(values)
    assert upper.iloc[-1] == values.mean() + 2 * np.std(values)
    assert lower.iloc[-1] == values.mean() - 2 * np.std(values)


def test_forming_candle_cannot_move_agent_indicators():
    end = pd.Timestamp.now(tz='UTC').floor('15min')
    times = pd.date_range(end=end, periods=240, freq='15min')
    bars = [[int(t.timestamp() * 1000), 100, 102, 98, 100, 10] for t in times]
    bars[-1] = [bars[-1][0], 100, 9000, 1, 8000, 999999]
    mt = object.__new__(MarketTool)
    mt.exchange = SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: bars)
    result = mt.process_timeframe('ETH/USDT', '15m')
    assert result['price'] == 100
    assert result['ema']['ema_20'] == 100
    assert result['data_quality']['forming_candles_excluded'] == 1
    assert result['data_quality']['basis'] == 'closed_candles_only'
    assert result['vp']['window_bars'] == 239
    text = format_market_data_to_text({'technical_indicators': {'15m': result}})
    assert 'closed_candles_only' in text and 'UTC' in text
    assert 'OHLCV' in text


def test_short_monthly_history_reports_unavailable_instead_of_zero():
    times = pd.date_range('2024-01-01', periods=12, freq='MS', tz='UTC')
    bars = [[int(t.timestamp() * 1000), 100, 102, 98, 100, 10] for t in times]
    mt = object.__new__(MarketTool)
    mt.exchange = SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: bars)
    result = mt.process_timeframe('ETH/USDT', '1M')
    assert result is not None
    assert result['rsi_analysis']['rsi'] is None
    assert result['ema']['ema_200'] is None
    assert result['bollinger']['available'] is False
