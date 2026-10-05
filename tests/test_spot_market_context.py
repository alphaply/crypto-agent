from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from backend.utils.formatters import format_market_data_to_text
from backend.utils.indicators import build_spot_indicator_context
from backend.utils.market_data import MarketTool


def frame(count=220):
    closes = np.arange(count, dtype=float) + 100
    return pd.DataFrame({'close': closes, 'high': closes + 2, 'low': closes - 2, 'volume': 10.0})


def test_spot_context_reports_long_horizon_metrics_without_contract_indicators():
    bars = frame()
    bars.loc[219, 'volume'] = 30
    context = build_spot_indicator_context(bars, 180)
    assert set(context['ema']) == {'ema_20', 'ema_50', 'ema_200'}
    assert context['rsi_analysis']['rsi'] == 100
    assert context['atr'] == 4
    assert context['volume_analysis']['ratio'] == 3
    assert context['spot_context']['atr_pct'] == pytest.approx(4 / 319 * 100)
    assert context['spot_context']['recent_high'] == 321
    assert context['spot_context']['drawdown_from_high_pct'] == pytest.approx((319 / 321 - 1) * 100)
    assert 'macd' not in context and 'smc' not in context


def test_short_history_and_zero_volume_remain_explicitly_unavailable():
    context = build_spot_indicator_context(frame(12).assign(volume=0), 52)
    assert all(value is None for value in context['ema'].values())
    assert context['rsi_analysis']['rsi'] is None
    assert context['atr'] is None
    assert context['volume_analysis']['ratio'] is None
    assert context['spot_context']['recent_high'] is None
    assert context['spot_context']['drawdown_from_high_pct'] is None
    assert context['spot_context']['high_available_bars'] == 12


def test_spot_profile_filters_forming_candle_and_formats_missing_ema200():
    end = pd.Timestamp.now(tz='UTC').floor('4h')
    times = pd.date_range(end=end, periods=80, freq='4h')
    bars = [[int(t.timestamp() * 1000), 100, 102, 98, 100, 10] for t in times]
    bars[-1] = [bars[-1][0], 100, 9000, 1, 8000, 999999]
    tool = object.__new__(MarketTool)
    tool.exchange = SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: bars)
    result = tool.process_timeframe('ETH/USDT', '4h', profile='spot')
    assert result['price'] == 100
    assert result['ema']['ema_20'] == 100
    assert result['ema']['ema_200'] is None
    assert result['data_quality']['forming_candles_excluded'] == 1
    assert len(result['recent_closes']) == 5
    text = format_market_data_to_text({'technical_indicators': {'4h': result}})
    assert '100.0/100.0/N/A' in text
    assert 'Funding' not in text and 'MACD' not in text and 'SMC' not in text
    assert 'closed_candles_only' in text


def test_spot_default_timeframes_skip_derivatives_and_explicit_timeframes_are_honored():
    tool = object.__new__(MarketTool)
    tool.process_timeframe = Mock(return_value={'price': 100})
    tool._fetch_market_derivatives = Mock(side_effect=AssertionError('spot must not fetch futures data'))
    result = tool.get_market_analysis('BTC/USDT', mode='SPOT_DCA')
    assert list(result['analysis']) == ['4h', '1d', '1w']
    assert result['sentiment'] == {}
    tool.process_timeframe.reset_mock()
    result = tool.get_market_analysis('BTC/USDT', mode='SPOT_DCA', timeframes=['1d'])
    assert list(result['analysis']) == ['1d']
    result['technical_indicators'] = {'1M': {'indicator_profile': 'spot_long_term'}}
    assert '[1M]' in format_market_data_to_text(result)


def test_spot_account_reads_actual_quote_and_base_holdings_without_futures_requests():
    tool = object.__new__(MarketTool)
    calls = []
    def orders(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return []
    tool.exchange = SimpleNamespace(
        options={'defaultType': 'spot'},
        fetch_balance=lambda: {'USDT': {'total': 9999, 'free': 9999},
                               'USDC': {'total': 100, 'free': 80}, 'ETH': {'total': 2, 'free': 1}},
        fetch_open_orders=orders,
    )
    result = tool.get_account_status('ETH/USDC', is_real=True)
    assert result['quote_asset'] == 'USDC'
    assert result['balance'] == 100 and result['available_balance'] == 80
    assert result['real_positions'][0]['amount'] == 2
    assert result['real_positions'][0]['entry_price'] is None
    assert result['spot_holdings']['free'] == 1
    assert calls == [('ETH/USDC', {})]
