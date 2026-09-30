"""Verify bounded prompt data without sacrificing indicator history or data quality."""

import json
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.utils.formatters import format_market_data_to_text
from backend.utils.indicators import calc_ema, calc_vwap, smart_fmt
from backend.utils.market_data import MarketTool


def market_bars(count=260, frequency='15min'):
    end = pd.Timestamp.now(tz='UTC').floor(frequency) - pd.Timedelta(frequency)
    times = pd.date_range(end=end, periods=count, freq=frequency)
    closes = 100 + np.arange(count) * .2 + np.sin(np.arange(count) * .4)
    return [[int(stamp.timestamp() * 1000), close - .1, close + 2, close - 2, close, 100 + index]
            for index, (stamp, close) in enumerate(zip(times, closes))]


def analyze(bars, timeframe='15m'):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return bars

    tool = object.__new__(MarketTool)
    tool.exchange = SimpleNamespace(fetch_ohlcv=fetch)
    return tool.process_timeframe('TEST/USDT', timeframe), calls


def test_10_visible_candles_retain_full_indicator_warmup_and_aligned_volume():
    bars = market_bars()
    result, calls = analyze(bars)
    closes = pd.Series([bar[4] for bar in bars])
    assert calls == [{'limit': 1000}]
    assert result['data_quality']['bars'] == 260
    assert result['data_quality']['display_bars'] == 10
    assert result['ema']['ema_200'] == smart_fmt(calc_ema(closes, 200).iloc[-1])
    assert result['ema']['ema_200'] != smart_fmt(calc_ema(closes.tail(10), 200).iloc[-1])
    assert len(result['recent_times']) == len(result['recent_closes']) == len(result['recent_volumes']) == 10
    assert result['recent_closes'][0] == smart_fmt(bars[-10][4])
    assert result['recent_volumes'] == [bar[5] for bar in bars[-10:]]
    assert result['volume_analysis']['ratio'] == round(bars[-1][5] / np.mean([bar[5] for bar in bars[-21:-1]]), 2)
    # No NaN/Infinity can leak into a strict model/API JSON payload.
    json.dumps(result, allow_nan=False)


def test_normalized_context_uses_raw_values_and_prior_range_excludes_current():
    bars = market_bars()
    bars[-1][2] = bars[-1][4] + 30
    result, _ = analyze(bars)
    context = result['decision_context']
    closes = pd.Series([bar[4] for bar in bars])
    assert context['momentum']['return_15_bars_pct'] == round((closes.iloc[-1] / closes.iloc[-16] - 1) * 100, 3)
    assert context['prior_range_20']['high'] == smart_fmt(max(bar[2] for bar in bars[-21:-1]))
    assert context['prior_range_20']['high'] < bars[-1][2]
    assert context['volatility']['atr_pct'] == pytest.approx(result['atr'] / result['price'] * 100, abs=.0005)
    assert context['trend']['price_minus_ema20_atr'] == pytest.approx(
        (closes.iloc[-1] - calc_ema(closes, 20).iloc[-1]) / result['atr'], abs=.0005)


def test_extreme_forming_candle_cannot_change_derived_context_or_visible_candles():
    bars = market_bars()
    clean, _ = analyze(bars)
    forming = [int(pd.Timestamp.now(tz='UTC').floor('15min').timestamp() * 1000), 100, 99999, 1, 90000, 999999]
    result, _ = analyze(bars + [forming])
    for field in ('decision_context', 'recent_closes', 'recent_volumes', 'recent_times', 'ema', 'rsi_analysis', 'macd'):
        assert result[field] == clean[field]
    assert result['data_quality']['forming_candles_excluded'] == 1


def test_zero_volume_and_zero_atr_are_unavailable_not_synthetic_signals():
    bars = [[bar[0], 100, 100, 100, 100, 0] for bar in market_bars()]
    result, _ = analyze(bars)
    assert result['volume_analysis']['ratio'] is None
    assert result['vp']['available'] is False
    assert 'vwap' not in result
    assert result['decision_context']['momentum']['macd_hist_atr'] is None
    assert result['decision_context']['volatility']['atr_pct'] == 0
    frame = pd.DataFrame({'high': [100], 'low': [100], 'close': [100], 'volume': [0]})
    assert calc_vwap(frame).isna().all()
    json.dumps(result, allow_nan=False)


def test_invalid_timestamps_are_excluded_and_missing_period_is_visible():
    bars = market_bars()
    bars[100][0] = 'invalid'
    result, _ = analyze(bars)
    assert result['data_quality']['invalid_candles_excluded'] == 1
    assert result['data_quality']['gap_count'] == 1
    assert result['data_quality']['bars'] == 259
    text = format_market_data_to_text({'technical_indicators': {'15m': result}})
    assert 'DATA QUALITY WARNING' in text


def test_daily_prompt_has_10_timestamped_closed_ohlcv_rows_and_normalized_context():
    result, _ = analyze(market_bars(frequency='1d'), timeframe='1d')
    text = format_market_data_to_text({'technical_indicators': {'1d': result}})
    candles = next(line for line in text.splitlines() if 'Recent 10 candles' in line)
    assert 'UTC open,O,H,L,C,V' in candles
    assert candles.count('[') == 10
    assert result['recent_times'][0] in candles
    assert result['recent_times'][-1] in candles
    assert 'RVOL20=' in text
    assert '(C-EMA20)/ATR=' in text
    assert 'Close returns 1/3/15 bars (%)' in text
    assert 'CHOP14=' in text and 'CMF20=' in text and 'Squeeze(' in text
    assert 'not net capital flow' in text
    assert 'not a live quote' in text


def test_price_precision_preserves_small_tokens_and_missing_values():
    assert smart_fmt(.000000000123456789) == .00000000012345679
    assert smart_fmt(123456.789) == 123456.79
    assert smart_fmt(float('nan')) is None
    assert smart_fmt(float('inf')) is None


def test_short_monthly_prompt_marks_unavailable_indicators_without_zero_fallback():
    timestamps = pd.date_range('2024-01-01', periods=12, freq='MS', tz='UTC')
    bars = [[int(stamp.timestamp() * 1000), 100, 102, 98, 100, 10] for stamp in timestamps]
    result, _ = analyze(bars, '1M')
    text = format_market_data_to_text({'technical_indicators': {'1M': result}})
    assert 'RSI=N/A' in text
    assert 'MACD: Diff=N/A Hist=N/A' in text
    assert 'Volume Profile: N/A' in text
    assert not re.search(r'\bnan\b', text, re.IGNORECASE)
    json.dumps(result, allow_nan=False)


def test_fourteen_closed_bars_suffice_for_atr_but_not_rsi():
    timestamps = pd.date_range('2024-01-01', periods=14, freq='MS', tz='UTC')
    bars = [[int(stamp.timestamp() * 1000), 100, 102, 98, 100, 10] for stamp in timestamps]
    result, _ = analyze(bars, '1M')
    assert result['rsi_analysis']['rsi'] is None
    assert result['atr'] == 4
    assert result['decision_context']['volatility']['atr_pct'] == 4
