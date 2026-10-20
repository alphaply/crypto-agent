"""Current quotes must never be inferred from a closed or forming candle."""

from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from backend.agent import agent_graph, chat_graph
from backend.agent.agent_models import AgentState
from backend.agent.decision_context import DecisionMemory
from backend.utils.formatters import format_market_data_to_text
from backend.utils.market_data import MarketTool


@pytest.fixture
def quote_market():
    end = pd.Timestamp.now(tz='UTC').floor('1h')
    times = pd.date_range(end=end, periods=261, freq='1h')
    bars = [[int(t.timestamp() * 1000), 2410, 2412, 2408, 2410.51, 100] for t in times]
    bars[-1] = [bars[-1][0], 2410, 2460, 2408, 2444, 200]
    tool = object.__new__(MarketTool)
    tool.runtime_config = {'market_profile': 'hourly'}
    tool.exchange = SimpleNamespace(
        id='binance',
        fetch_ohlcv=Mock(return_value=bars),
        fetch_ticker=Mock(return_value={'last': 2450, 'timestamp': 1792506607000}),
    )
    tool._fetch_market_derivatives = Mock(return_value={})
    return tool


def test_closed_2410_and_forming_2444_do_not_replace_live_2450(quote_market):
    events = []
    quote_market.exchange.fetch_ohlcv.side_effect = lambda *a, **kw: (
        events.append('candles') or quote_market.exchange.fetch_ohlcv.return_value
    )
    quote_market.exchange.fetch_ticker.side_effect = lambda symbol: (
        events.append('ticker') or {'last': 2450, 'timestamp': 1792506607000}
    )
    market = quote_market.get_market_analysis('ETH/USDT:USDT', timeframes=['1h'])
    assert events == ['candles', 'ticker']
    assert market['analysis']['1h']['price'] == 2410.51
    assert market['analysis']['1h']['data_quality']['forming_candles_excluded'] == 1
    assert market['live_quote']['price'] == 2450
    assert market['live_quote']['exchange_timestamp_ms'] == 1792506607000
    assert market['live_quote']['fetched_at'].endswith('+00:00')
    quote_market.exchange.fetch_ticker.assert_called_once_with('ETH/USDT:USDT')
    text = chat_graph._temporary_market_context_text(market)
    assert 'Price: 2450.0' in text and 'Close=2410.51' in text
    assert 'binance ETH/USDT:USDT ticker.last' in text
    assert 'fetched_at:' in text and 'exchange_timestamp_ms: 1792506607000' in text


@pytest.mark.parametrize('last', [None, 0, -1, float('nan'), float('inf'), 'bad'])
def test_invalid_last_never_falls_back_to_ticker_close_or_candle(quote_market, last):
    quote_market.exchange.fetch_ticker.return_value = {'last': last, 'close': 2450}
    market = quote_market.get_market_analysis('ETH/USDT:USDT', timeframes=['1h'])
    assert market['live_quote']['available'] is False
    assert market['live_quote']['price'] is None
    text = chat_graph._temporary_market_context_text(market)
    assert 'Price: N/A' in text and 'Close=2410.51' in text
    assert 'Price: 2410.51' not in text and 'Price: 0' not in text


def test_quote_failure_keeps_closed_indicators_without_inventing_current_price(quote_market):
    quote_market.exchange.fetch_ticker.side_effect = TimeoutError('offline')
    market = quote_market.get_market_analysis('ETH/USDT:USDT', timeframes=['1h'])
    assert market['analysis']['1h']['price'] == 2410.51
    assert market['live_quote']['error'] == 'TimeoutError'
    assert 'Price: N/A' in chat_graph._temporary_market_context_text(market)


def test_missing_exchange_timestamp_is_not_replaced_by_fetch_time(quote_market):
    quote_market.exchange.fetch_ticker.return_value = {'last': 2450}
    quote = quote_market._fetch_live_quote('ETH/USDT:USDT')
    text = format_market_data_to_text({'current_price': quote['price'], 'live_quote': quote})
    assert quote['available'] and quote['exchange_timestamp_ms'] is None
    assert 'exchange_timestamp_ms: N/A' in text and quote['fetched_at'] in text


@pytest.mark.parametrize('timeframes', [['1h', '4h', '1d'], ['15m', '1h']])
@pytest.mark.parametrize('available', [True, False])
def test_decision_prompt_uses_live_price_even_with_15m_or_hourly_context(
    monkeypatch, timeframes, available,
):
    market = Mock()
    market.get_market_analysis.return_value = {
        'analysis': {tf: {'price': 2410.51, 'atr': 16.389869} for tf in timeframes},
        'live_quote': {'available': available, 'price': 2450 if available else None,
                       'source': 'ticker.last', 'symbol': 'ETH/USDT:USDT', 'exchange': 'binance',
                       'fetched_at': '2026-10-20T14:30:07+00:00'},
    }
    market.get_account_status.return_value = {
        'balance': 1000, 'mock_open_orders': [
            {'is_filled': 1, 'side': 'SELL', 'price': 2422, 'amount': .14},
        ],
    }
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'load_decision_memory', lambda *_: DecisionMemory('', ''))
    monkeypatch.setattr(agent_graph, 'fetch_news_risk_context', lambda *_: {})
    monkeypatch.setattr(agent_graph.database, 'save_news_snapshot', Mock())
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda *_: 2)
    monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_:
                        '最新价格：{current_price}\n{formatted_market_data}\n{positions_text}')
    state = AgentState(symbol='ETH/USDT:USDT', messages=[], market_context={}, account_context={})
    result = agent_graph.start_node(state, {'configurable': {
        'config_id': 'quote-test', 'agent_config': {
            'symbol': state.symbol, 'mode': 'STRATEGY', 'exit_mode': 'attached_optional',
            'market_timeframes': timeframes,
        },
    }})
    prompt = result.messages[0].content
    assert f"最新价格：{2450 if available else 'N/A'}" in prompt
    assert f"Price: {2450 if available else 'N/A'}" in prompt
    assert 'Close=2410.51' in prompt and '最新价格：2410.51' not in prompt
    assert ('PnL: -3.920' if available else 'PnL: N/A') in prompt
    assert 'Base ATR: 16.389869' in prompt


def test_spot_quotes_keep_the_configured_spot_symbol(quote_market):
    quote_market.market_type = 'spot'
    quote_market.process_timeframe = Mock(return_value={
        'price': 2410.51, 'indicator_profile': 'spot_long_term',
    })
    market = quote_market.get_market_analysis('ETH/USDC', mode='SPOT_DCA', timeframes=['4h'])
    quote_market._fetch_market_derivatives.assert_not_called()
    quote_market.exchange.fetch_ticker.assert_called_once_with('ETH/USDC')
    text = chat_graph._temporary_market_context_text(market)
    assert 'Live price: 2450.0' in text and 'Close=2410.51' in text
    assert 'binance ETH/USDC ticker.last' in text
